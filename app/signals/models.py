"""Signal lifecycle value objects - immutable snapshots, primitives only.

The lifecycle core deliberately imports no strategy/risk/cost/telegram
modules: plans, candidates and market state arrive as plain snapshots built
by the adapter layer.  Every price here is an internal technical model
reference - never a fill, never a position, never an instruction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.signals.enums import (
    EntryTriggerType,
    SignalEventType,
    SignalRejectionCode,
    SignalState,
    SignalUpdateType,
)


@dataclass(frozen=True)
class PlanSnapshot:
    """Immutable view of a phase-9 eligibility plan (from persistence)."""

    plan_id: uuid.UUID
    candidate_id: uuid.UUID
    status: str
    instrument_pk: int
    instrument_id: int
    symbol: str
    asset_class: str
    candidate_type: str
    direction: str  # "BULLISH" | "BEARISH" - research context only
    entry_low: float
    entry_high: float
    entry_reference_price: float
    entry_basis: str
    invalidation_price: float
    invalidation_basis: str
    target_prices: tuple[float, ...]
    as_of: datetime
    expiry_at: datetime
    dedupe_key: str
    strategy_version: str
    risk_model_version: str
    cost_model_version: str
    fee_schedule_version: str
    risk_config_hash: str
    cost_config_hash: str

    @property
    def bullish(self) -> bool:
        return self.direction == "BULLISH"


@dataclass(frozen=True)
class CandidateStateSnapshot:
    """Current phase-8 candidate lifecycle state (from persistence)."""

    candidate_id: uuid.UUID
    state: str
    expiry_at: datetime | None
    as_of: datetime | None


@dataclass(frozen=True)
class MarketStateSnapshot:
    """Fresh public market state for one evaluation cycle.

    ``last_price`` is intentionally absent as a decision basis - monitors
    use mark price, BBO and CLOSED candles only.
    """

    mark_price: float | None
    best_bid: float | None
    best_ask: float | None
    bbo_fresh: bool
    book_fresh: bool
    book_resyncing: bool
    last_closed_5m_close: float | None
    last_closed_5m_low: float | None
    last_closed_5m_high: float | None
    last_closed_5m_close_time: datetime | None
    data_quality_status: str
    data_quality_ok: bool
    snapshot_at: datetime | None


@dataclass(frozen=True)
class SignalSnapshot:
    """Immutable view of a persisted lifecycle signal for one cycle."""

    signal_id: uuid.UUID
    plan_id: uuid.UUID
    candidate_id: uuid.UUID
    state: SignalState
    state_version: int
    direction: str
    candidate_type: str
    instrument_pk: int
    instrument_id: int
    symbol: str
    asset_class: str
    entry_low: float
    entry_high: float
    entry_reference_price: float
    invalidation_price: float
    target_prices: tuple[float, ...]
    entry_trigger: EntryTriggerType
    expires_at: datetime
    watching_entry_at: datetime | None
    entry_confirmed_at: datetime | None
    data_degraded_since: datetime | None
    dedupe_key: str
    correlation_id: str

    @property
    def bullish(self) -> bool:
        return self.direction == "BULLISH"


@dataclass(frozen=True)
class MonitorContext:
    """Immutable inputs for one lifecycle evaluation cycle."""

    signal: SignalSnapshot
    plan: PlanSnapshot | None  # None when the plan row disappeared
    candidate: CandidateStateSnapshot | None
    market: MarketStateSnapshot
    session_state: str
    session_allowed: bool
    watchlist_active: bool
    market_quality_score: int | None
    bot_paused: bool
    #: confirmed opposing structure signatures observed after entry
    opposing_structure_events: tuple[str, ...]
    #: a newer ELIGIBLE plan replacing this signal's plan context, if any
    superseding_plan_id: uuid.UUID | None
    as_of: datetime
    data_timestamps: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MonitorFinding:
    """One monitor's proposal: a state transition and/or observations."""

    monitor: str
    priority_key: str  # key into the configured priority map
    to_state: SignalState | None
    event_type: SignalEventType | None
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)
    updates: tuple[tuple[SignalUpdateType, str], ...] = ()
    approximation_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransitionStep:
    """One validated step of a transition chain (e.g. ENTRY -> ACTIVE)."""

    from_state: SignalState
    to_state: SignalState
    event_type: SignalEventType
    reason: str
    priority: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineResult:
    """Deterministic outcome of one evaluation cycle.

    At most ONE final state per cycle; ``transitions`` may contain the
    documented two-step entry chain (ENTRY_CONFIRMED -> ACTIVE_RESEARCH).
    Non-conflicting observations are captured as updates/metadata.
    """

    signal_id: uuid.UUID
    transitions: tuple[TransitionStep, ...]
    updates: tuple[tuple[SignalUpdateType, str], ...]
    observed: tuple[str, ...]
    suppressed_findings: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def final_state(self) -> SignalState | None:
        return self.transitions[-1].to_state if self.transitions else None


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    code: SignalRejectionCode | None
    detail: str
    trigger: EntryTriggerType | None = None
    approximation_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SignalRejection:
    """Structured admission/lifecycle rejection (aggregated persistence)."""

    rejection_id: uuid.UUID
    plan_id: uuid.UUID | None
    candidate_id: uuid.UUID | None
    signal_id: uuid.UUID | None
    instrument_pk: int
    symbol: str
    as_of: datetime
    primary_code: SignalRejectionCode
    codes: tuple[SignalRejectionCode, ...]
    detail: str
    lifecycle_model_version: str
    lifecycle_config_hash: str
