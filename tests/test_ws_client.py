"""WebSocket client: state machine, reconnect, subscriptions, shutdown.
No real network involved - a fake transport drives every scenario."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from tests.conftest import FakeClock

from app.adapters.polymarket_ws import PolymarketWsClient
from app.config import PolymarketWsSettings
from app.domain.enums import WsConnectionState


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]

    async def close(self) -> None:
        self.closed = True
        # unblock any pending recv
        self.incoming.put_nowait(ConnectionError("closed"))

    def feed(self, payload: Any) -> None:
        self.incoming.put_nowait(json.dumps(payload))

    def feed_raw(self, raw: str) -> None:
        self.incoming.put_nowait(raw)

    def feed_error(self, exc: Exception) -> None:
        self.incoming.put_nowait(exc)

    def sub_channels(self) -> set[str]:
        channels: set[str] = set()
        for message in self.sent:
            if message.get("req") == "sub":
                channels.update(message["chs"])
        return channels


async def until(predicate, deadline_seconds: float = 2.0) -> None:
    """Poll a predicate against the fake transport (no external events exist
    to await, so bounded polling is the correct tool here)."""
    async with asyncio.timeout(deadline_seconds):
        while not predicate():  # noqa: ASYNC110 - polling a plain predicate
            await asyncio.sleep(0.005)


def make_client(transports: list[Any], **overrides: Any):
    settings = PolymarketWsSettings(_env_file=None, **overrides)
    clock = FakeClock()
    sleeps: list[float] = []
    index = {"value": 0}

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)
        await asyncio.sleep(0)

    async def connect():
        item = transports[min(index["value"], len(transports) - 1)]
        index["value"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    states: list[WsConnectionState] = []

    async def on_state(state: WsConnectionState) -> None:
        states.append(state)

    client = PolymarketWsClient(
        settings,
        connect=connect,
        clock=clock,
        sleep=fake_sleep,
        rng=lambda: 0.0,
        on_state_change=on_state,
    )
    return client, sleeps, states


async def test_connect_subscribes_desired_channels() -> None:
    transport = FakeTransport()
    client, _, states = make_client([transport])
    await client.set_subscriptions({"tickers::1", "bbo::1"})
    await client.start()
    try:
        await until(lambda: client.is_connected and len(transport.sent) > 0)
        assert transport.sub_channels() == {"tickers::1", "bbo::1"}
        assert client.active_subscription_count == 2
        assert WsConnectionState.CONNECTING in states
        assert WsConnectionState.CONNECTED in states
    finally:
        await client.stop()
    assert client.state is WsConnectionState.DISCONNECTED


async def test_set_subscriptions_is_idempotent_and_diffs() -> None:
    transport = FakeTransport()
    client, _, _ = make_client([transport])
    await client.start()
    try:
        await until(lambda: client.is_connected)
        await client.set_subscriptions({"tickers::1"})
        await client.set_subscriptions({"tickers::1"})  # identical: no new messages
        sub_messages = [m for m in transport.sent if m["req"] == "sub"]
        assert len(sub_messages) == 1

        await client.set_subscriptions({"tickers::1", "trades::1"})
        sub_messages = [m for m in transport.sent if m["req"] == "sub"]
        assert sub_messages[-1]["chs"] == ["trades::1"]  # only the delta

        await client.set_subscriptions({"trades::1"})
        unsub_messages = [m for m in transport.sent if m["req"] == "unsub"]
        assert unsub_messages[-1]["chs"] == ["tickers::1"]
        assert client.active_subscription_count == 1
    finally:
        await client.stop()


async def test_resubscribe_after_reconnect() -> None:
    first, second = FakeTransport(), FakeTransport()
    client, _, states = make_client([first, second])
    await client.set_subscriptions({"tickers::1", "book::1"})
    await client.start()
    try:
        await until(lambda: len(first.sent) > 0)
        first.feed_error(ConnectionError("heartbeat timeout"))
        await until(lambda: len(second.sent) > 0)
        assert second.sub_channels() == {"tickers::1", "book::1"}
        assert client.reconnect_count == 1
        assert WsConnectionState.RECONNECTING in states
    finally:
        await client.stop()


async def test_repeated_failures_lead_to_degraded_with_max_backoff() -> None:
    client, sleeps, _ = make_client(
        [ConnectionError("refused")],
        max_reconnect_attempts=3,
        reconnect_min_seconds=1.0,
        reconnect_max_seconds=8.0,
        reconnect_jitter_seconds=0.0,
        reconnect_window_seconds=10_000.0,
    )
    await client.start()
    try:
        await until(lambda: client.state is WsConnectionState.DEGRADED, deadline_seconds=5.0)
        await until(lambda: len(sleeps) >= 5, deadline_seconds=5.0)
    finally:
        await client.stop()
    # exponential 1, 2, 4 then capped at max backoff once degraded
    assert sleeps[0] == pytest.approx(1.0)
    assert sleeps[1] == pytest.approx(2.0)
    assert sleeps[2] == pytest.approx(4.0)
    assert all(delay == pytest.approx(8.0) for delay in sleeps[3:5])


async def test_frames_are_queued_and_control_messages_skipped() -> None:
    transport = FakeTransport()
    client, _, _ = make_client([transport])
    await client.start()
    try:
        await until(lambda: client.is_connected)
        transport.feed({"id": 1, "req": "sub", "code": 0})  # ack: no "ch"
        transport.feed({"ch": "tickers::1", "ts": 1766120400000, "sq": 7, "data": {"a": 1}})
        transport.feed_raw("not-json{{")
        await until(lambda: client.queue.qsize() >= 1 and client.malformed_frames >= 1)
        frame = client.queue.get_nowait()
        assert frame.channel == "tickers::1"
        assert frame.sequence == 7
        assert client.queue.qsize() == 0
    finally:
        await client.stop()


async def test_queue_overflow_drops_oldest() -> None:
    transport = FakeTransport()
    client, _, _ = make_client([transport], event_queue_size=2)
    await client.start()
    try:
        await until(lambda: client.is_connected)
        for index in range(3):
            transport.feed({"ch": f"tickers::{index}", "ts": 1766120400000, "data": {}})
        await until(lambda: client.dropped_frames == 1)
        channels = [client.queue.get_nowait().channel for _ in range(client.queue.qsize())]
        assert channels == ["tickers::1", "tickers::2"]  # oldest dropped
    finally:
        await client.stop()


async def test_subscription_limit_is_enforced() -> None:
    transport = FakeTransport()
    client, _, _ = make_client([transport], max_subscriptions=3)
    await client.start()
    try:
        await until(lambda: client.is_connected)
        await client.set_subscriptions({f"tickers::{index}" for index in range(10)})
        assert client.desired_subscription_count == 3
        assert client.active_subscription_count == 3
    finally:
        await client.stop()


async def test_graceful_shutdown_closes_transport() -> None:
    transport = FakeTransport()
    client, _, _ = make_client([transport])
    await client.start()
    await until(lambda: client.is_connected)
    await client.stop()
    assert transport.closed is True
    assert client.state is WsConnectionState.DISCONNECTED
    assert client._run_task is None
