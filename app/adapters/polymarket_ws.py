"""Central async WebSocket client for public Polymarket Perps market data.

One managed connection serves the whole process - no connection per asset,
repository or strategy task.  The client only ever subscribes to public
channels (``tickers``, ``bbo``, ``book``, ``trades``, ``klines``); it holds no
credentials and never sends anything but subscribe/unsubscribe requests.

Features:
- connection state machine (DISCONNECTED/CONNECTING/CONNECTED/RECONNECTING/
  DEGRADED/STOPPING)
- exponential backoff with jitter, bounded attempts per rolling window
  (beyond the bound the state turns DEGRADED and retries continue at the
  maximum backoff - never a busy loop, never a silent give-up)
- heartbeat via websocket ping/pong (library-level, configurable)
- desired-subscription set with idempotent diffing and automatic
  re-subscription after every reconnect
- bounded event queue with drop-oldest backpressure
- graceful shutdown

The client never logs full payloads - only channel names, sizes and counters.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.config import PolymarketWsSettings
from app.domain.enums import WsConnectionState
from app.observability.logging import get_logger
from app.observability.metrics import metrics

_SUB_CHUNK_SIZE = 20


class WsTransport(Protocol):
    """Minimal transport interface (satisfied by ``websockets`` connections)."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


ConnectFactory = Callable[[], Awaitable[WsTransport]]
StateListener = Callable[[WsConnectionState], Awaitable[None]]


@dataclass(frozen=True)
class WsFrame:
    """A single validated-shape data frame from the feed (not yet validated
    for content - content validation happens in the market data service)."""

    channel: str
    ts_ms: int | None
    sequence: int | None
    data: Any


