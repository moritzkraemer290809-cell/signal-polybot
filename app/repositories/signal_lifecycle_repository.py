"""Persistence and lifecycle for internal research signals.

Current state lives in ``signal_lifecycles`` (one row per signal), the
immutable history in ``signal_lifecycle_events``.  Concurrency safety:

- ``active_key`` unique constraint: at most one non-terminal signal per
  plan/config dedupe context, even under races.
- ``state_version`` optimistic locking: a transition only applies when the
  row still carries the version the decision was computed against; a lost
  race returns CONFLICT and the higher-priority winner's state stands.
- lease columns: monitor workers claim a signal before evaluating it;
  expired leases are reclaimable (crash recovery).

A transition whose row update cannot be persisted is never reported as
successful.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import SignalLifecycleEvent, SignalLifecycleRecord
from app.signals.enums import SignalEventType, SignalState
from app.signals.idempotency import event_idempotency_key
from app.signals.lifecycle import NewSignal
from app.signals.models import TransitionStep
from app.signals.state_machine import TERMINAL_STATES

_TERMINAL_VALUES = tuple(state.value for state in TERMINAL_STATES)


class TransitionOutcome(StrEnum):
    APPLIED = "APPLIED"
    CONFLICT = "CONFLICT"  # optimistic lock lost - someone else transitioned
    NOT_FOUND = "NOT_FOUND"
    FAILED = "FAILED"


def _now() -> datetime:
    return datetime.now(tz=UTC)


class SignalLifecycleRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ---------------------------------------------------------------- create

    async def insert_new(self, new: NewSignal, event_schema_version: str) -> bool:
        """Persist a freshly admitted signal with its creation event chain.

        Returns False when an active signal for the same dedupe context
        already exists (unique active_key)."""
        row = SignalLifecycleRecord(
            id=new.signal_id,
            plan_id=new.plan_id,
            candidate_id=new.candidate_id,
            instrument_pk=new.instrument_pk,
            instrument_id=new.instrument_id,
            symbol=new.symbol,
            asset_class=new.asset_class,
            candidate_type=new.candidate_type,
            direction=new.direction,
            state=new.state.value,
            state_version=2,
            entry_low=new.entry_low,
            entry_high=new.entry_high,
            entry_reference_price=new.entry_reference_price,
            invalidation_price=new.invalidation_price,
            target_prices={"targets": list(new.target_prices)},
            entry_trigger=new.entry_trigger.value,
            admission_at=new.admission_at,
            watching_entry_at=new.admission_at,
            expires_at=new.expires_at,
            last_event_type=SignalEventType.WATCHING_ENTRY.value,
            reference_snapshot=new.reference_snapshot,
            approximation_warnings={"flags": list(new.approximation_warnings)},
            lifecycle_model_name=new.lifecycle_model_name,
            lifecycle_model_version=new.lifecycle_model_version,
            lifecycle_config_hash=new.lifecycle_config_hash,
            strategy_version=new.strategy_version,
            risk_model_version=new.risk_model_version,
            cost_model_version=new.cost_model_version,
            fee_schedule_version=new.fee_schedule_version,
            dedupe_key=new.dedupe_key,
            active_key=new.dedupe_key,
            correlation_id=new.correlation_id,
            created_at=new.admission_at,
            updated_at=new.admission_at,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            chain = (
                (SignalEventType.CREATED, None, SignalState.DRAFT, 0),
                (SignalEventType.ADMITTED, SignalState.DRAFT, SignalState.PENDING_ADMISSION, 1),
                (
                    SignalEventType.WATCHING_ENTRY,
                    SignalState.PENDING_ADMISSION,
                    SignalState.WATCHING_ENTRY,
                    2,
                ),
            )
            for event_type, from_state, to_state, version in chain:
                session.add(
                    SignalLifecycleEvent(
                        signal_id=new.signal_id,
                        event_type=event_type.value,
                        from_state=from_state.value if from_state else None,
                        to_state=to_state.value,
                        state_version=version,
                        priority=None,
                        idempotency_key=event_idempotency_key(
                            signal_id=str(new.signal_id),
                            event_type=event_type.value,
                            to_state=to_state.value,
                            expected_state_version=version - 1,
                        ),
                        detail={
                            "reason": "signal admitted from eligible plan",
                            "schema": event_schema_version,
                            "correlation_id": new.correlation_id,
                        },
                        as_of=new.admission_at,
                    )
                )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()  # events already recorded (retry)
        return True

    # ------------------------------------------------------------ transition

    async def apply_transition(
        self,
        signal_id: uuid.UUID,
        expected_state_version: int,
        step: TransitionStep,
        event: dict[str, Any],
        *,
        as_of: datetime,
    ) -> TransitionOutcome:
        """Optimistically apply one transition and its idempotent event."""
        terminal = step.to_state.value in _TERMINAL_VALUES
        values: dict[str, Any] = {
            "state": step.to_state.value,
            "state_version": expected_state_version + 1,
            "last_event_type": step.event_type.value,
            "updated_at": as_of,
        }
        if terminal:
            values["terminal_at"] = as_of
            values["active_key"] = None
        if step.to_state is SignalState.ENTRY_CONFIRMED:
            values["entry_confirmed_at"] = as_of
        if step.to_state is SignalState.ACTIVE_RESEARCH:
            values["active_research_at"] = as_of
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    sa.update(SignalLifecycleRecord)
                    .where(
                        SignalLifecycleRecord.id == signal_id,
                        SignalLifecycleRecord.state_version == expected_state_version,
                        SignalLifecycleRecord.state == step.from_state.value,
                    )
                    .values(**values)
                )
                await session.commit()
                if int(getattr(result, "rowcount", 0) or 0) == 0:
                    exists = (
                        await session.execute(
                            sa.select(SignalLifecycleRecord.id).where(
                                SignalLifecycleRecord.id == signal_id
                            )
                        )
                    ).scalar_one_or_none()
                    return (
                        TransitionOutcome.CONFLICT
                        if exists is not None
                        else TransitionOutcome.NOT_FOUND
                    )
                session.add(SignalLifecycleEvent(**event))
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()  # idempotent retry - event exists
                return TransitionOutcome.APPLIED
        except Exception:
            return TransitionOutcome.FAILED

    # --------------------------------------------------------------- leases

    async def claim(
        self, signal_id: uuid.UUID, owner: str, now: datetime, lease_seconds: float
    ) -> bool:
        """Atomically claim a signal for monitoring; reclaims expired leases."""
        async with self._session_factory() as session:
            result = await session.execute(
                sa.update(SignalLifecycleRecord)
                .where(
                    SignalLifecycleRecord.id == signal_id,
                    sa.or_(
                        SignalLifecycleRecord.lease_owner.is_(None),
                        SignalLifecycleRecord.lease_expires_at < now,
                        SignalLifecycleRecord.lease_owner == owner,
                    ),
                )
                .values(
                    lease_owner=owner,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0

    async def release(self, signal_id: uuid.UUID, owner: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                sa.update(SignalLifecycleRecord)
                .where(
                    SignalLifecycleRecord.id == signal_id,
                    SignalLifecycleRecord.lease_owner == owner,
                )
                .values(lease_owner=None, lease_expires_at=None)
            )
            await session.commit()

    async def expired_lease_count(self, now: datetime) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count()).where(
                    SignalLifecycleRecord.lease_owner.is_not(None),
                    SignalLifecycleRecord.lease_expires_at < now,
                )
            )
            return int(value.scalar_one())

    # ----------------------------------------------------------- monitoring

    async def update_monitoring_fields(
        self,
        signal_id: uuid.UUID,
        *,
        last_evaluated_at: datetime,
        last_market_data_at: datetime | None,
        data_quality_status: str,
        session_state: str,
        data_degraded_since: datetime | None,
        clear_degraded: bool,
    ) -> None:
        values: dict[str, Any] = {
            "last_evaluated_at": last_evaluated_at,
            "last_market_data_at": last_market_data_at,
            "last_data_quality_status": data_quality_status,
            "last_session_state": session_state,
        }
        if clear_degraded:
            values["data_degraded_since"] = None
        elif data_degraded_since is not None:
            values["data_degraded_since"] = data_degraded_since
        async with self._session_factory() as session:
            await session.execute(
                sa.update(SignalLifecycleRecord)
                .where(SignalLifecycleRecord.id == signal_id)
                .values(**values)
            )
            await session.commit()

    # ----------------------------------------------------------------- read

    async def get(self, signal_id: uuid.UUID) -> SignalLifecycleRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(SignalLifecycleRecord).where(SignalLifecycleRecord.id == signal_id)
                )
            ).scalar_one_or_none()

    async def non_terminal(self) -> list[SignalLifecycleRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SignalLifecycleRecord)
                .where(SignalLifecycleRecord.state.not_in(_TERMINAL_VALUES))
                .order_by(SignalLifecycleRecord.created_at)
            )
            return list(rows.scalars().all())

    async def active_by_dedupe_key(self, dedupe_key: str) -> SignalLifecycleRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(SignalLifecycleRecord).where(
                        SignalLifecycleRecord.active_key == dedupe_key
                    )
                )
            ).scalar_one_or_none()

    async def active_count_for_instrument(self, instrument_pk: int) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count()).where(
                    SignalLifecycleRecord.instrument_pk == instrument_pk,
                    SignalLifecycleRecord.state.not_in(_TERMINAL_VALUES),
                )
            )
            return int(value.scalar_one())

    async def signal_exists_for_plan(self, plan_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count()).where(SignalLifecycleRecord.plan_id == plan_id)
            )
            return int(value.scalar_one()) > 0

    async def counts_by_state(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SignalLifecycleRecord.state, sa.func.count()).group_by(
                    SignalLifecycleRecord.state
                )
            )
            return {state: int(count) for state, count in rows.all()}

    async def recent(self, limit: int = 20) -> list[SignalLifecycleRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SignalLifecycleRecord)
                .order_by(SignalLifecycleRecord.updated_at.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())
