"""Strategy core value objects: immutable snapshots and results.

The central anti-look-ahead guarantee lives in :class:`CandleSeries`: it only
ever contains CLOSED candles with close_time <= as_of, strictly ordered,
gap-checked.  Every downstream feature and rule operates on these series and
can therefore never see future data.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.enums import Timeframe
from app.strategy.enums import (
    CandidateDirection,
    CandidateState,
    CandidateType,
    LiquidityLevelType,
    RejectionCode,
    RetestStatus,
    StrategyRegime,
    StructureEventType,
    StructureState,
    SweepDirection,
    SwingType,
)


@dataclass(frozen=True)
class Candle:
    """A closed OHLCV candle (floats for deterministic feature math)."""

    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int
    timeframe: Timeframe

    @property
    def close_time(self) -> datetime:
        return self.open_time + timedelta(milliseconds=self.timeframe.milliseconds)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open


class CandleSeriesError(Exception):
    def __init__(self, code: RejectionCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CandleSeries:
    """Validated, closed-only candle series up to ``as_of``."""

    timeframe: Timeframe
    candles: tuple[Candle, ...]
    as_of: datetime

    @classmethod
    def build(
        cls,
        timeframe: Timeframe,
        candles: list[Candle],
        as_of: datetime,
        *,
        min_candles: int,
        max_gap_multiplier: float,
    ) -> CandleSeries:
        """Validate and freeze a series.  Raises CandleSeriesError on any
        integrity problem - the caller must reject, never guess."""
        tf_ms = timeframe.milliseconds
        closed = [candle for candle in candles if candle.close_time <= as_of]
        dropped_open = len(candles) - len(closed)
        closed.sort(key=lambda candle: candle.open_time)
        # de-duplicate on open_time (revisions of the same candle: last wins
        # while open; the repository only serves the latest revision)
        unique: dict[datetime, Candle] = {candle.open_time: candle for candle in closed}
        ordered = sorted(unique.values(), key=lambda candle: candle.open_time)

        if not ordered:
            raise CandleSeriesError(
                RejectionCode.OPEN_CANDLE_ONLY
                if dropped_open > 0
                else RejectionCode.INSUFFICIENT_CANDLE_HISTORY,
                f"{timeframe.value}: no closed candles up to as_of",
            )
        if len(ordered) < min_candles:
            raise CandleSeriesError(
                RejectionCode.INSUFFICIENT_CANDLE_HISTORY,
                f"{timeframe.value}: {len(ordered)} closed candles < required {min_candles}",
            )
        max_gap = timedelta(milliseconds=tf_ms * max_gap_multiplier)
        for previous, current in itertools.pairwise(ordered):
            delta = current.open_time - previous.open_time
            if delta <= timedelta(0):
                raise CandleSeriesError(
                    RejectionCode.CANDLE_GAP,
                    f"{timeframe.value}: non-monotonic candle order at {current.open_time}",
                )
            if delta > max_gap:
                raise CandleSeriesError(
                    RejectionCode.CANDLE_GAP,
                    f"{timeframe.value}: gap {delta} after {previous.open_time}",
                )
        # freshness: the most recent closed candle must be adjacent to as_of
        last = ordered[-1]
        staleness = as_of - last.close_time
        if staleness > timedelta(milliseconds=tf_ms * max_gap_multiplier):
            raise CandleSeriesError(
                RejectionCode.CANDLE_GAP,
                f"{timeframe.value}: last closed candle {last.open_time} too old for as_of",
            )
        return cls(timeframe=timeframe, candles=tuple(ordered), as_of=as_of)

    def __len__(self) -> int:
        return len(self.candles)

    @property
    def last(self) -> Candle:
        return self.candles[-1]

    def window_metadata(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe.value,
            "count": len(self.candles),
            "first_open_time": self.candles[0].open_time.isoformat(),
            "last_open_time": self.last.open_time.isoformat(),
            "last_close_time": self.last.close_time.isoformat(),
        }


@dataclass(frozen=True)
class Swing:
    swing_id: str
    timeframe: Timeframe
    swing_type: SwingType
    price: float
    candle_open_time: datetime
    confirmed_at: datetime
    strength: float
    relevance: float
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StructureEvent:
    event_type: StructureEventType
    timeframe: Timeframe
    price: float
    reference_swing_id: str | None
    confirm_close_time: datetime
    detail: str


@dataclass(frozen=True)
class StructureAnalysis:
    state: StructureState
    events: tuple[StructureEvent, ...]
    swings: tuple[Swing, ...]
    detail: str


@dataclass(frozen=True)
class LiquidityLevel:
    level_type: LiquidityLevelType
    timeframe: Timeframe
    price: float
    relevance: float
    touches: int
    is_major: bool
    detail: str


@dataclass(frozen=True)
class SweepEvent:
    level: LiquidityLevel
    direction: SweepDirection
    sweep_candle_open_time: datetime
    overshoot_bps: float
    reacted: bool
    confirmed: bool
    confidence: float
    detail: str


@dataclass(frozen=True)
class ReclaimEvent:
    level_price: float
    close_price: float
    distance_bps: float
    confirmed_at: datetime
    confidence: float
    detail: str


@dataclass(frozen=True)
class RejectionEvent:
    level_price: float
    wick_ratio: float
    close_position: float
    followed_through: bool
    confirmed_at: datetime
    confidence: float
    detail: str


@dataclass(frozen=True)
class RetestResult:
    status: RetestStatus
    level_price: float | None
    detail: str


@dataclass(frozen=True)
class RegimeResult:
    regime: StrategyRegime
    confidence: int
    secondary_flags: tuple[str, ...]
    reasons: tuple[str, ...]
    features_used: dict[str, float | str | None]


@dataclass(frozen=True)
class MarketContextSnapshot:
    """Latest valid public market/book state as of evaluation time.

    Invalid/unavailable values are None and flagged - never estimated.
    """

    mark_price: float | None
    index_price: float | None
    last_price: float | None
    mid_price: float | None
    best_bid: float | None
    best_ask: float | None
    spread_bps: float | None
    bid_depth_pusd: float | None
    ask_depth_pusd: float | None
    book_fresh: bool
    bbo_fresh: bool
    funding_rate: float | None
    volume_24h_pusd: float | None
    snapshot_at: datetime


@dataclass(frozen=True)
class EvaluationContext:
    """Immutable input snapshot for one pure strategy evaluation."""

    instrument_pk: int
    instrument_id: int
    symbol: str
    asset_class: str
    as_of: datetime
    series: dict[str, CandleSeries]  # timeframe value -> series
    market: MarketContextSnapshot
    session_state: str
    session_allowed: bool
    watchlist_active: bool
    bot_paused: bool
    data_quality_status: str
    data_quality_ok: bool
    orderbook_ok: bool
    market_quality_score: int | None
    low_liquidity: bool
    strategy_name: str
    strategy_version: str
    feature_schema_version: str
    config_hash: str
    ruleset_hash: str
    settings_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreBreakdown:
    component: str
    max_points: int
    awarded: float
    reason: str


@dataclass(frozen=True)
class SetupCandidate:
    """Internal research hypothesis with a structure direction.

    NOT a trade recommendation, NOT a signal, and never sent to Telegram.
    Carries no trade execution parameters of any kind.
    """

    candidate_id: uuid.UUID
    candidate_type: CandidateType
    direction: CandidateDirection
    state: CandidateState
    instrument_pk: int
    instrument_id: int
    symbol: str
    asset_class: str
    as_of: datetime
    expiry_at: datetime  # technical evaluation expiry, not a trade expiry
    strategy_name: str
    strategy_version: str
    feature_schema_version: str
    config_hash: str
    ruleset_hash: str
    session_state: str
    market_quality_score: int | None
    data_quality_status: str
    primary_regime: StrategyRegime
    regime_confidence: int
    higher_timeframe_structure: StructureState
    local_structure: StructureState
    referenced_levels: tuple[dict[str, Any], ...]
    structure_events: tuple[str, ...]
    liquidity_events: tuple[str, ...]
    reclaim_status: str
    rejection_status: str
    retest_status: RetestStatus
    setup_score: int
    score_components: tuple[ScoreBreakdown, ...]
    confidence: int
    reason_summary: str
    reason_codes: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    dedupe_key: str
    feature_snapshot_id: uuid.UUID | None
    candle_window_metadata: dict[str, Any]
    features: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SetupRejection:
    rejection_id: uuid.UUID
    instrument_pk: int
    instrument_id: int
    symbol: str
    as_of: datetime
    primary_code: RejectionCode
    codes: tuple[RejectionCode, ...]
    detail: str
    strategy_name: str
    strategy_version: str
    config_hash: str
    candidate_type: CandidateType | None = None


@dataclass(frozen=True)
class EvaluationOutcome:
    """Result of one pure evaluation: candidates found and/or rejections."""

    context_symbol: str
    as_of: datetime
    regime: RegimeResult | None
    candidates: tuple[SetupCandidate, ...]
    rejections: tuple[SetupRejection, ...]
    feature_values: dict[str, Any] = field(default_factory=dict)
    feature_validity: dict[str, bool] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    candle_window_metadata: dict[str, Any] = field(default_factory=dict)
    structure_by_timeframe: dict[str, StructureAnalysis] = field(default_factory=dict)


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