class PolymarketWsClient:
    def __init__(
        self,
        settings: PolymarketWsSettings,
        *,
        connect: ConnectFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        on_state_change: StateListener | None = None,
    ) -> None:
        self._settings = settings
        self._connect = connect or self._default_connect
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._on_state_change = on_state_change
        self._log = get_logger("polymarket_ws")

        self.state: WsConnectionState = WsConnectionState.DISCONNECTED
        self.queue: asyncio.Queue[WsFrame] = asyncio.Queue(maxsize=settings.event_queue_size)

        self._desired: set[str] = set()
        self._active: set[str] = set()
        self._transport: WsTransport | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._request_id = 0
        self._failure_times: deque[float] = deque()

        # observable counters
        self.reconnect_count = 0
        self.messages_received = 0
        self.dropped_frames = 0
        self.malformed_frames = 0
        self.last_message_at: datetime | None = None
        self.connected_since: datetime | None = None

    def set_state_listener(self, listener: StateListener | None) -> None:
        """Attach/replace the state-change listener (wired at bootstrap time)."""
        self._on_state_change = listener

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._run_task is not None:
            return
        self._run_task = asyncio.create_task(self._run(), name="polymarket_ws")

    async def stop(self) -> None:
        await self._set_state(WsConnectionState.STOPPING)
        if self._run_task is not None:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
            self._run_task = None
        await self._close_transport()
        await self._set_state(WsConnectionState.DISCONNECTED)

    @property
    def is_connected(self) -> bool:
        return self.state is WsConnectionState.CONNECTED

    @property
    def active_subscription_count(self) -> int:
        return len(self._active)

    @property
    def desired_subscription_count(self) -> int:
        return len(self._desired)

    # -------------------------------------------------------- subscriptions

    async def set_subscriptions(self, channels: set[str]) -> None:
        """Idempotently reconcile the desired subscription set.

        When connected, only the delta is sent; the same set twice sends
        nothing.  Beyond ``max_subscriptions`` the set is truncated
        deterministically with a warning.
        """
        if len(channels) > self._settings.max_subscriptions:
            truncated = set(sorted(channels)[: self._settings.max_subscriptions])
            self._log.warning(
                "subscription_limit_truncated",
                requested=len(channels),
                limit=self._settings.max_subscriptions,
            )
            metrics.increment("ws.subscriptions_truncated")
            channels = truncated

        to_add = channels - self._desired
        to_remove = self._desired - channels
        self._desired = set(channels)
        if not self.is_connected:
            return
        if to_add:
            await self._send_subscribe(sorted(to_add))
        if to_remove:
            await self._send_unsubscribe(sorted(to_remove & self._active))

    async def resubscribe(self, channels: set[str]) -> None:
        """Force re-subscription of channels (e.g. order book resync)."""
        present = channels & self._desired
        if not present or not self.is_connected:
            return
        await self._send_unsubscribe(sorted(present & self._active))
        await self._send_subscribe(sorted(present))

    async def _send_subscribe(self, channels: list[str]) -> None:
        for start in range(0, len(channels), _SUB_CHUNK_SIZE):
            chunk = channels[start : start + _SUB_CHUNK_SIZE]
            await self._send_request("sub", chunk)
            self._active.update(chunk)
        metrics.set_gauge("ws.active_subscriptions", float(len(self._active)))

    async def _send_unsubscribe(self, channels: list[str]) -> None:
        for start in range(0, len(channels), _SUB_CHUNK_SIZE):
            chunk = channels[start : start + _SUB_CHUNK_SIZE]
            await self._send_request("unsub", chunk)
            self._active.difference_update(chunk)
        metrics.set_gauge("ws.active_subscriptions", float(len(self._active)))

    async def _send_request(self, request: str, channels: list[str]) -> None:
        if self._transport is None:
            return
        self._request_id += 1
        message = json.dumps({"id": self._request_id, "req": request, "chs": channels})
        try:
            await self._transport.send(message)
        except Exception as exc:
            self._log.warning("ws_send_failed", request=request, error=type(exc).__name__)

    # ------------------------------------------------------------ main loop

    async def _run(self) -> None:
        while True:
            target = (
                WsConnectionState.CONNECTING
                if self.reconnect_count == 0
                else WsConnectionState.RECONNECTING
            )
            if self.state is not WsConnectionState.DEGRADED:
                await self._set_state(target)
            try:
                transport = await asyncio.wait_for(
                    self._connect(), timeout=self._settings.connect_timeout_seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning("ws_connect_failed", error=type(exc).__name__)
                await self._register_failure()
                continue

            self._transport = transport
            self._active = set()
            self.connected_since = datetime.now(tz=UTC)
            await self._set_state(WsConnectionState.CONNECTED)
            self._log.info("ws_connected", desired_subscriptions=len(self._desired))
            if self._desired:
                await self._send_subscribe(sorted(self._desired))

            try:
                await self._read_loop(transport)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # heartbeat timeout / connection closed / transport error
                self._log.warning("ws_disconnected", error=type(exc).__name__)
            finally:
                await self._close_transport()
                self.connected_since = None
            self.reconnect_count += 1
            metrics.increment("ws.reconnects")
            await self._register_failure()

    async def _read_loop(self, transport: WsTransport) -> None:
        while True:
            raw = await transport.recv()
            self.messages_received += 1
            self.last_message_at = datetime.now(tz=UTC)
            metrics.increment("ws.messages_received")
            frame = self._parse_frame(raw)
            if frame is None:
                continue
            self._enqueue(frame)

    def _parse_frame(self, raw: str | bytes) -> WsFrame | None:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            self.malformed_frames += 1
            metrics.increment("ws.malformed_frames")
            return None
        if not isinstance(payload, dict):
            self.malformed_frames += 1
            metrics.increment("ws.malformed_frames")
            return None
        channel = payload.get("ch")
        if not isinstance(channel, str):
            # control frame (sub/unsub ack, pong, info) - not market data
            return None
        ts = payload.get("ts")
        sequence = payload.get("sq")
        return WsFrame(
            channel=channel,
            ts_ms=int(ts) if isinstance(ts, (int, float)) else None,
            sequence=int(sequence) if isinstance(sequence, (int, float)) else None,
            data=payload.get("data"),
        )

    def _enqueue(self, frame: WsFrame) -> None:
        if self.queue.full():
            # drop-oldest backpressure: real-time data beats stale data
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped_frames += 1
            metrics.increment("ws.dropped_frames")
        self.queue.put_nowait(frame)

    # ------------------------------------------------------------- backoff

    async def _register_failure(self) -> None:
        now = self._clock()
        window = self._settings.reconnect_window_seconds
        self._failure_times.append(now)
        while self._failure_times and now - self._failure_times[0] > window:
            self._failure_times.popleft()

        attempts_in_window = len(self._failure_times)
        if attempts_in_window > self._settings.max_reconnect_attempts:
            if self.state is not WsConnectionState.DEGRADED:
                self._log.error(
                    "ws_degraded",
                    attempts_in_window=attempts_in_window,
                    window_seconds=window,
                )
            await self._set_state(WsConnectionState.DEGRADED)
            delay = self._settings.reconnect_max_seconds
        else:
            exponent = max(0, attempts_in_window - 1)
            delay = min(
                self._settings.reconnect_min_seconds * (2**exponent),
                self._settings.reconnect_max_seconds,
            )
        delay += self._settings.reconnect_jitter_seconds * self._rng()
        await self._sleep(delay)

    # -------------------------------------------------------------- helpers

    async def _set_state(self, state: WsConnectionState) -> None:
        if state is self.state:
            return
        self.state = state
        metrics.set_gauge("ws.state_" + state.value.lower(), 1.0)
        if self._on_state_change is not None:
            try:
                await self._on_state_change(state)
            except Exception as exc:
                self._log.warning("ws_state_listener_failed", error=type(exc).__name__)

    async def _close_transport(self) -> None:
        if self._transport is None:
            return
        transport, self._transport = self._transport, None
        self._active = set()
        metrics.set_gauge("ws.active_subscriptions", 0.0)
        with contextlib.suppress(Exception):
            await transport.close()

    async def _default_connect(self) -> WsTransport:
        import websockets

        return await websockets.connect(
            self._settings.url,
            ping_interval=self._settings.ping_interval_seconds,
            ping_timeout=self._settings.ping_timeout_seconds,
            open_timeout=self._settings.connect_timeout_seconds,
        )
