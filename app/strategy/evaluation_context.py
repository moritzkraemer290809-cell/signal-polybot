"""EvaluationContext assembly from live adapters (DB candles, cached market
data, watchlist/session state).  This is the only strategy module that talks
to repositories/services - the core engine stays pure.

No REST calls happen here: candles come from the phase-5 persistence, market
state from in-memory trackers/books.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.config import StrategySettings
from app.data.data_quality import DataQualityService
from app.data.market_data_service import MarketDataService
from app.data.orderbook_service import OrderbookManager
from app.domain.enums import Channel, FreshnessStatus, InstrumentQualityStatus, Timeframe
from app.repositories.candle_repository import CandleRepository
from app.strategy.enums import RejectionCode
from app.strategy.models import (
    Candle,
    CandleSeries,
    CandleSeriesError,
    EvaluationContext,
    MarketContextSnapshot,
)
from app.strategy.version import FEATURE_SCHEMA_VERSION, configuration_hash, ruleset_hash

_TIMEFRAMES = {
    "1m": Timeframe.M1,
    "5m": Timeframe.M5,
    "15m": Timeframe.M15,
    "1h": Timeframe.H1,
}


class ContextBuildError(Exception):
    def __init__(self, code: RejectionCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _WatchlistInfo(Protocol):
    def is_active(self, instrument_pk: int) -> bool: ...

    def quality_score(self, instrument_pk: int) -> int | None: ...

    def session_state(self, asset_class: str) -> tuple[str, bool]: ...


class EvaluationContextBuilder:
    def __init__(
        self,
        settings: StrategySettings,
        candle_repo: CandleRepository,
        market_data: MarketDataService | None,
        data_quality: DataQualityService | None,
        books: OrderbookManager | None,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._settings = settings
        self._candle_repo = candle_repo
        self._market_data = market_data
        self._data_quality = data_quality
        self._books = books
        self._now = now_fn
        self._config_hash = configuration_hash(settings)

    @property
    def config_hash(self) -> str:
        return self._config_hash

    def _min_candles(self, timeframe: Timeframe) -> int:
        return {
            Timeframe.M1: self._settings.min_candles_1m,
            Timeframe.M5: self._settings.min_candles_5m,
            Timeframe.M15: self._settings.min_candles_15m,
            Timeframe.H1: self._settings.min_candles_1h,
        }[timeframe]

    async def _load_series(
        self, instrument_pk: int, timeframe: Timeframe, as_of: datetime
    ) -> CandleSeries:
        window = timedelta(
            milliseconds=timeframe.milliseconds * int(self._min_candles(timeframe) * 3)
        )
        rows = await self._candle_repo.get_range(
            instrument_pk, timeframe, as_of - window, as_of + timedelta(seconds=1)
        )
        candles = [
            Candle(
                open_time=row.open_time
                if row.open_time.tzinfo
                else row.open_time.replace(tzinfo=UTC),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
                trade_count=int(row.trade_count),
                timeframe=timeframe,
            )
            for row in rows
        ]
        return CandleSeries.build(
            timeframe,
            candles,
            as_of,
            min_candles=self._min_candles(timeframe),
            max_gap_multiplier=self._settings.max_candle_gap_multiplier,
        )

    def _market_snapshot(self, instrument_id: int, as_of: datetime) -> MarketContextSnapshot:
        if self._market_data is None or self._data_quality is None or self._books is None:
            return MarketContextSnapshot(
                mark_price=None,
                index_price=None,
                last_price=None,
                mid_price=None,
                best_bid=None,
                best_ask=None,
                spread_bps=None,
                bid_depth_pusd=None,
                ask_depth_pusd=None,
                book_fresh=False,
                bbo_fresh=False,
                funding_rate=None,
                volume_24h_pusd=None,
                snapshot_at=as_of,
            )
        tracker = self._market_data.tracker_for(instrument_id)
        book = self._books.book(instrument_id)
        fresh = (FreshnessStatus.FRESH, FreshnessStatus.AGING)
        book_fresh = (
            book.reliable
            and self._data_quality.channel_freshness(instrument_id, Channel.ORDERBOOK) in fresh
        )
        bbo_fresh = self._data_quality.channel_freshness(instrument_id, Channel.BBO) in fresh
        bbo = tracker.last_bbo if tracker is not None else None
        mid = book.mid_price if book.reliable else None
        bid_depth = ask_depth = None
        if book.reliable and mid is not None:
            from decimal import Decimal

            window_bps = Decimal("25")
            bid_depth = float(book.depth_within_bps("bid", window_bps) * mid)
            ask_depth = float(book.depth_within_bps("ask", window_bps) * mid)
        return MarketContextSnapshot(
            mark_price=(
                float(tracker.last_mark_price)
                if tracker and tracker.last_mark_price is not None
                else None
            ),
            index_price=(
                float(tracker.last_index_price)
                if tracker and tracker.last_index_price is not None
                else None
            ),
            last_price=None,
            mid_price=float(mid) if mid is not None else None,
            best_bid=float(bbo.bid_price) if bbo and bbo_fresh else None,
            best_ask=float(bbo.ask_price) if bbo and bbo_fresh else None,
            spread_bps=(
                float(bbo.spread_bps) if bbo and bbo_fresh and bbo.spread_bps is not None else None
            ),
            bid_depth_pusd=bid_depth,
            ask_depth_pusd=ask_depth,
            book_fresh=book_fresh,
            bbo_fresh=bbo_fresh,
            funding_rate=(
                float(tracker.last_funding_rate)
                if tracker and tracker.last_funding_rate is not None
                else None
            ),
            volume_24h_pusd=(
                float(tracker.last_volume_24h)
                if tracker and tracker.last_volume_24h is not None
                else None
            ),
            snapshot_at=as_of,
        )

    async def build(
        self,
        *,
        instrument_pk: int,
        instrument_id: int,
        symbol: str,
        asset_class: str,
        watchlist_active: bool,
        bot_paused: bool,
        session_state: str,
        session_allowed: bool,
        market_quality_score: int | None,
        low_liquidity: bool,
        as_of: datetime | None = None,
    ) -> EvaluationContext:
        """Assemble the immutable context.  Raises ContextBuildError with a
        rejection code when candle integrity cannot be established."""
        as_of = as_of or self._now()

        data_quality_status = "UNKNOWN"
        data_quality_ok = False
        orderbook_ok = False
        if self._data_quality is not None:
            report = self._data_quality.evaluate(instrument_id)
            data_quality_status = report.status.value
            data_quality_ok = report.status is InstrumentQualityStatus.HEALTHY or (
                report.status is InstrumentQualityStatus.DEGRADED
                and self._settings.allow_degraded_data
            )
            fresh = (FreshnessStatus.FRESH, FreshnessStatus.AGING)
            orderbook_ok = (
                self._books is not None
                and self._books.book(instrument_id).reliable
                and self._data_quality.channel_freshness(instrument_id, Channel.ORDERBOOK) in fresh
            )

        series: dict[str, CandleSeries] = {}
        for timeframe_value in self._settings.required_timeframes:
            timeframe = _TIMEFRAMES[timeframe_value]
            try:
                series[timeframe_value] = await self._load_series(instrument_pk, timeframe, as_of)
            except CandleSeriesError as error:
                if timeframe_value == "1m" and self._settings.allow_1m_optional:
                    continue  # 1m is optional in V1
                raise ContextBuildError(error.code, error.detail) from None

        return EvaluationContext(
            instrument_pk=instrument_pk,
            instrument_id=instrument_id,
            symbol=symbol,
            asset_class=asset_class,
            as_of=as_of,
            series=series,
            market=self._market_snapshot(instrument_id, as_of),
            session_state=session_state,
            session_allowed=session_allowed,
            watchlist_active=watchlist_active,
            bot_paused=bot_paused,
            data_quality_status=data_quality_status,
            data_quality_ok=data_quality_ok,
            orderbook_ok=orderbook_ok,
            market_quality_score=market_quality_score,
            low_liquidity=low_liquidity,
            strategy_name=self._settings.name,
            strategy_version=self._settings.version,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            config_hash=self._config_hash,
            ruleset_hash=ruleset_hash(),
        )


def snapshot_settings(settings: StrategySettings) -> dict[str, Any]:
    return settings.model_dump(mode="json")
