"""Persistence and lifecycle for signal eligibility plans.

Current state lives in ``risk_plans`` (one row per plan), the immutable
lifecycle history in ``risk_plan_events``.  The ``active_key`` unique
constraint (dedupe_key while ELIGIBLE, NULL otherwise) prevents duplicate
active plans even under race conditions - the phase-8 candidate pattern.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import RiskPlanEvent, RiskPlanRecord
from app.risk.enums import PlanStatus
from app.risk.explainability import dashboard_plan_details
from app.risk.models import SignalEligibilityPlan

ACTIVE_STATES = (PlanStatus.ELIGIBLE.value,)


def _now() -> datetime:
    return datetime.now(tz=UTC)


class RiskPlanRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ---------------------------------------------------------------- write

    async def insert_new(
        self, plan: SignalEligibilityPlan, *, instrument_snapshot_id: uuid.UUID | None = None
    ) -> bool:
        """Insert a new plan.  Returns False when an active plan with the
        same dedupe key already exists (unique active_key)."""
        primary = plan.costs.primary_target
        row = RiskPlanRecord(
            id=plan.plan_id,
            candidate_id=plan.candidate_id,
            instrument_pk=plan.instrument_pk,
            instrument_id=plan.instrument_id,
            symbol=plan.symbol,
            asset_class=plan.asset_class,
            candidate_type=plan.candidate_type,
            direction=plan.direction,
            status=plan.status.value,
            eligibility_score=plan.eligibility_score,
            net_rr_primary=(
                primary.net_rr if primary is not None and primary.net_rr is not None else None
            ),
            cost_to_risk_pct=plan.costs.cost_to_risk_pct,
            risk_model_name=plan.risk_model_name,
            risk_model_version=plan.risk_model_version,
            risk_config_hash=plan.risk_config_hash,
            cost_model_version=plan.cost_model_version,
            cost_config_hash=plan.cost_config_hash,
            fee_schedule_version=plan.fee_schedule_version,
            execution_assumption_version=plan.execution_assumption_version,
            instrument_snapshot_id=instrument_snapshot_id,
            instrument_snapshot_version=plan.instrument_snapshot_version,
            strategy_version=plan.strategy_version,
            strategy_config_hash=plan.strategy_config_hash,
            payload=dashboard_plan_details(plan),
            dedupe_key=plan.dedupe_key,
            active_key=plan.dedupe_key if plan.status.value in ACTIVE_STATES else None,
            as_of=plan.as_of,
            expiry_at=plan.expiry_at,
            created_at=plan.as_of,
            updated_at=plan.as_of,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            session.add(
                RiskPlanEvent(
                    plan_id=plan.plan_id,
                    event_type=plan.status.value,
                    from_status=None,
                    to_status=plan.status.value,
                    detail={"eligibility_score": plan.eligibility_score},
                )
            )
            await session.commit()
        return True

    async def transition(
        self,
        plan_id: uuid.UUID,
        new_status: PlanStatus,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        """Transition a plan; history preserved in events.  Non-active
        states clear active_key so the dedupe slot frees up."""
        async with self._session_factory() as session:
            row = (
                await session.execute(sa.select(RiskPlanRecord).where(RiskPlanRecord.id == plan_id))
            ).scalar_one_or_none()
            if row is None or row.status == new_status.value:
                return False
            from_status = row.status
            row.status = new_status.value
            row.updated_at = _now()
            if new_status.value not in ACTIVE_STATES:
                row.active_key = None
            session.add(
                RiskPlanEvent(
                    plan_id=plan_id,
                    event_type=new_status.value,
                    from_status=from_status,
                    to_status=new_status.value,
                    detail=detail,
                )
            )
            await session.commit()
        return True

    # ----------------------------------------------------------------- read

    async def active_by_dedupe_key(self, dedupe_key: str) -> RiskPlanRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(RiskPlanRecord).where(RiskPlanRecord.active_key == dedupe_key)
                )
            ).scalar_one_or_none()

    async def active_plans(self, instrument_pk: int | None = None) -> list[RiskPlanRecord]:
        query = sa.select(RiskPlanRecord).where(RiskPlanRecord.status.in_(ACTIVE_STATES))
        if instrument_pk is not None:
            query = query.where(RiskPlanRecord.instrument_pk == instrument_pk)
        async with self._session_factory() as session:
            rows = await session.execute(query.order_by(RiskPlanRecord.created_at))
            return list(rows.scalars().all())

    async def plans_for_candidate(self, candidate_id: uuid.UUID) -> list[RiskPlanRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(RiskPlanRecord)
                .where(RiskPlanRecord.candidate_id == candidate_id)
                .order_by(RiskPlanRecord.created_at)
            )
            return list(rows.scalars().all())

    async def recent(self, limit: int = 30) -> list[RiskPlanRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(RiskPlanRecord).order_by(RiskPlanRecord.updated_at.desc()).limit(limit)
            )
            return list(rows.scalars().all())

    async def events_for(self, plan_id: uuid.UUID) -> list[RiskPlanEvent]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(RiskPlanEvent)
                .where(RiskPlanEvent.plan_id == plan_id)
                .order_by(RiskPlanEvent.created_at, RiskPlanEvent.id)
            )
            return list(rows.scalars().all())

    async def counts_by_status(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(RiskPlanRecord.status, sa.func.count()).group_by(RiskPlanRecord.status)
            )
            return {status: int(count) for status, count in rows.all()}
