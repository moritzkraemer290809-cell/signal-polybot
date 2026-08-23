"""Cost engine value objects.

Immutable snapshots and results.  Every number here is a conservative
hypothetical research estimate on notional basis - never a realised cost,
never a promise, never an order parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.costs.enums import ExecutionMode, FeeKind, FundingModelState


@dataclass(frozen=True)
class FeeScheduleSnapshot:
    """Versioned fee assumption used for one evaluation.

    Rates are decimal fractions of notional.  The schedule is an assumption
    from administered configuration - it must be validated against the
    official Polymarket documentation before live operation.
    """

    version: str
    source: str
    tier: str
    maker_fee_rate: float
    taker_fee_rate: float
    config_hash: str
    active: bool
    effective_at: datetime | None = None

    def rate_for(self, kind: FeeKind) -> float:
        return self.maker_fee_rate if kind is FeeKind.MAKER else self.taker_fee_rate


@dataclass(frozen=True)
class ExecutionAssumptions:
    """Versioned execution modes for the three hypothetical plan legs."""

    version: str
    entry: ExecutionMode
    target: ExecutionMode
    invalidation: ExecutionMode

    def as_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "entry": self.entry.value,
            "target": self.target.value,
            "invalidation": self.invalidation.value,
        }


@dataclass(frozen=True)
class DepthLevel:
    price: float
    quantity: float


@dataclass(frozen=True)
class OrderbookDepthSnapshot:
    """Immutable public order book excerpt for VWAP walking.

    ``bids`` descending by price, ``asks`` ascending by price.  ``fresh`` is
    the phase-5 reliability + freshness verdict - a stale or resyncing book
    must never be walked.
    """

    instrument_id: int
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]
    fresh: bool
    snapshot_at: datetime | None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread_bps(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None or bid <= 0:
            return None
        mid = (bid + ask) / 2
        return (ask - bid) / mid * 10_000


@dataclass(frozen=True)
class VwapResult:
    """Result of walking one book side for a hypothetical quantity."""

    side: str  # "bid" | "ask"
    requested_quantity: float
    filled_quantity: float
    unfilled_quantity: float
    vwap_price: float | None
    reference_price: float | None  # best price of the walked side
    slippage_abs: float | None
    slippage_bps: float | None
    levels_consumed: int
    detail: str

    @property
    def fully_filled(self) -> bool:
        return self.unfilled_quantity <= 0 and self.vwap_price is not None


@dataclass(frozen=True)
class LegExecutionEstimate:
    """Conservative execution estimate for one hypothetical plan leg."""

    leg: str  # "entry" | "target" | "invalidation"
    mode: ExecutionMode
    fee_kind: FeeKind
    reference_price: float
    execution_price: float
    vwap: VwapResult
    stress_bps_applied: float
    slippage_cost: float  # pUSD vs the leg's reference price
    fee_cost: float  # pUSD on executed notional
    notional: float


@dataclass(frozen=True)
class FundingSnapshot:
    """Public funding context: current rate + persisted history."""

    current_rate: float | None
    history: tuple[tuple[datetime, float], ...]
    next_funding_at: datetime | None
    interval_hours: float
    data_fresh: bool


@dataclass(frozen=True)
class FundingProjection:
    """Conservative expected funding cost over the technical hold window.

    Never models funding income as required profit: a favourable rate is
    floored at zero benefit.  ``state`` documents how the number was formed.
    """

    state: FundingModelState
    direction: str  # "BULLISH" | "BEARISH"
    hold_minutes: int
    intervals_charged: int
    assumed_rate_per_interval: float
    expected_cost: float  # pUSD, >= 0
    buffer_cost: float  # pUSD from the thin-data buffer
    basis: str
    detail: str


@dataclass(frozen=True)
class TargetCostBreakdown:
    """Gross vs net economics for one reference target level."""

    target_price: float
    target_index: int
    gross_target_pnl: float
    net_target_pnl: float
    gross_rr: float | None
    net_rr: float | None
    exit_fee: float
    exit_slippage_cost: float


@dataclass(frozen=True)
class CostEstimate:
    """Full conservative cost picture for one hypothetical plan.

    All values in pUSD on notional basis.  This is a research estimate under
    versioned assumptions - not a realised cost and not a guarantee.
    """

    cost_model_name: str
    cost_model_version: str
    cost_config_hash: str
    fee_schedule: FeeScheduleSnapshot
    execution_assumptions: ExecutionAssumptions
    entry: LegExecutionEstimate
    invalidation: LegExecutionEstimate
    targets: tuple[TargetCostBreakdown, ...]
    funding: FundingProjection
    gross_invalidation_loss: float
    net_invalidation_loss: float
    total_entry_cost: float
    total_invalidation_path_cost: float
    cost_buffer: float
    cost_to_risk_pct: float  # percent points of the gross technical risk
    warnings: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def best_net_rr(self) -> float | None:
        values = [t.net_rr for t in self.targets if t.net_rr is not None]
        return max(values) if values else None

    @property
    def primary_target(self) -> TargetCostBreakdown | None:
        return self.targets[0] if self.targets else None


class CostModelError(Exception):
    """Raised by cost components when a conservative estimate is impossible.

    ``code`` carries the risk-plan rejection code name so the risk engine can
    map the failure to a structured rejection without importing risk enums.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
