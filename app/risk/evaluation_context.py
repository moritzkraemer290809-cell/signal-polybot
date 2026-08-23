"""RiskEvaluationContext assembly from live adapters.

The only risk module that talks to repositories/services.  It builds
immutable snapshots from phase-4 instrument metadata, phase-5 public market
data, phase-7 selection state and phase-8 candidates - no REST calls, no
account data, no Telegram.  Incomplete or stale data raises a structured
:class:`ContextBuildError` instead of producing an optimistic context.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from app.config import CostSettings, RiskSettings
from app.costs.execution_assumptions import build_execution_assumptions
from app.costs.models import (
    DepthLevel,
    FeeScheduleSnapshot,
    FundingSnapshot,
    OrderbookDepthSnapshot,
)
from app.costs.version import cost_configuration_hash
from app.data.data_quality import DataQualityService
from app.data.market_data_service import MarketDataService
from app.data.orderbook_service import OrderbookManager
from app.domain.enums import Channel, FreshnessStatus, InstrumentQualityStatus, Timeframe
from app.repositories.candle_repository import CandleRepository
from app.repositories.feature_repository import FeatureRepository
from app.repositories.funding_repository import FundingRateRepository
from app.repositories.orm import Instrument, SetupCandidateRecord
from app.risk.enums import PlanRejectionCode
from app.risk.models import (
    CandidateSnapshot,
    InstrumentRiskSnapshot,
    RiskEvaluationContext,
    TechnicalLevel,
)
from app.risk.version import INSTRUMENT_RISK_SNAPSHOT_VERSION, risk_configuration_hash
from app.strategy.candle_features import atr as candle_atr
from app.strategy.models import CandleSeries, CandleSeriesError

_FRESH = (FreshnessStatus.FRESH, FreshnessStatus.AGING)
_ATR_PERIOD = 14
_ATR_MIN_CANDLES = _ATR_PERIOD + 1


class ContextBuildError(Exception):
    def __init__(self, code: PlanRejectionCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _funding_interval_hours(raw: str | None) -> float | None:
    if not raw:
        return None
    text = str(raw).strip().lower()
    try:
        if text.endswith("h"):
            return float(text[:-1])
        if text.endswith("m"):
            return float(text[:-1]) / 60.0
        return float(text)
    except ValueError:
        return None


def candidate_snapshot_from_record(row: SetupCandidateRecord) -> CandidateSnapshot:
    details = row.details or {}
    as_of = row.as_of if row.as_of.tzinfo else row.as_of.replace(tzinfo=UTC)
    expiry = row.expiry_at if row.expiry_at.tzinfo else row.expiry_at.replace(tzinfo=UTC)
    return CandidateSnapshot(
        candidate_id=row.id,
        candidate_type=row.candidate_type,
        direction=row.direction,
        state=row.state,
        instrument_pk=row.instrument_pk,
        instrument_id=row.instrument_id,
        symbol=row.symbol,
        asset_class=row.asset_class,
        as_of=as_of,
        expiry_at=expiry,
        setup_score=row.setup_score,
        primary_regime=row.primary_regime,
        referenced_levels=tuple(details.get("referenced_levels") or ()),
        invalidation_conditions=tuple(details.get("invalidation_conditions") or ()),
        strategy_name=row.strategy_name,
        strategy_version=row.strategy_version,
        strategy_config_hash=row.config_hash,
        features=dict(details.get("features") or {}),
    )


class RiskPlanEvaluationContextBuilder:
    def __init__(
        self,
        risk_settings: RiskSettings,
        cost_settings: CostSettings,
        candle_repo: CandleRepository,
        feature_repo: FeatureRepository,
        funding_repo: FundingRateRepository,
        market_data: MarketDataService | None,
        data_quality: DataQualityService | None,
        books: OrderbookManager | None,
    ) -> None:
        self._risk = risk_settings
        self._costs = cost_settings
        self._candles = candle_repo
        self._features = feature_repo
        self._funding = funding_repo
        self._market_data = market_data
        self._data_quality = data_quality
        self._books = books
        self.risk_config_hash = risk_configuration_hash(risk_settings)
        self.cost_config_hash = cost_configuration_hash(cost_settings)
        self._assumptions = build_execution_assumptions(cost_settings)

    # ------------------------------------------------------------- snapshots

    def _depth_snapshot(self, instrument_id: int) -> OrderbookDepthSnapshot:
        if self._books is None or self._data_quality is None:
            return OrderbookDepthSnapshot(
                instrument_id=instrument_id, bids=(), asks=(), fresh=False, snapshot_at=None
            )
        book = self._books.book(instrument_id)
        fresh = (
            book.reliable
            and self._data_quality.channel_freshness(instrument_id, Channel.ORDERBOOK) in _FRESH
        )
        max_levels = self._costs.orderbook_max_levels
        bids = tuple(
            DepthLevel(price=float(level.price), quantity=float(level.quantity))
            for level in book.levels("bid", max_levels)
        )
        asks = tuple(
            DepthLevel(price=float(level.price), quantity=float(level.quantity))
            for level in book.levels("ask", max_levels)
        )
        return OrderbookDepthSnapshot(
            instrument_id=instrument_id,
            bids=bids,
            asks=asks,
            fresh=fresh,
            snapshot_at=book.last_reliable_update,
        )

    async def _atr(self, instrument_pk: int, timeframe: Timeframe, as_of: datetime) -> float | None:
        window = timedelta(milliseconds=timeframe.milliseconds * _ATR_MIN_CANDLES * 3)
        rows = await self._candles.get_range(
            instrument_pk, timeframe, as_of - window, as_of + timedelta(seconds=1)
        )
        from app.strategy.models import Candle as StrategyCandle

        candles = [
            StrategyCandle(
                open_time=(
                    row.open_time if row.open_time.tzinfo else row.open_time.replace(tzinfo=UTC)
                ),
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
        try:
            series = CandleSeries.build(
                timeframe,
                candles,
                as_of,
                min_candles=_ATR_MIN_CANDLES,
                max_gap_multiplier=2.0,
            )
        except CandleSeriesError:
            return None
        return candle_atr(series, _ATR_PERIOD)

    def _instrument_snapshot(
        self,
        instrument: Instrument,
        instrument_id: int,
        as_of: datetime,
        book: OrderbookDepthSnapshot,
    ) -> InstrumentRiskSnapshot:
        tracker = (
            self._market_data.tracker_for(instrument_id) if self._market_data is not None else None
        )
        data_quality_status = "UNKNOWN"
        data_quality_ok = False
        if self._data_quality is not None:
            report = self._data_quality.evaluate(instrument_id)
            data_quality_status = report.status.value
            data_quality_ok = report.status is InstrumentQualityStatus.HEALTHY or (
                report.status is InstrumentQualityStatus.DEGRADED and self._risk.allow_degraded_data
            )
        raw = instrument.metadata_raw or {}
        mid = None
        if book.best_bid is not None and book.best_ask is not None:
            mid = (book.best_bid + book.best_ask) / 2
        return InstrumentRiskSnapshot(
            instrument_pk=instrument.id,
            instrument_id=instrument_id,
            symbol=instrument.symbol,
            status=instrument.status,
            asset_class=instrument.asset_class,
            mark_price=(
                float(tracker.last_mark_price)
                if tracker is not None and tracker.last_mark_price is not None
                else None
            ),
            index_price=(
                float(tracker.last_index_price)
                if tracker is not None and tracker.last_index_price is not None
                else None
            ),
            mid_price=mid,
            last_price=None,
            max_leverage=int(instrument.max_leverage) if instrument.max_leverage else None,
            min_notional=(
                float(instrument.min_notional) if instrument.min_notional is not None else None
            ),
            tick_size=_float(raw.get("tick_size")),
            price_decimals=int(instrument.price_decimals),
            quantity_decimals=int(instrument.quantity_decimals),
            risk_tiers=tuple(cast("list[dict[str, Any]]", instrument.risk_tiers or [])),
            maintenance_margin_rate=_float(raw.get("maintenance_margin_rate")),
            initial_margin_rate=_float(raw.get("initial_margin_rate")),
            liquidation_fee_rate=(
                float(instrument.liquidation_fee)
                if instrument.liquidation_fee is not None
                else None
            ),
            isolated_only=bool(instrument.isolated_only),
            funding_interval_hours=_funding_interval_hours(instrument.funding_interval),
            instrument_updated_at=instrument.last_seen_at,
            data_quality_status=data_quality_status,
            data_quality_ok=data_quality_ok,
            orderbook_fresh=book.fresh,
            source="phase4_discovery+phase5_public_market_data",
            snapshot_version=INSTRUMENT_RISK_SNAPSHOT_VERSION,
            as_of=as_of,
        )

    async def _levels(self, instrument_pk: int) -> tuple[TechnicalLevel, ...]:
        levels: list[TechnicalLevel] = []
        seen: set[tuple[str, float]] = set()
        for timeframe in ("15m", "1h"):
            for swing in await self._features.recent_swings(instrument_pk, timeframe):
                kind = f"SWING_{swing.swing_type}"
                price = float(swing.price)
                if (kind, price) in seen:
                    continue
                seen.add((kind, price))
                levels.append(
                    TechnicalLevel(
                        price=price,
                        kind=kind,
                        timeframe=timeframe,
                        relevance=float(swing.relevance or 0.5),
                        source_id=f"swing:{swing.id}",
                    )
                )
        for level in await self._features.recent_liquidity_levels(instrument_pk, "15m"):
            price = float(level.price)
            if (level.level_type, price) in seen:
                continue
            seen.add((level.level_type, price))
            levels.append(
                TechnicalLevel(
                    price=price,
                    kind=level.level_type,
                    timeframe="15m",
                    relevance=float(level.relevance or 0.5),
                    source_id=f"liquidity:{level.id}",
                )
            )
        return tuple(levels)

    async def _funding_snapshot(
        self, instrument_pk: int, instrument_id: int, interval_hours: float | None, as_of: datetime
    ) -> FundingSnapshot:
        tracker = (
            self._market_data.tracker_for(instrument_id) if self._market_data is not None else None
        )
        effective_interval = interval_hours or self._costs.default_funding_interval_hours
        # a funding value counts as CURRENT only when it carries a timestamp
        # inside its own interval window - a rate of unknown age is not live
        max_age = timedelta(hours=max(2.0 * effective_interval, 2.0))
        current: float | None = None
        current_fresh = False
        if (
            tracker is not None
            and tracker.last_funding_rate is not None
            and tracker.last_funding_rate_at is not None
        ):
            rate_at = tracker.last_funding_rate_at
            if rate_at.tzinfo is None:
                rate_at = rate_at.replace(tzinfo=UTC)
            if as_of - rate_at <= max_age:
                current = float(tracker.last_funding_rate)
                current_fresh = True
        next_at = tracker.last_next_funding_at if tracker is not None else None
        since = as_of - timedelta(hours=self._costs.funding_lookback_hours)
        history = await self._funding.recent_rates(instrument_pk, since)
        normalized = tuple(
            (ts if ts.tzinfo else ts.replace(tzinfo=UTC), rate) for ts, rate in history
        )
        history_fresh = bool(normalized) and (as_of - normalized[-1][0]) <= max_age
        return FundingSnapshot(
            current_rate=current,
            history=normalized,
            next_funding_at=next_at,
            interval_hours=effective_interval,
            data_fresh=current_fresh or history_fresh,
        )

    # ------------------------------------------------------------------ build

    async def build(
        self,
        *,
        candidate: CandidateSnapshot,
        instrument: Instrument,
        watchlist_active: bool,
        bot_paused: bool,
        session_state: str,
        session_allowed: bool,
        equity_session_remaining_minutes: float | None,
        fee_schedule: FeeScheduleSnapshot | None,
        as_of: datetime | None = None,
    ) -> RiskEvaluationContext:
        as_of = as_of or datetime.now(tz=UTC)
        instrument_id = candidate.instrument_id
        book = self._depth_snapshot(instrument_id)
        snapshot = self._instrument_snapshot(instrument, instrument_id, as_of, book)

        tracker = (
            self._market_data.tracker_for(instrument_id) if self._market_data is not None else None
        )
        bbo = tracker.last_bbo if tracker is not None else None
        bbo_fresh = (
            self._data_quality is not None
            and self._data_quality.channel_freshness(instrument_id, Channel.BBO) in _FRESH
            and bbo is not None
        )
        bbo_at = None
        if bbo is not None:
            bbo_at = datetime.fromtimestamp(bbo.ts_ms / 1000, tz=UTC)

        atr_5m = await self._atr(candidate.instrument_pk, Timeframe.M5, as_of)
        atr_15m = await self._atr(candidate.instrument_pk, Timeframe.M15, as_of)
        levels = await self._levels(candidate.instrument_pk)
        funding = await self._funding_snapshot(
            candidate.instrument_pk, instrument_id, snapshot.funding_interval_hours, as_of
        )

        equity_overnight_risk = False
        if candidate.asset_class == "EQUITY":
            hold = float(self._costs.reference_hold_minutes)
            equity_overnight_risk = (
                equity_session_remaining_minutes is None or hold > equity_session_remaining_minutes
            )

        return RiskEvaluationContext(
            candidate=candidate,
            instrument=snapshot,
            book=book,
            best_bid=float(bbo.bid_price) if bbo is not None else None,
            best_ask=float(bbo.ask_price) if bbo is not None else None,
            bbo_at=bbo_at,
            bbo_fresh=bbo_fresh,
            funding=funding,
            fee_schedule=fee_schedule,
            execution_assumptions=self._assumptions,
            levels=levels,
            atr_5m=atr_5m,
            atr_15m=atr_15m,
            session_state=session_state,
            session_allowed=session_allowed,
            equity_overnight_risk=equity_overnight_risk,
            watchlist_active=watchlist_active,
            bot_paused=bot_paused,
            as_of=as_of,
            risk_model_name=self._risk.model_name,
            risk_model_version=self._risk.model_version,
            risk_config_hash=self.risk_config_hash,
            cost_model_name=self._costs.model_name,
            cost_model_version=self._costs.model_version,
            cost_config_hash=self.cost_config_hash,
            instrument_snapshot_version=INSTRUMENT_RISK_SNAPSHOT_VERSION,
        )
