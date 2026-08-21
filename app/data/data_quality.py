"""Central data quality evaluation for the real-time pipeline.

Classifies per-channel freshness and derives one clear status per instrument.

Decision rules (evaluated top-down, first match wins):

1. ``UNAVAILABLE``          - no valid data ever received for the instrument
                              and the WebSocket is not connected, OR every
                              critical channel is UNKNOWN/STALE while the
                              connection is DEGRADED/DISCONNECTED.
2. ``DATA_INVALID``         - invalid events within the configured rolling
                              window reached ``invalid_event_threshold``.
3. ``ORDERBOOK_RESYNCING``  - the in-memory order book is awaiting a snapshot
                              resync (sequence gap / crossed book / delta
                              before snapshot).
4. ``DATA_STALE``           - any critical channel (ticker, BBO, order book)
                              is STALE or has never delivered data.
5. ``DEGRADED``             - any critical channel is AGING, a non-critical
                              channel (trades, candles) is STALE, the
                              connection is RECONNECTING/DEGRADED, persistence
                              buffers or the cache are degraded, or mark/index/
                              mid prices diverge beyond the configured bound.
6. ``HEALTHY``              - everything above passed.

Channel freshness (age of the last VALID event, local receipt time):
``FRESH``  age <= aging_fraction * threshold
``AGING``  age <= threshold
``STALE``  age >  threshold
``INVALID`` when the channel itself accumulated enough invalid events in the
window; ``RESYNCING`` for the order book during resync; ``UNKNOWN`` when no
valid event was ever received.

Phase 5 produces NO trade signals and NO market analysis - consumers in later
phases must block signal generation whenever the status is not HEALTHY (and
may at most watch during DEGRADED, per the master brief).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.adapters.polymarket_ws import PolymarketWsClient
from app.config import DataFreshnessSettings, DataQualitySettings
from app.data.event_validation import deviation_bps
from app.data.market_data_service import MarketDataService
from app.data.orderbook_service import OrderbookManager
from app.domain.enums import (
    Channel,
    FreshnessStatus,
    InstrumentQualityStatus,
    WsConnectionState,
)

CRITICAL_CHANNELS = (Channel.TICKER, Channel.BBO, Channel.ORDERBOOK)
NON_CRITICAL_CHANNELS = (Channel.TRADES, Channel.KLINES)


@dataclass(frozen=True)
class InstrumentQualityReport:
    instrument_id: int
    symbol: str
    status: InstrumentQualityStatus
    channel_freshness: dict[Channel, FreshnessStatus]
    invalid_events_in_window: int
    resync_requests: int
    reasons: list[str] = field(default_factory=list)
    last_event_at: datetime | None = None


class DataQualityService:
    def __init__(
        self,
        market_data: MarketDataService,
        ws_client: PolymarketWsClient,
        books: OrderbookManager,
        freshness: DataFreshnessSettings,
        quality: DataQualitySettings,
        *,
        cache_degraded: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._market_data = market_data
        self._ws = ws_client
        self._books = books
        self._freshness = freshness
        self._quality = quality
        self._cache_degraded = cache_degraded
        self._clock = clock
        self._now = now_fn

    # ------------------------------------------------------------ freshness

    def _threshold(self, channel: Channel) -> float:
        return {
            Channel.TICKER: self._freshness.ticker_seconds,
            Channel.BBO: self._freshness.bbo_seconds,
            Channel.ORDERBOOK: self._freshness.orderbook_seconds,
            Channel.TRADES: self._freshness.trades_seconds,
            Channel.KLINES: self._freshness.candles_seconds,
            # 24h statistics move slowly; reuse the trades threshold
            Channel.STATISTICS: self._freshness.trades_seconds,
        }[channel]

    def channel_freshness(self, instrument_id: int, channel: Channel) -> FreshnessStatus:
        tracker = self._market_data.tracker_for(instrument_id)
        if tracker is None:
            return FreshnessStatus.UNKNOWN
        if channel is Channel.ORDERBOOK:
            book = self._books.book(instrument_id)
            if book.needs_resync:
                return FreshnessStatus.RESYNCING
        health = tracker.channels.get(channel)
        if health is None or health.last_local_at is None:
            return FreshnessStatus.UNKNOWN

        # channel-level invalid burst
        now_mono = self._clock()
        window = self._quality.invalid_event_window_seconds
        times = health.invalid_times
        while times and now_mono - times[0] > window:
            times.popleft()
        if len(times) >= self._quality.invalid_event_threshold:
            return FreshnessStatus.INVALID

        age = (self._now() - health.last_local_at).total_seconds()
        threshold = self._threshold(channel)
        if age <= threshold * self._freshness.aging_fraction:
            return FreshnessStatus.FRESH
        if age <= threshold:
            return FreshnessStatus.AGING
        return FreshnessStatus.STALE

    # ------------------------------------------------------------- evaluate

    def evaluate(self, instrument_id: int) -> InstrumentQualityReport:
        tracker = self._market_data.tracker_for(instrument_id)
        if tracker is None:
            return InstrumentQualityReport(
                instrument_id=instrument_id,
                symbol="?",
                status=InstrumentQualityStatus.UNAVAILABLE,
                channel_freshness={},
                invalid_events_in_window=0,
                resync_requests=0,
                reasons=["instrument not tracked"],
            )

        freshness = {
            channel: self.channel_freshness(instrument_id, channel)
            for channel in (*CRITICAL_CHANNELS, *NON_CRITICAL_CHANNELS)
        }
        invalid_in_window = tracker.invalid_in_window(
            self._clock(), self._quality.invalid_event_window_seconds
        )
        last_event_at = max(
            (
                health.last_local_at
                for health in tracker.channels.values()
                if health.last_local_at is not None
            ),
            default=None,
        )
        reasons: list[str] = []
        status = self._classify(tracker, freshness, invalid_in_window, last_event_at, reasons)
        return InstrumentQualityReport(
            instrument_id=instrument_id,
            symbol=tracker.symbol,
            status=status,
            channel_freshness=freshness,
            invalid_events_in_window=invalid_in_window,
            resync_requests=tracker.resync_requests,
            reasons=reasons,
            last_event_at=last_event_at,
        )

    def _classify(
        self,
        tracker: object,
        freshness: dict[Channel, FreshnessStatus],
        invalid_in_window: int,
        last_event_at: datetime | None,
        reasons: list[str],
    ) -> InstrumentQualityStatus:
        ws_state = self._ws.state
        ws_down = ws_state in (
            WsConnectionState.DISCONNECTED,
            WsConnectionState.CONNECTING,
            WsConnectionState.DEGRADED,
        )
        critical = [freshness[channel] for channel in CRITICAL_CHANNELS]

        # 1. UNAVAILABLE
        if last_event_at is None and ws_state is not WsConnectionState.CONNECTED:
            reasons.append("no data received and websocket not connected")
            return InstrumentQualityStatus.UNAVAILABLE
        if ws_down and all(
            state in (FreshnessStatus.UNKNOWN, FreshnessStatus.STALE) for state in critical
        ):
            reasons.append(f"websocket {ws_state.value} and all critical channels stale/unknown")
            return InstrumentQualityStatus.UNAVAILABLE

        # 2. DATA_INVALID
        if invalid_in_window >= self._quality.invalid_event_threshold:
            reasons.append(
                f"{invalid_in_window} invalid events within "
                f"{self._quality.invalid_event_window_seconds:.0f}s"
            )
            return InstrumentQualityStatus.DATA_INVALID

        # 3. ORDERBOOK_RESYNCING
        if freshness[Channel.ORDERBOOK] is FreshnessStatus.RESYNCING:
            reasons.append("order book awaiting snapshot resync")
            return InstrumentQualityStatus.ORDERBOOK_RESYNCING

        # 4. DATA_STALE
        stale_critical = [
            channel.value
            for channel in CRITICAL_CHANNELS
            if freshness[channel]
            in (FreshnessStatus.STALE, FreshnessStatus.UNKNOWN, FreshnessStatus.INVALID)
        ]
        if stale_critical:
            reasons.append(f"critical channels stale/unknown/invalid: {stale_critical}")
            return InstrumentQualityStatus.DATA_STALE

        # 5. DEGRADED
        degraded = False
        aging_critical = [
            channel.value
            for channel in CRITICAL_CHANNELS
            if freshness[channel] is FreshnessStatus.AGING
        ]
        if aging_critical:
            reasons.append(f"critical channels aging: {aging_critical}")
            degraded = True
        stale_noncritical = [
            channel.value
            for channel in NON_CRITICAL_CHANNELS
            if freshness[channel] is FreshnessStatus.STALE
        ]
        if stale_noncritical:
            reasons.append(f"non-critical channels stale: {stale_noncritical}")
            degraded = True
        if ws_state in (WsConnectionState.RECONNECTING, WsConnectionState.DEGRADED):
            reasons.append(f"websocket {ws_state.value}")
            degraded = True
        buffers = self._market_data.buffers
        if buffers is not None and buffers.degraded:
            reasons.append("persistence buffers degraded (audit data at risk)")
            degraded = True
        if self._cache_degraded():
            reasons.append("market cache degraded")
            degraded = True
        divergence = self._mark_divergence_bps(tracker)
        if divergence is not None and divergence > self._quality.mark_divergence_degraded_bps:
            reasons.append(f"mark/index/mid divergence {divergence:.0f}bps")
            degraded = True
        if degraded:
            return InstrumentQualityStatus.DEGRADED

        return InstrumentQualityStatus.HEALTHY

    def _mark_divergence_bps(self, tracker: object) -> float | None:
        mark = getattr(tracker, "last_mark_price", None)
        index = getattr(tracker, "last_index_price", None)
        instrument_id = getattr(tracker, "instrument_id", None)
        candidates: list[float] = []
        if mark is not None and index is not None and index > 0:
            candidates.append(float(deviation_bps(mark, index)))
        if instrument_id is not None and mark is not None:
            book = self._books.book(int(instrument_id))
            mid = book.mid_price if book.reliable else None
            if mid is not None and mid > 0:
                candidates.append(float(deviation_bps(mark, mid)))
        return max(candidates) if candidates else None

    # -------------------------------------------------------------- summary

    def summary(self) -> dict[str, object]:
        reports = [self.evaluate(tracker.instrument_id) for tracker in self._market_data.trackers()]
        by_status: dict[str, int] = {}
        for report in reports:
            by_status[report.status.value] = by_status.get(report.status.value, 0) + 1
        return {
            "instruments": {
                report.symbol: {
                    "status": report.status.value,
                    "channels": {
                        channel.value: freshness.value
                        for channel, freshness in report.channel_freshness.items()
                    },
                    "invalid_events_in_window": report.invalid_events_in_window,
                    "resync_requests": report.resync_requests,
                    "last_event_at": (
                        report.last_event_at.isoformat() if report.last_event_at else None
                    ),
                    "reasons": report.reasons,
                }
                for report in reports
            },
            "status_counts": by_status,
            "stale_count": sum(
                1
                for report in reports
                if report.status
                in (
                    InstrumentQualityStatus.DATA_STALE,
                    InstrumentQualityStatus.DATA_INVALID,
                    InstrumentQualityStatus.UNAVAILABLE,
                )
            ),
        }
