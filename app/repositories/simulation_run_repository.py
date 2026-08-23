"""Shadow and backtest run state.

A completed run is never deleted or overwritten: progress fields advance
while the run is active, and terminal runs are read-only history.  The
dedupe key blocks a second concurrent run of the same manifest context.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import SimulationRunRecord
from app.simulation.enums import BacktestRunStatus, RunType, SimulationRunStatus

_TERMINAL = {
    SimulationRunStatus.COMPLETED.value,
    SimulationRunStatus.INCOMPLETE.value,
    SimulationRunStatus.REJECTED.value,
    SimulationRunStatus.FAILED.value,
    SimulationRunStatus.CANCELLED.value,
    BacktestRunStatus.COMPLETED.value,
    BacktestRunStatus.COMPLETED_WITH_GAPS.value,
}


class SimulationRunRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_run(
        self,
        *,
        run_id: uuid.UUID,
        experiment_id: uuid.UUID,
        manifest_id: uuid.UUID,
        run_type: RunType,
        status: str,
        dedupe_key: str,
        start_at: datetime,
        end_at: datetime,
        created_by: str = "system",
    ) -> bool:
        """Insert a run; False when the same manifest context is running."""
        now = datetime.now(tz=UTC)
        row = SimulationRunRecord(
            id=run_id,
            experiment_id=experiment_id,
            manifest_id=manifest_id,
            run_type=run_type.value,
            run_status=status,
            dedupe_key=dedupe_key,
            start_at=start_at,
            end_at=end_at,
            started_at=now,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def update_progress(
        self,
        run_id: uuid.UUID,
        *,
        status: str | None = None,
        events_replayed: int | None = None,
        checkpoint: dict[str, Any] | None = None,
        counts: dict[str, int] | None = None,
        warnings: tuple[str, ...] | None = None,
        excluded_intervals: list[dict[str, Any]] | None = None,
        data_quality_summary: dict[str, Any] | None = None,
        detail: str | None = None,
        completed: bool = False,
    ) -> bool:
        values: dict[str, Any] = {"updated_at": datetime.now(tz=UTC)}
        if status is not None:
            values["run_status"] = status
        if events_replayed is not None:
            values["events_replayed"] = events_replayed
        if checkpoint is not None:
            values["checkpoint"] = checkpoint
        if counts is not None:
            values.update(
                {
                    "simulations_total": counts.get("total", 0),
                    "simulations_completed": counts.get("completed", 0),
                    "simulations_incomplete": counts.get("incomplete", 0),
                    "simulations_rejected": counts.get("rejected", 0),
                }
            )
        if warnings is not None:
            values["warnings"] = {"warnings": list(warnings)}
        if excluded_intervals is not None:
            values["excluded_intervals"] = {"intervals": excluded_intervals}
        if data_quality_summary is not None:
            values["data_quality_summary"] = data_quality_summary
        if detail is not None:
            values["detail"] = detail
        if completed:
            values["completed_at"] = datetime.now(tz=UTC)
        async with self._session_factory() as session:
            result = await session.execute(
                sa.update(SimulationRunRecord)
                .where(
                    SimulationRunRecord.id == run_id,
                    # terminal runs are immutable history
                    SimulationRunRecord.run_status.not_in(tuple(_TERMINAL)),
                )
                .values(**values)
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0

    async def get(self, run_id: uuid.UUID) -> SimulationRunRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(SimulationRunRecord).where(SimulationRunRecord.id == run_id)
                )
            ).scalar_one_or_none()

    async def active_runs(self, run_type: RunType | None = None) -> list[SimulationRunRecord]:
        query = sa.select(SimulationRunRecord).where(
            SimulationRunRecord.run_status.not_in(tuple(_TERMINAL))
        )
        if run_type is not None:
            query = query.where(SimulationRunRecord.run_type == run_type.value)
        async with self._session_factory() as session:
            rows = await session.execute(query.order_by(SimulationRunRecord.created_at))
            return list(rows.scalars().all())

    async def run_by_dedupe_key(self, dedupe_key: str) -> SimulationRunRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(SimulationRunRecord).where(
                        SimulationRunRecord.dedupe_key == dedupe_key
                    )
                )
            ).scalar_one_or_none()

    async def recent(self, limit: int = 20) -> list[SimulationRunRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SimulationRunRecord)
                .order_by(SimulationRunRecord.updated_at.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def counts_by_status(self, run_type: RunType | None = None) -> dict[str, int]:
        query = sa.select(SimulationRunRecord.run_status, sa.func.count()).group_by(
            SimulationRunRecord.run_status
        )
        if run_type is not None:
            query = query.where(SimulationRunRecord.run_type == run_type.value)
        async with self._session_factory() as session:
            rows = await session.execute(query)
            return {status: int(count) for status, count in rows.all()}

    async def last_completed(self, run_type: RunType) -> SimulationRunRecord | None:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SimulationRunRecord)
                .where(
                    SimulationRunRecord.run_type == run_type.value,
                    SimulationRunRecord.completed_at.is_not(None),
                )
                .order_by(SimulationRunRecord.completed_at.desc())
                .limit(1)
            )
            return rows.scalars().first()
