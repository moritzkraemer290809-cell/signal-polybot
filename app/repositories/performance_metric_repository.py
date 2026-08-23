"""Persistence of hypothetical metric sets and their values."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import PerformanceMetricSetRecord, PerformanceMetricValueRecord
from app.simulation.models import MetricSet
from app.simulation.version import metric_set_key


class PerformanceMetricRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_metric_set(self, run_id: uuid.UUID, metric_set: MetricSet) -> bool:
        """Persist one segment's metrics; False when already recorded."""
        key = metric_set_key(
            run_id=str(run_id),
            segment_kind=metric_set.segment_kind,
            segment_key=metric_set.segment_key,
            metrics_version=metric_set.metrics_version,
        )
        set_id = uuid.uuid4()
        async with self._session_factory() as session:
            session.add(
                PerformanceMetricSetRecord(
                    id=set_id,
                    run_id=run_id,
                    set_key=key,
                    segment_kind=metric_set.segment_kind,
                    segment_key=metric_set.segment_key,
                    sample_status=metric_set.sample_status,
                    complete_simulations=metric_set.complete_simulations,
                    metrics_version=metric_set.metrics_version,
                    disclaimer_version=metric_set.disclaimer_version,
                    warnings={"warnings": list(metric_set.warnings)},
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            session.add_all(
                [
                    PerformanceMetricValueRecord(
                        metric_set_id=set_id,
                        name=value.name,
                        value=(
                            None
                            if value.value is None
                            else Decimal(str(round(float(value.value), 10)))
                        ),
                        unit=value.unit,
                        detail=value.detail or None,
                    )
                    for value in metric_set.values
                ]
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
        return True

    async def add_metric_sets(self, run_id: uuid.UUID, metric_sets: Sequence[MetricSet]) -> int:
        written = 0
        for metric_set in metric_sets:
            if await self.add_metric_set(run_id, metric_set):
                written += 1
        return written

    async def sets_for_run(self, run_id: uuid.UUID) -> list[PerformanceMetricSetRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(PerformanceMetricSetRecord)
                .where(PerformanceMetricSetRecord.run_id == run_id)
                .order_by(
                    PerformanceMetricSetRecord.segment_kind,
                    PerformanceMetricSetRecord.segment_key,
                )
            )
            return list(rows.scalars().all())

    async def values_for_set(self, metric_set_id: uuid.UUID) -> list[PerformanceMetricValueRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(PerformanceMetricValueRecord)
                .where(PerformanceMetricValueRecord.metric_set_id == metric_set_id)
                .order_by(PerformanceMetricValueRecord.name)
            )
            return list(rows.scalars().all())

    async def insufficient_sample_count(self) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count()).where(
                    PerformanceMetricSetRecord.sample_status == "INSUFFICIENT_SAMPLE"
                )
            )
            return int(value.scalar_one())
