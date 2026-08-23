"""Pure replay state and per-stage context assembly for backtests.

The replay state accumulates ONLY what the clock has already revealed:
closed candles, the latest observed BBO/book/funding/session state.  Every
context handed to a phase-8/9/10 core is built from that state, so the
domain rules see exactly what they would have seen live - no future data,
no idealised prices, no rule changes.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import CostSettings, RiskSettings, StrategySettings
from app.costs.execution_assumptions import build_execution_assumptions
from app.costs.models import (
    DepthLevel,
    FeeScheduleSnapshot,
    FundingSnapshot,
    OrderbookDepthSnapshot,
)
from app.costs.version import cost_configuration_hash
from app.domain.enums import Timeframe
from app.risk.models import (
    CandidateSnapshot,
    InstrumentRiskSnapshot,
    RiskEvaluationContext,
    TechnicalLevel,
)
from app.risk.version import INSTRUMENT_RISK_SNAPSHOT_VERSION, risk_configuration_hash
from app.simulation.models import MarketReferenceSnapshot
from app.simulation.replay_clock import ReplayClock
from app.strategy.candle_features import atr as candle_atr
from app.strategy.models import (
    Candle,
    CandleSeries,
    CandleSeriesError,
    EvaluationContext,
    MarketContextSnapshot,
)
from app.strategy.version import FEATURE_SCHEMA_VERSION, configuration_hash, ruleset_hash

_ATR_PERIOD = 14
_MAX_BUFFER = 400


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class SymbolReplayState:
    """Everything observed so far for ONE symbol (never anything later)."""

    symbol: str
    instrument_pk: int = 1
    instrument_id: int = 1
    asset_class: str = "CRYPTO"
    candles: dict[str, list[Candle]] = field(default_factory=lambda: defaultdict(list))
    best_bid: float | None = None
    best_ask: float | None = None
    mark_price: float | None = None
    bbo_at: datetime | None = None
    book_bids: tuple[DepthLevel, ...] = ()
    book_asks: tuple[DepthLevel, ...] = ()
    book_at: datetime | None = None
    book_fresh: bool = False
    funding_rate: float | None = None
    funding_history: list[tuple[datetime, float]] = field(default_factory=list)
    next_funding_at: datetime | None = None
    funding_interval_hours: float = 8.0
    session_state: str = "CRYPTO_24_7"
    session_allowed: bool = True
    watchlist_active: bool = True
    data_quality_status: str = "HEALTHY"
    data_quality_ok: bool = True
    market_quality_score: int = 90
    instrument_status: str = "ACTIVE"
    max_leverage: int | None = 10
    min_notional: float | None = 10.0
    tick_size: float | None = 0.01
    price_decimals: int = 2
    quantity_decimals: int = 4
    maintenance_margin_rate: float | None = 0.01
    initial_margin_rate: float | None = 0.05
    liquidation_fee_rate: float | None = 0.002
    levels: list[TechnicalLevel] = field(default_factory=list)

    def append_candle(self, timeframe: str, candle: Candle) -> None:
        bucket = self.candles[timeframe]
        bucket.append(candle)
        if len(bucket) > _MAX_BUFFER:
            del bucket[: len(bucket) - _MAX_BUFFER]

    def book_snapshot(self) -> OrderbookDepthSnapshot:
        return OrderbookDepthSnapshot(
            instrument_id=self.instrument_id,
            bids=self.book_bids,
            asks=self.book_asks,
            fresh=self.book_fresh,
            snapshot_at=self.book_at,
        )

    def funding_snapshot(self, now: datetime) -> FundingSnapshot:
        return FundingSnapshot(
            current_rate=self.funding_rate,
            history=tuple(self.funding_history[-64:]),
            next_funding_at=self.next_funding_at,
            interval_hours=self.funding_interval_hours,
            data_fresh=self.funding_rate is not None or bool(self.funding_history),
        )


class ReplayState:
    """Mutable-but-causal view of the replayed world (pure, no I/O)."""

    def __init__(self, clock: ReplayClock) -> None:
        self._clock = clock
        self._symbols: dict[str, SymbolReplayState] = {}

    @property
    def symbols(self) -> dict[str, SymbolReplayState]:
        return self._symbols

    def state_for(self, symbol: str) -> SymbolReplayState:
        if symbol not in self._symbols:
            self._symbols[symbol] = SymbolReplayState(symbol=symbol)
        return self._symbols[symbol]

    # ------------------------------------------------------------- ingestion

    def apply(self, category: str, symbol: str, as_of: datetime, payload: dict[str, Any]) -> None:
        """Fold one replayed input event into the observable state."""
        self._clock.guard(as_of, f"{category} event")
        state = self.state_for(symbol)
        if category == "INSTRUMENT_STATUS":
            state.instrument_status = str(payload.get("status", state.instrument_status))
            state.instrument_pk = int(payload.get("instrument_pk", state.instrument_pk))
            state.instrument_id = int(payload.get("instrument_id", state.instrument_id))
            state.asset_class = str(payload.get("asset_class", state.asset_class))
        elif category == "SESSION_CALENDAR":
            state.session_state = str(payload.get("session_state", state.session_state))
            state.session_allowed = bool(payload.get("session_allowed", state.session_allowed))
            state.watchlist_active = bool(payload.get("watchlist_active", state.watchlist_active))
        elif category == "DATA_QUALITY_BOOK":
            if "bids" in payload or "asks" in payload:
                state.book_bids = tuple(
                    DepthLevel(price=float(level[0]), quantity=float(level[1]))
                    for level in payload.get("bids", ())
                )
                state.book_asks = tuple(
                    DepthLevel(price=float(level[0]), quantity=float(level[1]))
                    for level in payload.get("asks", ())
                )
                state.book_at = as_of
                state.book_fresh = bool(payload.get("fresh", True))
            if "data_quality_status" in payload:
                state.data_quality_status = str(payload["data_quality_status"])
                state.data_quality_ok = state.data_quality_status == "HEALTHY"
            if "market_quality_score" in payload:
                state.market_quality_score = int(payload["market_quality_score"])
        elif category == "TICKER_BBO_TRADES":
            kind = str(payload.get("kind", "bbo")).lower()
            if kind == "funding":
                rate = float(payload["funding_rate"])
                state.funding_rate = rate
                state.funding_history.append((as_of, rate))
                if payload.get("next_funding_at"):
                    state.next_funding_at = _utc(
                        datetime.fromisoformat(str(payload["next_funding_at"]))
                    )
                if payload.get("interval_hours"):
                    state.funding_interval_hours = float(payload["interval_hours"])
            else:
                if payload.get("best_bid") is not None:
                    state.best_bid = float(payload["best_bid"])
                if payload.get("best_ask") is not None:
                    state.best_ask = float(payload["best_ask"])
                if payload.get("mark_price") is not None:
                    state.mark_price = float(payload["mark_price"])
                state.bbo_at = as_of
        elif category == "CLOSED_CANDLE":
            timeframe = str(payload.get("timeframe", "5m"))
            candle = Candle(
                open_time=_utc(datetime.fromisoformat(str(payload["open_time"]))),
                open=float(payload["open"]),
                high=float(payload["high"]),
                low=float(payload["low"]),
                close=float(payload["close"]),
                volume=float(payload.get("volume", 0.0)),
                trade_count=int(payload.get("trade_count", 0)),
                timeframe=Timeframe(timeframe),
            )
            # a candle only becomes observable AFTER it closed
            close_time = candle.open_time + timedelta(
                milliseconds=Timeframe(timeframe).milliseconds
            )
            self._clock.guard(close_time, f"{timeframe} candle close")
            state.append_candle(timeframe, candle)
            if state.mark_price is None:
                state.mark_price = candle.close

    # --------------------------------------------------------------- contexts

    def strategy_context(self, symbol: str, settings: StrategySettings) -> EvaluationContext | None:
        """Phase-8 evaluation context from CLOSED candles only."""
        state = self.state_for(symbol)
        now = self._clock.now()
        minimums = {
            "1m": settings.min_candles_1m,
            "5m": settings.min_candles_5m,
            "15m": settings.min_candles_15m,
            "1h": settings.min_candles_1h,
        }
        series: dict[str, CandleSeries] = {}
        for timeframe in ("5m", "15m", "1h"):
            candles = state.candles.get(timeframe) or []
            if len(candles) < minimums[timeframe]:
                return None
            try:
                series[timeframe] = CandleSeries.build(
                    Timeframe(timeframe),
                    list(candles),
                    now,
                    min_candles=minimums[timeframe],
                    max_gap_multiplier=settings.max_candle_gap_multiplier,
                )
            except CandleSeriesError:
                return None
        price = series["5m"].last.close
        market = MarketContextSnapshot(
            mark_price=state.mark_price or price,
            index_price=state.mark_price or price,
            last_price=None,
            mid_price=(
                (state.best_bid + state.best_ask) / 2
                if state.best_bid is not None and state.best_ask is not None
                else None
            ),
            best_bid=state.best_bid,
            best_ask=state.best_ask,
            spread_bps=(
                (state.best_ask - state.best_bid) / state.best_bid * 10_000
                if state.best_bid and state.best_ask
                else None
            ),
            bid_depth_pusd=sum(level.price * level.quantity for level in state.book_bids),
            ask_depth_pusd=sum(level.price * level.quantity for level in state.book_asks),
            book_fresh=state.book_fresh,
            bbo_fresh=state.bbo_at is not None,
            funding_rate=state.funding_rate,
            volume_24h_pusd=None,
            snapshot_at=state.bbo_at or now,
        )
        return EvaluationContext(
            instrument_pk=state.instrument_pk,
            instrument_id=state.instrument_id,
            symbol=symbol,
            asset_class=state.asset_class,
            as_of=now,
            series=series,
            market=market,
            session_state=state.session_state,
            session_allowed=state.session_allowed,
            watchlist_active=state.watchlist_active,
            bot_paused=False,
            data_quality_status=state.data_quality_status,
            data_quality_ok=state.data_quality_ok,
            orderbook_ok=state.book_fresh,
            market_quality_score=state.market_quality_score,
            low_liquidity=False,
            strategy_name=settings.name,
            strategy_version=settings.version,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            config_hash=configuration_hash(settings),
            ruleset_hash=ruleset_hash(),
        )

    def _atr(
        self, state: SymbolReplayState, timeframe: str, settings: StrategySettings
    ) -> float | None:
        candles = state.candles.get(timeframe) or []
        if len(candles) < _ATR_PERIOD + 1:
            return None
        try:
            series = CandleSeries.build(
                Timeframe(timeframe),
                list(candles),
                self._clock.now(),
                min_candles=_ATR_PERIOD + 1,
                max_gap_multiplier=settings.max_candle_gap_multiplier,
            )
        except CandleSeriesError:
            return None
        return candle_atr(series, _ATR_PERIOD)

    def instrument_snapshot(self, symbol: str) -> InstrumentRiskSnapshot:
        state = self.state_for(symbol)
        now = self._clock.now()
        book = state.book_snapshot()
        mid = (
            (book.best_bid + book.best_ask) / 2
            if book.best_bid is not None and book.best_ask is not None
            else None
        )
        return InstrumentRiskSnapshot(
            instrument_pk=state.instrument_pk,
            instrument_id=state.instrument_id,
            symbol=symbol,
            status=state.instrument_status,
            asset_class=state.asset_class,
            mark_price=state.mark_price,
            index_price=state.mark_price,
            mid_price=mid,
            last_price=None,
            max_leverage=state.max_leverage,
            min_notional=state.min_notional,
            tick_size=state.tick_size,
            price_decimals=state.price_decimals,
            quantity_decimals=state.quantity_decimals,
            risk_tiers=(),
            maintenance_margin_rate=state.maintenance_margin_rate,
            initial_margin_rate=state.initial_margin_rate,
            liquidation_fee_rate=state.liquidation_fee_rate,
            isolated_only=True,
            funding_interval_hours=state.funding_interval_hours,
            instrument_updated_at=now,
            data_quality_status=state.data_quality_status,
            data_quality_ok=state.data_quality_ok,
            orderbook_fresh=state.book_fresh,
            source="historical_replay",
            snapshot_version=INSTRUMENT_RISK_SNAPSHOT_VERSION,
            as_of=now,
        )

    def risk_context(
        self,
        candidate: CandidateSnapshot,
        *,
        fee_schedule: FeeScheduleSnapshot | None,
        risk_settings: RiskSettings,
        cost_settings: CostSettings,
        strategy_settings: StrategySettings,
        levels: tuple[TechnicalLevel, ...],
    ) -> RiskEvaluationContext:
        """Phase-9 evaluation context from the observable replay state."""
        state = self.state_for(candidate.symbol)
        now = self._clock.now()
        book = state.book_snapshot()
        return RiskEvaluationContext(
            candidate=candidate,
            instrument=self.instrument_snapshot(candidate.symbol),
            book=book,
            best_bid=state.best_bid,
            best_ask=state.best_ask,
            bbo_at=state.bbo_at,
            bbo_fresh=state.bbo_at is not None,
            funding=state.funding_snapshot(now),
            fee_schedule=fee_schedule,
            execution_assumptions=build_execution_assumptions(cost_settings),
            levels=levels,
            atr_5m=self._atr(state, "5m", strategy_settings),
            atr_15m=self._atr(state, "15m", strategy_settings),
            session_state=state.session_state,
            session_allowed=state.session_allowed,
            equity_overnight_risk=False,
            watchlist_active=state.watchlist_active,
            bot_paused=False,
            as_of=now,
            risk_model_name=risk_settings.model_name,
            risk_model_version=risk_settings.model_version,
            risk_config_hash=risk_configuration_hash(risk_settings),
            cost_model_name=cost_settings.model_name,
            cost_model_version=cost_settings.model_version,
            cost_config_hash=cost_configuration_hash(cost_settings),
            instrument_snapshot_version=INSTRUMENT_RISK_SNAPSHOT_VERSION,
        )

    def market_reference(self, symbol: str) -> MarketReferenceSnapshot:
        """Public market reference for a modelled simulation leg."""
        state = self.state_for(symbol)
        now = self._clock.now()
        return MarketReferenceSnapshot(
            as_of=now,
            book=state.book_snapshot(),
            best_bid=state.best_bid,
            best_ask=state.best_ask,
            mark_price=state.mark_price,
            bbo_at=state.bbo_at,
            book_at=state.book_at,
            data_quality_status=state.data_quality_status,
            data_quality_ok=state.data_quality_ok,
            snapshot_id=uuid.uuid5(uuid.NAMESPACE_OID, f"{symbol}|{now.isoformat()}"),
        )

    def last_closed_5m(self, symbol: str) -> Candle | None:
        candles = self.state_for(symbol).candles.get("5m") or []
        return candles[-1] if candles else None
