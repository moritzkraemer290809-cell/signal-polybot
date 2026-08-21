"""MarketDataService: routing, cache updates, buffers, resync, universe sync."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import FakeClock

from app.adapters.polymarket_ws import WsFrame
from app.config import DataQualitySettings, PolymarketWsSettings
from app.data.market_data_service import MarketDataService, PersistenceBundle
from app.data.orderbook_service import OrderbookManager
from app.data.persistence_buffer import PersistenceBuffer
from app.domain.enums import Channel, FreshnessStatus, WsConnectionState

FRAMES = json.loads((Path(__file__).parent / "fixtures" / "ws_frames.json").read_text())


class FakeWs:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[WsFrame] = asyncio.Queue()
        self.state = WsConnectionState.CONNECTED
        self.subscription_sets: list[set[str]] = []
        self.resubscribed: list[set[str]] = []

    async def set_subscriptions(self, channels: set[str]) -> None:
        self.subscription_sets.append(set(channels))

    async def resubscribe(self, channels: set[str]) -> None:
        self.resubscribed.append(set(channels))


class FakeCache:
    def __init__(self) -> None:
        self.degraded = False
        self.tickers: dict[int, dict[str, Any]] = {}
        self.bbos: dict[int, dict[str, Any]] = {}
        self.orderbook_status: dict[int, str] = {}
        self.last_updates: list[tuple[str, str, str]] = []
        self.ws_states: list[str] = []
        self.heartbeats: list[str] = []

    async def set_ticker(self, instrument_id, payload):
        self.tickers[instrument_id] = payload

    async def set_bbo(self, instrument_id, payload):
        self.bbos[instrument_id] = payload

    async def set_orderbook_status(self, instrument_id, status):
        self.orderbook_status[instrument_id] = status.value

    async def set_last_update(self, source, channel, symbol, ts=None):
        self.last_updates.append((source.value, channel, symbol))

    async def set_ws_state(self, state):
        self.ws_states.append(state.value)

    async def set_quality_status(self, instrument_id, status):
        pass

    async def heartbeat(self, name, ts=None):
        self.heartbeats.append(name)


def make_bundle(sink: dict[str, list[Any]]) -> PersistenceBundle:
    def collector(name: str):
        async def flush(records: list[Any]) -> None:
            sink.setdefault(name, []).extend(records)

        return flush

    return PersistenceBundle(
        ticks=PersistenceBuffer("ticks", collector("ticks")),
        candles=PersistenceBuffer(
            "candles",
            collector("candles"),
            dedup_key=lambda item: (item[0], item[1].timeframe.value, item[1].open_time),
        ),
        books=PersistenceBuffer("books", collector("books")),
        funding=PersistenceBuffer(
            "funding", collector("funding"), dedup_key=lambda item: (item[0], item[1].ts)
        ),
    )


def frame(kind: str, **overrides: Any) -> WsFrame:
    spec = json.loads(json.dumps(FRAMES[kind]))  # deep copy
    data = spec["data"]
    if isinstance(data, dict):
        data.update(overrides.pop("data_overrides", {}))
    return WsFrame(
        channel=overrides.get("channel", spec["ch"]),
        ts_ms=overrides.get("ts_ms", spec.get("ts")),
        sequence=overrides.get("sequence", spec.get("sq")),
        data=data,
    )


@pytest.fixture
def service_setup():
    ws = FakeWs()
    cache = FakeCache()
    books = OrderbookManager()
    sink: dict[str, list[Any]] = {}
    bundle = make_bundle(sink)
    clock = FakeClock()
    now = {"value": datetime(2026, 8, 21, 12, 0, tzinfo=UTC)}
    service = MarketDataService(
        ws,  # duck-typed fake
        cache,
        books,
        bundle,
        PolymarketWsSettings(_env_file=None),
        DataQualitySettings(_env_file=None),
        None,
        clock=clock,
        now_fn=lambda: now["value"],
    )
    return service, ws, cache, books, sink, clock, now


async def seed_universe(service) -> None:
    await service.sync_universe([(10, 1, "BTC-PERP"), (20, 2, "AAPL-PERP")])


async def test_sync_universe_builds_expected_channels(service_setup) -> None:
    service, ws, *_ = service_setup
    await seed_universe(service)
    channels = ws.subscription_sets[-1]
    for instrument_id in (1, 2):
        assert f"tickers::{instrument_id}" in channels
        assert f"bbo::{instrument_id}" in channels
        assert f"book::{instrument_id}" in channels
        assert f"trades::{instrument_id}" in channels
        for timeframe in ("1m", "5m", "15m", "1h"):
            assert f"klines::{instrument_id}::{timeframe}" in channels
    assert len(channels) == 2 * (4 + 4)


async def test_sync_universe_removes_delisted_instruments(service_setup) -> None:
    service, ws, _, books, *_ = service_setup
    await seed_universe(service)
    books.book(2)  # simulate existing book state
    await service.sync_universe([(10, 1, "BTC-PERP")])
    assert service.tracker_for(2) is None
    assert all("2" not in channel.split("::")[1] for channel in ws.subscription_sets[-1])
    assert books.books_needing_resync() == []


async def test_ticker_frame_updates_cache_and_buffers(service_setup) -> None:
    service, _, cache, *_ = service_setup
    await seed_universe(service)
    await service._handle_frame(frame("ticker"))
    assert cache.tickers[1]["mark_price"] == "65012.50"
    tracker = service.tracker_for(1)
    assert tracker.channel(Channel.TICKER).valid_count == 1
    assert str(tracker.last_mark_price) == "65012.50"
    assert len(service.buffers.ticks) == 1
    assert len(service.buffers.funding) == 1  # first funding value counts as change

    # same funding rate again -> no second funding record
    await service._handle_frame(frame("ticker", data_overrides={"timestamp": 1766120401000}))
    assert len(service.buffers.funding) == 1


async def test_invalid_ticker_is_rejected_and_counted(service_setup) -> None:
    service, _, cache, *_ = service_setup
    await seed_universe(service)
    await service._handle_frame(frame("ticker", data_overrides={"mark_price": "-5"}))
    assert 1 not in cache.tickers
    tracker = service.tracker_for(1)
    assert tracker.channel(Channel.TICKER).invalid_count == 1
    assert service.total_invalid_events() == 1


async def test_out_of_order_ticker_rejected(service_setup) -> None:
    service, *_ = service_setup
    await seed_universe(service)
    await service._handle_frame(frame("ticker"))
    await service._handle_frame(frame("ticker", data_overrides={"timestamp": 1766120300000}))
    tracker = service.tracker_for(1)
    assert tracker.channel(Channel.TICKER).invalid_count == 1
    assert tracker.channel(Channel.TICKER).valid_count == 1


async def test_bbo_frame_updates_cache(service_setup) -> None:
    service, _, cache, *_ = service_setup
    await seed_universe(service)
    await service._handle_frame(frame("bbo"))
    assert cache.bbos[1]["bid_price"] == "65010.00"
    assert ("WEBSOCKET", "bbo", "BTC-PERP") in cache.last_updates


async def test_kline_frame_feeds_candle_buffer(service_setup) -> None:
    service, _, cache, *_ = service_setup
    await seed_universe(service)
    await service._handle_frame(frame("kline"))
    assert len(service.buffers.candles) == 1
    assert ("WEBSOCKET", "klines:1m", "BTC-PERP") in cache.last_updates
    # updated forming candle replaces the old buffer entry (dedup)
    await service._handle_frame(frame("kline"))
    assert len(service.buffers.candles) == 1


async def test_trades_frame_updates_freshness(service_setup) -> None:
    service, _, cache, *_ = service_setup
    await seed_universe(service)
    await service._handle_frame(frame("trade"))
    assert ("WEBSOCKET", "trades", "BTC-PERP") in cache.last_updates


async def test_book_snapshot_then_gap_requests_resync(service_setup) -> None:
    service, ws, cache, books, *_ = service_setup
    await seed_universe(service)
    await service._handle_frame(frame("book_snapshot"))
    assert books.book(1).reliable
    assert cache.orderbook_status[1] == FreshnessStatus.FRESH.value
    assert len(service.buffers.books) == 1  # snapshots are persisted

    # delta with a sequence gap -> resync
    await service._handle_frame(frame("book_delta", data_overrides={"sequence": 1099}))
    assert cache.orderbook_status[1] == FreshnessStatus.RESYNCING.value
    assert {"book::1"} in ws.resubscribed
    assert service.total_resync_requests() == 1
    assert not books.book(1).initialized  # awaiting fresh snapshot

    # resync completes with a new snapshot
    await service._handle_frame(frame("book_snapshot", data_overrides={"sequence": 2000}))
    assert books.book(1).reliable
    assert cache.orderbook_status[1] == FreshnessStatus.FRESH.value


async def test_resync_requests_are_rate_limited(service_setup) -> None:
    service, _, _, _, _, clock, _ = service_setup
    await seed_universe(service)
    await service._handle_frame(frame("book_delta"))  # delta before snapshot
    await service._handle_frame(frame("book_delta"))  # immediately again
    assert service.total_resync_requests() == 1
    clock.advance(10)
    await service._handle_frame(frame("book_delta"))
    assert service.total_resync_requests() == 2


async def test_unknown_instrument_frames_are_counted(service_setup) -> None:
    service, *_ = service_setup
    await seed_universe(service)
    await service._handle_frame(frame("ticker", channel="tickers::999"))
    assert service.unknown_instrument_frames == 1


async def test_handler_errors_do_not_crash_consumer(service_setup) -> None:
    service, ws, *_rest = service_setup
    await seed_universe(service)
    await service.start()
    try:
        ws.queue.put_nowait(WsFrame(channel="tickers::1", ts_ms=None, sequence=None, data=object()))
        ws.queue.put_nowait(frame("ticker"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if service.tracker_for(1).channel(Channel.TICKER).valid_count >= 1:
                break
        assert service.tracker_for(1).channel(Channel.TICKER).valid_count == 1
    finally:
        await service.stop()
