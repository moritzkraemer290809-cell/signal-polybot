"""Lifecycle instance construction from an admitted plan."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config import SignalLifecycleSettings
from app.signals.enums import EntryTriggerType, SignalState
from app.signals.models import PlanSnapshot, SignalSnapshot
from app.signals.version import signal_dedupe_key


@dataclass(frozen=True)
class NewSignal:
    """Everything the repository needs to persist a freshly admitted signal.

    The technical reference prices are copied as IMMUTABLE snapshot fields -
    they are internal model values for monitoring, shown only on the local
    dashboard and never sent to Telegram in phase 10.
    """

    signal_id: uuid.UUID
    plan_id: uuid.UUID
    candidate_id: uuid.UUID
    state: SignalState
    instrument_pk: int
    instrument_id: int
    symbol: str
    asset_class: str
    candidate_type: str
    direction: str
    entry_low: float
    entry_high: float
    entry_reference_price: float
    invalidation_price: float
    target_prices: tuple[float, ...]
    entry_trigger: EntryTriggerType
    expires_at: datetime
    admission_at: datetime
    dedupe_key: str
    correlation_id: str
    lifecycle_model_name: str
    lifecycle_model_version: str
    lifecycle_config_hash: str
    strategy_version: str
    risk_model_version: str
    cost_model_version: str
    fee_schedule_version: str
    approximation_warnings: tuple[str, ...]
    reference_snapshot: dict[str, Any] = field(default_factory=dict)


def new_signal_from_plan(
    plan: PlanSnapshot,
    *,
    trigger: EntryTriggerType,
    settings: SignalLifecycleSettings,
    lifecycle_config_hash: str,
    approximation_flags: tuple[str, ...],
    now: datetime,
    correlation_id: str,
) -> NewSignal:
    # the plan window bounds the research window
    expires_at = plan.expiry_at
    return NewSignal(
        signal_id=uuid.uuid4(),
        plan_id=plan.plan_id,
        candidate_id=plan.candidate_id,
        state=SignalState.WATCHING_ENTRY,
        instrument_pk=plan.instrument_pk,
        instrument_id=plan.instrument_id,
        symbol=plan.symbol,
        asset_class=plan.asset_class,
        candidate_type=plan.candidate_type,
        direction=plan.direction,
        entry_low=plan.entry_low,
        entry_high=plan.entry_high,
        entry_reference_price=plan.entry_reference_price,
        invalidation_price=plan.invalidation_price,
        target_prices=plan.target_prices,
        entry_trigger=trigger,
        expires_at=expires_at,
        admission_at=now,
        dedupe_key=signal_dedupe_key(
            plan_id=str(plan.plan_id),
            candidate_id=str(plan.candidate_id),
            lifecycle_model_version=settings.lifecycle_model_version,
            lifecycle_config_hash=lifecycle_config_hash,
        ),
        correlation_id=correlation_id,
        lifecycle_model_name=settings.lifecycle_model_name,
        lifecycle_model_version=settings.lifecycle_model_version,
        lifecycle_config_hash=lifecycle_config_hash,
        strategy_version=plan.strategy_version,
        risk_model_version=plan.risk_model_version,
        cost_model_version=plan.cost_model_version,
        fee_schedule_version=plan.fee_schedule_version,
        approximation_warnings=approximation_flags,
        reference_snapshot={
            "label": "Interne Modell-Referenzwerte - keine Handelsanweisung",
            "entry_zone": [plan.entry_low, plan.entry_high],
            "entry_reference_price": plan.entry_reference_price,
            "entry_basis": plan.entry_basis,
            "invalidation_price": plan.invalidation_price,
            "invalidation_basis": plan.invalidation_basis,
            "target_prices": list(plan.target_prices),
            "entry_trigger": trigger.value,
            "plan_as_of": plan.as_of.isoformat(),
            "risk_config_hash": plan.risk_config_hash,
            "cost_config_hash": plan.cost_config_hash,
        },
    )


def snapshot_from_new_signal(new: NewSignal) -> SignalSnapshot:
    """In-memory snapshot for the first evaluation of a fresh signal."""
    return SignalSnapshot(
        signal_id=new.signal_id,
        plan_id=new.plan_id,
        candidate_id=new.candidate_id,
        state=new.state,
        state_version=2,  # after CREATED + ADMITTED/WATCHING chain
        direction=new.direction,
        candidate_type=new.candidate_type,
        instrument_pk=new.instrument_pk,
        instrument_id=new.instrument_id,
        symbol=new.symbol,
        asset_class=new.asset_class,
        entry_low=new.entry_low,
        entry_high=new.entry_high,
        entry_reference_price=new.entry_reference_price,
        invalidation_price=new.invalidation_price,
        target_prices=new.target_prices,
        entry_trigger=new.entry_trigger,
        expires_at=new.expires_at,
        watching_entry_at=new.admission_at,
        entry_confirmed_at=None,
        data_degraded_since=None,
        dedupe_key=new.dedupe_key,
        correlation_id=new.correlation_id,
    )
