"""Immutable value objects for hypothetical simulations.

Every price, quantity and result here is a MODEL value: a hypothetical,
delayed follower simulation over public data.  Nothing describes a real
execution, a real position, a real account or a realised outcome.  The
core consumes plain snapshots so it stays pure and deterministic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.costs.models import FeeScheduleSnapshot, FundingSnapshot, OrderbookDepthSnapshot
from app.simulation.enums import (
    DataCompleteness,
    DelayModel,
    SimulatedPositionState,
    SimulationExitReason,
    SimulationLegKind,
    SimulationRejectionCode,
)


@dataclass(frozen=True)
class LifecycleObservation:
    """One observed phase-10 lifecycle state/event (audit input only)."""

    signal_id: uuid.UUID
    plan_id: uuid.UUID
    candidate_id: uuid.UUID
    state: str
    event_type: str
    state_version: int
    as_of: datetime
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SimulationPlanReference:
    """Audited plan reference values copied for one simulation.

    These are phase-9 model reference levels, never order parameters.
    """

    plan_id: uuid.UUID
    candidate_id: uuid.UUID
    instrument_pk: int
    instrument_id: int
    symbol: str
    asset_class: str
    candidate_type: str
    direction: str  # "BULLISH" | "BEARISH" - research context only
    entry_reference_price: float
    invalidation_price: float
    target_prices: tuple[float, ...]
    reference_quantity: float
    reference_notional: float
    risk_per_unit: float
    virtual_account_pusd: float
    strategy_version: str
    risk_model_version: str
    cost_model_version: str
    fee_schedule_version: str
    execution_assumption_version: str

    @property
    def bullish(self) -> bool:
        return self.direction == "BULLISH"


@dataclass(frozen=True)
class MarketReferenceSnapshot:
    """Public market state at ONE reference instant (no last-price basis)."""

    as_of: datetime
    book: OrderbookDepthSnapshot
    best_bid: float | None
    best_ask: float | None
    mark_price: float | None
    bbo_at: datetime | None
    book_at: datetime | None
    data_quality_status: str
    data_quality_ok: bool
    snapshot_id: uuid.UUID | None = None

    def staleness_seconds(self, now: datetime) -> float | None:
        newest = max(
            (stamp for stamp in (self.bbo_at, self.book_at) if stamp is not None),
            default=None,
        )
        if newest is None:
            return None
        return max(0.0, (now - newest).total_seconds())


@dataclass(frozen=True)
class DelayRealization:
    """Outcome of the versioned follower delay model."""

    model: DelayModel
    parameters: dict[str, float]
    delay_seconds: float
    event_reference_at: datetime
    scheduled_entry_at: datetime
    seed: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class ModelledExecution:
    """One modelled leg of a hypothetical simulation - never an execution.

    ``modelled_price`` is what a conservative taker-side book walk WOULD
    have implied at the reference instant; it is not a fill and nobody
    transacted at it.
    """

    execution_id: uuid.UUID
    leg: SimulationLegKind
    side_consumed: str  # "bid" | "ask" - the modelled adverse side
    modelled_price: float
    quantity: float
    notional: float
    reference_price: float
    slippage_cost: float
    slippage_bps: float
    fee_cost: float
    levels_consumed: int
    book_snapshot_id: uuid.UUID | None
    book_at: datetime | None
    as_of: datetime
    assumptions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelledFunding:
    """Modelled funding cost over the hypothetical holding window."""

    funding_cost: float
    intervals_modelled: int
    hours_modelled: float
    rate_basis: str
    data_available: bool
    detail: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SimulationCostBreakdown:
    """All modelled frictions of one hypothetical simulation."""

    entry_fee: float
    exit_fee: float
    entry_slippage: float
    exit_slippage: float
    funding_cost: float

    @property
    def fees_total(self) -> float:
        return self.entry_fee + self.exit_fee

    @property
    def slippage_total(self) -> float:
        return self.entry_slippage + self.exit_slippage

    @property
    def total(self) -> float:
        return self.fees_total + self.slippage_total + self.funding_cost


@dataclass(frozen=True)
class SimulatedPositionResult:
    """Complete hypothetical simulation outcome for one lifecycle.

    Gross/net values are MODEL results in virtual reference units; they are
    never realised amounts and never a statement about real performance.
    """

    simulation_position_id: uuid.UUID
    run_id: uuid.UUID
    lifecycle_signal_id: uuid.UUID
    plan_id: uuid.UUID
    candidate_id: uuid.UUID
    symbol: str
    asset_class: str
    candidate_type: str
    direction: str
    state: SimulatedPositionState
    event_reference_at: datetime
    delay: DelayRealization | None
    entry: ModelledExecution | None
    exit_execution: ModelledExecution | None
    partial_reduction: ModelledExecution | None
    exit_reason: SimulationExitReason | None
    funding: ModelledFunding | None
    costs: SimulationCostBreakdown | None
    gross_result: float | None
    net_result: float | None
    gross_r: float | None
    net_r: float | None
    duration_seconds: float | None
    data_completeness: DataCompleteness
    warnings: tuple[str, ...] = ()
    dedupe_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.state is SimulatedPositionState.CLOSED_SIMULATION


@dataclass(frozen=True)
class SimulationRejection:
    """Structured, aggregated reason a simulation was not modelled."""

    rejection_id: uuid.UUID
    run_id: uuid.UUID | None
    lifecycle_signal_id: uuid.UUID | None
    plan_id: uuid.UUID | None
    candidate_id: uuid.UUID | None
    symbol: str
    as_of: datetime
    primary_code: SimulationRejectionCode
    codes: tuple[SimulationRejectionCode, ...]
    detail: str
    simulation_model_version: str
    simulation_config_hash: str


@dataclass(frozen=True)
class SimulationInputs:
    """Everything the pure simulation core needs for one lifecycle.

    Assembled by an adapter from persisted public data; the core performs
    no I/O and reads nothing beyond this snapshot.
    """

    lifecycle_signal_id: uuid.UUID
    plan: SimulationPlanReference
    entry_observation: LifecycleObservation
    exit_observation: LifecycleObservation | None
    entry_market: MarketReferenceSnapshot | None
    exit_market: MarketReferenceSnapshot | None
    fee_schedule: FeeScheduleSnapshot | None
    funding: FundingSnapshot | None
    funding_interval_hours: float | None
    session_allowed: bool
    session_state: str
    data_quality_ok: bool
    bot_paused: bool
    duplicate_exists: bool
    plan_audit_available: bool
    candidate_audit_available: bool
    run_id: uuid.UUID
    as_of: datetime
    #: filled by the adapter when the lifecycle already reached a terminal state
    exit_reason: SimulationExitReason | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DataGapInterval:
    """A time window excluded from a backtest because data was missing."""

    start_at: datetime
    end_at: datetime
    channel: str
    symbol: str
    detail: str

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end_at - self.start_at).total_seconds())


@dataclass(frozen=True)
class DataCoverageReport:
    """Outcome of validating historical inputs before a backtest run."""

    complete: bool
    gaps: tuple[DataGapInterval, ...]
    excluded_intervals: tuple[DataGapInterval, ...]
    missing_channels: tuple[str, ...]
    checked_channels: tuple[str, ...]
    detail: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayEvent:
    """One immutable, time-ordered replay input event."""

    as_of: datetime
    category: str
    symbol: str
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricValue:
    """One named hypothetical metric value with its unit."""

    name: str
    value: float | None
    unit: str
    detail: str = ""


@dataclass(frozen=True)
class MetricSet:
    """A segment's hypothetical metrics plus its sample-size verdict."""

    segment_kind: str
    segment_key: str
    sample_status: str
    complete_simulations: int
    values: tuple[MetricValue, ...]
    metrics_version: str
    disclaimer_version: str
    warnings: tuple[str, ...] = ()

    def value_of(self, name: str) -> float | None:
        for metric in self.values:
            if metric.name == name:
                return metric.value
        return None


@dataclass(frozen=True)
class DrawdownResult:
    """Hypothetical drawdown of a modelled equity curve (R or virtual)."""

    basis: str  # "R" | "VIRTUAL_ACCOUNT_PUSD"
    peak_value: float
    trough_value: float
    max_drawdown: float
    max_drawdown_pct: float | None
    peak_index: int
    trough_index: int
    recovery_index: int | None
    drawdown_duration_steps: int
    recovered: bool


@dataclass(frozen=True)
class BootstrapResult:
    """Seeded resampling uncertainty of a hypothetical net-R sample."""

    statistic: str
    observed: float
    resamples: int
    seed: int
    lower_percentile: float
    upper_percentile: float
    lower_bound: float
    upper_bound: float
    sample_size: int
    note: str
