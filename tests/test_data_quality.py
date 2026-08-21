"""DataQualityService: freshness transitions and status classification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.conftest import FakeClock
from tests.test_market_data_service import FakeCache, FakeWs, frame

from app.config import DataFreshnessSettings, DataQualitySettings, PolymarketWsSettings
from app.data.data_quality import DataQualityService
from app.data.market_data_service import MarketDataService
from app.data.orderbook_service import OrderbookManager
from app.domain.enums import (
    Channel,
    FreshnessStatus,
    InstrumentQualityStatus,
    WsConnectionState,
)


@pytest.fixture
def quality_setup():
    ws = FakeWs()
    cache = FakeCache()
    books = OrderbookManager()
    clock = FakeClock()
    now = {"value": datetime(2026, 8, 21, 12, 0, tzinfo=UTC)}
    market_data = MarketDataService(
        ws,
        cache,
        books,
        None,  # buffers optional
        PolymarketWsSettings(_env_file=None),
        DataQualitySettings(_env_file=None),
        None,
        clock=clock,
        now_fn=lambda: now["value"],
    )
    quality = DataQualityService(
        market_data,
        ws,
        books,
        DataFreshnessSettings(_env_file=None),
        DataQualitySettings(_env_file=None),
        cache_degraded=lambda: cache.degraded,
        clock=clock,
        now_fn=lambda: now["value"],
    )
    return market_data, quality, ws, cache, books, clock, now


async def feed_all_channels(market_data) -> None:
    await market_data.sync_universe([(10, 1, "BTC-PERP")])
    await market_data._handle_frame(frame("ticker"))
    await market_data._handle_frame(frame("bbo"))
    await market_data._handle_frame(frame("trade"))
    await market_data._handle_frame(frame("kline"))
    await market_data._handle_frame(frame("book_snapshot"))


async def test_healthy_when_all_channels_fresh(quality_setup) -> None:
    market_data, quality, *_ = quality_setup
    await feed_all_channels(market_data)
    report = quality.evaluate(1)
    assert report.status is InstrumentQualityStatus.HEALTHY
    assert report.channel_freshness[Channel.TICKER] is FreshnessStatus.FRESH
    assert report.reasons == []


async def test_freshness_transitions_fresh_aging_stale(quality_setup) -> None:
    market_data, quality, _, _, _, _, now = quality_setup
    await feed_all_channels(market_data)
    assert quality.channel_freshness(1, Channel.TICKER) is FreshnessStatus.FRESH
    # ticker threshold 15s, aging_fraction 0.5 -> aging above 7.5s
    now["value"] += timedelta(seconds=10)
    assert quality.channel_freshness(1, Channel.TICKER) is FreshnessStatus.AGING
    now["value"] += timedelta(seconds=10)
    assert quality.channel_freshness(1, Channel.TICKER) is FreshnessStatus.STALE


async def test_aging_critical_channel_degrades(quality_setup) -> None:
    market_data, quality, _, _, _, _, now = quality_setup
    await feed_all_channels(market_data)
    now["value"] += timedelta(seconds=9)  # bbo (10s) aging, ticker aging
    report = quality.evaluate(1)
    assert report.status is InstrumentQualityStatus.DEGRADED
    assert any("aging" in reason for reason in report.reasons)


async def test_stale_critical_channel_marks_data_stale(quality_setup) -> None:
    market_data, quality, _, _, _, _, now = quality_setup
    await feed_all_channels(market_data)
    now["value"] += timedelta(seconds=60)
    report = quality.evaluate(1)
    assert report.status is InstrumentQualityStatus.DATA_STALE


async def test_never_seen_instrument_without_connection_is_unavailable(quality_setup) -> None:
    market_data, quality, ws, *_ = quality_setup
    await market_data.sync_universe([(10, 1, "BTC-PERP")])
    ws.state = WsConnectionState.DISCONNECTED
    report = quality.evaluate(1)
    assert report.status is InstrumentQualityStatus.UNAVAILABLE


async def test_invalid_event_burst_marks_data_invalid(quality_setup) -> None:
    market_data, quality, *_ = quality_setup
    await feed_all_channels(market_data)
    for _ in range(DataQualitySettings(_env_file=None).invalid_event_threshold):
        await market_data._handle_frame(frame("ticker", data_overrides={"mark_price": "nan"}))
    report = quality.evaluate(1)
    assert report.status is InstrumentQualityStatus.DATA_INVALID
    assert report.invalid_events_in_window >= 20


async def test_orderbook_resync_status(quality_setup) -> None:
    market_data, quality, *_ = quality_setup
    await feed_all_channels(market_data)
    await market_data._handle_frame(frame("book_delta", data_overrides={"sequence": 9999}))
    report = quality.evaluate(1)
    assert report.status is InstrumentQualityStatus.ORDERBOOK_RESYNCING
    assert report.channel_freshness[Channel.ORDERBOOK] is FreshnessStatus.RESYNCING


async def test_ws_reconnecting_degrades(quality_setup) -> None:
    market_data, quality, ws, *_ = quality_setup
    await feed_all_channels(market_data)
    ws.state = WsConnectionState.RECONNECTING
    report = quality.evaluate(1)
    assert report.status is InstrumentQualityStatus.DEGRADED


async def test_cache_degraded_degrades(quality_setup) -> None:
    market_data, quality, _, cache, *_ = quality_setup
    await feed_all_channels(market_data)
    cache.degraded = True
    report = quality.evaluate(1)
    assert report.status is InstrumentQualityStatus.DEGRADED
    assert any("cache" in reason for reason in report.reasons)


async def test_mark_divergence_degrades(quality_setup) -> None:
    market_data, quality, *_ = quality_setup
    await feed_all_channels(market_data)
    tracker = market_data.tracker_for(1)
    tracker.last_index_price = tracker.last_mark_price * Decimal("1.10")  # 1000bps apart
    report = quality.evaluate(1)
    assert report.status is InstrumentQualityStatus.DEGRADED
    assert any("divergence" in reason for reason in report.reasons)


async def test_summary_counts_stale_assets(quality_setup) -> None:
    market_data, quality, _, _, _, _, now = quality_setup
    await feed_all_channels(market_data)
    summary = quality.summary()
    assert summary["stale_count"] == 0
    assert summary["instruments"]["BTC-PERP"]["status"] == "HEALTHY"
    now["value"] += timedelta(seconds=120)
    summary = quality.summary()
    assert summary["stale_count"] == 1
    assert summary["status_counts"]["DATA_STALE"] == 1


async def test_untracked_instrument_is_unavailable(quality_setup) -> None:
    _, quality, *_ = quality_setup
    assert quality.evaluate(42).status is InstrumentQualityStatus.UNAVAILABLE
