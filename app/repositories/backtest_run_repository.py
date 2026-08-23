"""Backtest-specific run state, checkpoints and walk-forward splits."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import BacktestRunRecord, WalkForwardSplitRecord
from app.simulation.models import DataCoverageReport
from app.simulation.walk_forward import SelectionResult, WalkForwardSplit


class BacktestRunRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        run_id: uuid.UUID,
        status: str,
        ordering_version: str,
        clock_version: str,
    ) -> bool:
        now = datetime.now(tz=UTC)
        row = BacktestRunRecord(
            id=uuid.uuid4(),
            run_id=run_id,
            run_status=status,
            replay_ordering_version=ordering_version,
            replay_clock_version=clock_version,
            data_complete=False,
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

    async def record_coverage(self, run_id: uuid.UUID, report: DataCoverageReport) -> None:
        async with self._session_factory() as session:
            await session.execute(
                sa.update(BacktestRunRecord)
                .where(BacktestRunRecord.run_id == run_id)
                .values(
                    data_complete=report.complete,
                    missing_channels={"channels": list(report.missing_channels)},
                    gap_intervals={
                        "intervals": [
                            {
                                "symbol": gap.symbol,
                                "channel": gap.channel,
                                "start_at": gap.start_at.isoformat(),
                                "end_at": gap.end_at.isoformat(),
                                "detail": gap.detail,
                            }
                            for gap in report.gaps
                        ]
                    },
                    detail=report.detail,
                    updated_at=datetime.now(tz=UTC),
                )
            )
            await session.commit()

    async def save_checkpoint(
        self,
        run_id: uuid.UUID,
        *,
        checkpoint: dict[str, Any],
        events_replayed: int,
        last_event_at: datetime | None,
        status: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "checkpoint": checkpoint,
            "events_replayed": events_replayed,
            "last_event_at": last_event_at,
            "updated_at": datetime.now(tz=UTC),
        }
        if status is not None:
            values["run_status"] = status
        async with self._session_factory() as session:
            await session.execute(
                sa.update(BacktestRunRecord)
                .where(BacktestRunRecord.run_id == run_id)
                .values(**values)
            )
            await session.commit()

    async def get(self, run_id: uuid.UUID) -> BacktestRunRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(BacktestRunRecord).where(BacktestRunRecord.run_id == run_id)
                )
            ).scalar_one_or_none()

    async def add_split(
        self,
        *,
        run_id: uuid.UUID,
        split: WalkForwardSplit,
        selection: SelectionResult,
        candidates: tuple[str, ...],
    ) -> bool:
        row = WalkForwardSplitRecord(
            id=uuid.uuid4(),
            run_id=run_id,
            split_index=split.index,
            mode=split.mode.value,
            train_start_at=split.train.start_at,
            train_end_at=split.train.end_at,
            validation_start_at=split.validation.start_at,
            validation_end_at=split.validation.end_at,
            test_start_at=split.test.start_at,
            test_end_at=split.test.end_at,
            selected_configuration=selection.selected_key,
            sample_status=selection.sample_status.value,
            selection_rationale=selection.rationale,
            candidates={"candidates": list(candidates)},
            scores={"scores": [list(item) for item in selection.scores]},
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def splits_for(self, run_id: uuid.UUID) -> list[WalkForwardSplitRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(WalkForwardSplitRecord)
                .where(WalkForwardSplitRecord.run_id == run_id)
                .order_by(WalkForwardSplitRecord.split_index)
            )
            return list(rows.scalars().all())
