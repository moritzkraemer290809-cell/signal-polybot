"""Immutable, idempotent simulation audit events and rejections."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import SimulationEventRecord, SimulationRejectionRecord
from app.simulation.enums import SimulationEventType
from app.simulation.models import SimulationRejection


def event_idempotency_key(
    *,
    run_id: str,
    event_type: str,
    subject_id: str,
    marker: str = "",
) -> str:
    raw = "|".join([run_id, event_type, subject_id, marker])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _bucket(as_of: datetime, window_seconds: float) -> str:
    epoch = int(as_of.timestamp())
    return str(epoch - epoch % max(1, int(window_seconds)))


class SimulationEventRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_event(
        self,
        *,
        run_id: uuid.UUID,
        event_type: SimulationEventType,
        as_of: datetime,
        simulated_position_id: uuid.UUID | None = None,
        detail: dict[str, Any] | None = None,
        marker: str = "",
    ) -> bool:
        """Insert one audit event; False when it already exists (retry)."""
        row = SimulationEventRecord(
            run_id=run_id,
            simulated_position_id=simulated_position_id,
            event_type=event_type.value,
            idempotency_key=event_idempotency_key(
                run_id=str(run_id),
                event_type=event_type.value,
                subject_id=str(simulated_position_id or "-"),
                marker=marker,
            ),
            detail=detail,
            as_of=as_of,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def events_for(self, run_id: uuid.UUID) -> list[SimulationEventRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SimulationEventRecord)
                .where(SimulationEventRecord.run_id == run_id)
                .order_by(SimulationEventRecord.as_of, SimulationEventRecord.id)
            )
            return list(rows.scalars().all())

    async def count(self) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count()).select_from(SimulationEventRecord)
            )
            return int(value.scalar_one())


class SimulationRejectionRepository:
    """Aggregated rejection history (never a per-attempt row flood)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_rejection(self, rejection: SimulationRejection, window_seconds: float) -> bool:
        bucket = _bucket(rejection.as_of, window_seconds)
        context_key = str(
            rejection.lifecycle_signal_id or rejection.plan_id or rejection.run_id or "-"
        )
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(SimulationRejectionRecord).where(
                        SimulationRejectionRecord.context_key == context_key,
                        SimulationRejectionRecord.primary_code == rejection.primary_code.value,
                        SimulationRejectionRecord.window_bucket == bucket,
                        SimulationRejectionRecord.simulation_model_version
                        == rejection.simulation_model_version,
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.count += 1
                row.last_as_of = rejection.as_of
                row.detail = rejection.detail
                await session.commit()
                return False
            session.add(
                SimulationRejectionRecord(
                    run_id=rejection.run_id,
                    lifecycle_signal_id=rejection.lifecycle_signal_id,
                    plan_id=rejection.plan_id,
                    candidate_id=rejection.candidate_id,
                    context_key=context_key,
                    symbol=rejection.symbol,
                    primary_code=rejection.primary_code.value,
                    codes={"codes": [code.value for code in rejection.codes]},
                    detail=rejection.detail,
                    count=1,
                    first_as_of=rejection.as_of,
                    last_as_of=rejection.as_of,
                    window_bucket=bucket,
                    simulation_model_version=rejection.simulation_model_version,
                    simulation_config_hash=rejection.simulation_config_hash,
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def recent(self, limit: int = 30) -> list[SimulationRejectionRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SimulationRejectionRecord)
                .order_by(
                    SimulationRejectionRecord.last_as_of.desc(),
                    SimulationRejectionRecord.id.desc(),
                )
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def counts_by_code(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(
                    SimulationRejectionRecord.primary_code,
                    sa.func.sum(SimulationRejectionRecord.count),
                ).group_by(SimulationRejectionRecord.primary_code)
            )
            return {code: int(total) for code, total in rows.all()}

    async def count_since(self, since: datetime) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(SimulationRejectionRecord.count), 0)).where(
                    SimulationRejectionRecord.last_as_of >= since
                )
            )
            return int(value.scalar_one())
