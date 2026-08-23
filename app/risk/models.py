"""Risk engine value objects.

Everything here is an immutable, hypothetical research reference built from
public market data only.  No object models a real position, a real balance
or an order - reference prices are internal model values, prominently
labelled as such in every user-facing rendering.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.costs.models import (
    CostEstimate,
    ExecutionAssumptions,
    FeeScheduleSnapshot,
    FundingSnapshot,
    OrderbookDepthSnapshot,
)
from app.risk.enums import (
    EntryBasis,
    InvalidationBasis,
    MarginModelMode,
    PlanRejectionCode,
    PlanStatus,
    TargetLevelType,
)


class RiskEngineError(Exception):
    """Raised when an evaluation must stop with a structured rejection."""

    def __init__(self, code: PlanRejectionCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CandidateSnapshot:
    """Immutable view of a phase-8 research candidate (from persistence)."""

    candidate_id: uuid.UUID
    candidate_type: str
    direction: str  # "BULLISH" | "BEARISH" - research classification
    state: str
    instrument_pk: int
    instrument_id: int
    symbol: str
    asset_class: str
    as_of: datetime
    expiry_at: datetime
    setup_score: int
    primary_regime: str
    referenced_levels: tuple[dict[str, Any], ...]
    invalidation_conditions: tuple[str, ...]
    strategy_name: str
    strategy_version: str
    strategy_config_hash: str
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def bullish(self) -> bool:
        return self.direction == "BULLISH"


@dataclass(frozen=True)
class InstrumentRiskSnapshot:
    """Public instrument + market state relevant for risk plausibility.

    Built exclusively from phase-4 instrument metadata and phase-5 public
    ticker/book data.  ``maintenance_margin_rate``/``initial_margin_rate``
    stay ``None`` unless the public metadata provides them - the margin
    model then blocks by default instead of guessing.
    """

    instrument_pk: int
    instrument_id: int
    symbol: str
    status: str
    asset_class: str
    mark_price: float | None
    index_price: float | None
    mid_price: float | None
    last_price: float | None
    max_leverage: int | None
    min_notional: float | None
    tick_size: float | None
    price_decimals: int
    quantity_decimals: int
    risk_tiers: tuple[dict[str, Any], ...]
    maintenance_margin_rate: float | None
    initial_margin_rate: float | None
    liquidation_fee_rate: float | None
    isolated_only: bool
    funding_interval_hours: float | None
    instrument_updated_at: datetime | None
    data_quality_status: str
    data_quality_ok: bool
    orderbook_fresh: bool
    source: str
    snapshot_version: str
    as_of: datetime

    @property
    def reference_price(self) -> float | None:
        """Mark price is the primary margin/liquidation reference."""
        return self.mark_price if self.mark_price is not None else self.mid_price

    def as_payload(self) -> dict[str, Any]:
        return {
            "instrument_pk": self.instrument_pk,
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "status": self.status,
            "asset_class": self.asset_class,
            "mark_price": self.mark_price,
            "index_price": self.index_price,
            "mid_price": self.mid_price,
            "last_price": self.last_price,
            "max_leverage": self.max_leverage,
            "min_notional": self.min_notional,
            "tick_size": self.tick_size,
            "price_decimals": self.price_decimals,
            "quantity_decimals": self.quantity_decimals,
            "risk_tiers": list(self.risk_tiers),
            "maintenance_margin_rate": self.maintenance_margin_rate,
            "initial_margin_rate": self.initial_margin_rate,
            "liquidation_fee_rate": self.liquidation_fee_rate,
            "isolated_only": self.isolated_only,
            "funding_interval_hours": self.funding_interval_hours,
            "instrument_updated_at": (
                self.instrument_updated_at.isoformat() if self.instrument_updated_at else None
            ),
            "data_quality_status": self.data_quality_status,
            "orderbook_fresh": self.orderbook_fresh,
            "source": self.source,
            "snapshot_version": self.snapshot_version,
            "as_of": self.as_of.isoformat(),
        }


@dataclass(frozen=True)
class TechnicalLevel:
    """A confirmed public price level usable as target/invalidation context."""

    price: float
    kind: str  # e.g. SWING_HIGH, SWING_LOW, RANGE_HIGH, EQUAL_HIGHS, ...
    timeframe: str
    relevance: float
    source_id: str


@dataclass(frozen=True)
class TechnicalInvalidation:
    """Structural price at which the research thesis is no longer valid.

    An internal research risk definition - never a stop-loss recommendation
    and never a user-facing order parameter.
    """

    invalidation_id: uuid.UUID
    candidate_id: uuid.UUID
    direction: str
    invalidation_price: float
    reference_level_price: float
    reference_level_type: InvalidationBasis
    buffer_bps: float
    buffer_atr_component: float
    structure_reason: str
    source_timeframe: str
    source_ids: tuple[str, ...]
    confidence: float
    technical_conditions: tuple[str, ...]
    computed_at: datetime


@dataclass(frozen=True)
class ReferenceEntryZone:
    """Hypothetical technical entry zone (model value, never an instruction)."""

    entry_low: float
    entry_high: float
    entry_reference_price: float
    entry_basis: EntryBasis
    direction: str
    valid_until: datetime
    bbo_snapshot_at: datetime | None
    bbo_stale: bool
    execution_mode: str
    rationale: str


@dataclass(frozen=True)
class ReferenceTarget:
    """Technical reference target level - never a take-profit order."""

    target_id: uuid.UUID
    price: float
    target_type: TargetLevelType
    source_timeframe: str
    source_id: str
    relevance: float
    direction_compatible: bool
    distance_bps: float
    confidence: float


@dataclass(frozen=True)
class RiskDistance:
    """Distance between reference entry and technical invalidation."""

    absolute: float
    bps: float
    atr_value: float | None
    atr_multiple: float | None


@dataclass(frozen=True)
class ReferencePosition:
    """Purely hypothetical reference sizing against the virtual account.

    Never derived from - and never comparable to - a real account, balance
    or position.
    """

    virtual_account_pusd: float
    risk_per_plan_pct: float
    reference_cash_risk: float
    risk_per_unit: float
    raw_quantity: float
    reference_quantity: float  # after quantity rounding
    reference_notional: float
    effective_cash_risk: float  # risk re-checked after rounding
    quantity_decimals: int


@dataclass(frozen=True)
class MarginModelResult:
    """Isolated-margin research model - approximated only via explicit opt-in."""

    mode: MarginModelMode
    approximated: bool
    initial_margin_rate: float | None
    maintenance_margin_rate: float | None
    required_initial_margin_estimate: float | None
    maintenance_margin_estimate: float | None
    hypothetical_liquidation_price: float | None
    reference_leverage: float | None
    mark_price_reference: float | None
    approximation_flags: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class LiquidationBufferResult:
    """Distance between technical invalidation and hypothetical liquidation.

    A conservative model check - never a guarantee against liquidation.
    """

    sufficient: bool
    buffer_abs: float | None
    buffer_bps: float | None
    buffer_atr_multiple: float | None
    min_required_bps: float
    min_required_atr_multiple: float
    detail: str


@dataclass(frozen=True)
class LeverageSuitabilityRange:
    """Conservative reference-leverage suitability - never an optimal claim."""

    allowed_leverage_min: float
    allowed_leverage_max: float
    recommended_reference_leverage: float
    max_market_leverage: int
    required_initial_margin_estimate: float | None
    maintenance_margin_estimate: float | None
    liquidation_buffer_bps: float | None
    suitability_reasons: tuple[str, ...]
    model_confidence: float
    approximation_flags: tuple[str, ...]


@dataclass(frozen=True)
class ScoreComponent:
    component: str
    max_points: int
    awarded: float
    reason: str


@dataclass(frozen=True)
class RiskEvaluationContext:
    """Immutable inputs for one risk plan evaluation - assembled by the
    context builder, consumed by the pure engine core."""

    candidate: CandidateSnapshot
    instrument: InstrumentRiskSnapshot
    book: OrderbookDepthSnapshot
    best_bid: float | None
    best_ask: float | None
    bbo_at: datetime | None
    bbo_fresh: bool
    funding: FundingSnapshot
    fee_schedule: FeeScheduleSnapshot | None
    execution_assumptions: ExecutionAssumptions
    levels: tuple[TechnicalLevel, ...]
    atr_5m: float | None
    atr_15m: float | None
    session_state: str
    session_allowed: bool
    equity_overnight_risk: bool
    watchlist_active: bool
    bot_paused: bool
    as_of: datetime
    risk_model_name: str
    risk_model_version: str
    risk_config_hash: str
    cost_model_name: str
    cost_model_version: str
    cost_config_hash: str
    instrument_snapshot_version: str


@dataclass(frozen=True)
class SignalEligibilityPlan:
    """Internal technical research eligibility assessment.

    NOT an order, NOT a trade recommendation, NOT a Telegram message, NOT a
    guarantee and NOT based on any real account or position.
    """

    plan_id: uuid.UUID
    status: PlanStatus
    candidate_id: uuid.UUID
    candidate_type: str
    direction: str
    instrument_pk: int
    instrument_id: int
    symbol: str
    asset_class: str
    instrument_snapshot: InstrumentRiskSnapshot
    entry_zone: ReferenceEntryZone
    invalidation: TechnicalInvalidation
    targets: tuple[ReferenceTarget, ...]
    risk_distance: RiskDistance
    position: ReferencePosition
    leverage: LeverageSuitabilityRange
    margin: MarginModelResult
    liquidation_buffer: LiquidationBufferResult
    costs: CostEstimate
    eligibility_score: int
    score_components: tuple[ScoreComponent, ...]
    eligibility_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    dedupe_key: str
    risk_model_name: str
    risk_model_version: str
    risk_config_hash: str
    cost_model_name: str
    cost_model_version: str
    cost_config_hash: str
    fee_schedule_version: str
    execution_assumption_version: str
    instrument_snapshot_version: str
    strategy_name: str
    strategy_version: str
    strategy_config_hash: str
    as_of: datetime
    expiry_at: datetime
    data_timestamps: dict[str, str] = field(default_factory=dict)

    @property
    def net_rr_primary(self) -> float | None:
        primary = self.costs.primary_target
        return primary.net_rr if primary else None


@dataclass(frozen=True)
class RiskPlanRejection:
    """Structured, versioned rejection of a candidate at the risk stage."""

    rejection_id: uuid.UUID
    candidate_id: uuid.UUID | None
    instrument_pk: int
    instrument_id: int
    symbol: str
    as_of: datetime
    primary_code: PlanRejectionCode
    codes: tuple[PlanRejectionCode, ...]
    detail: str
    risk_model_name: str
    risk_model_version: str
    risk_config_hash: str
    cost_model_version: str
    cost_config_hash: str
    fee_schedule_version: str | None


@dataclass(frozen=True)
class RiskEvaluationOutcome:
    """Result of one candidate evaluation: a plan, or a rejection."""

    candidate_id: uuid.UUID
    symbol: str
    as_of: datetime
    plan: SignalEligibilityPlan | None
    rejection: RiskPlanRejection | None
    warnings: tuple[str, ...] = ()
