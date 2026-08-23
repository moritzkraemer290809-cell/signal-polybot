"""Experiments and their immutable manifests.

Manifests are write-once: an existing content hash is never rewritten, so
a changed configuration always becomes a NEW manifest and a NEW run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import ExperimentManifestRecord, ExperimentRecord
from app.simulation.experiment_manifest import Experiment, ExperimentManifest


class ExperimentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_experiment(self, experiment: Experiment) -> bool:
        row = ExperimentRecord(
            id=experiment.experiment_id,
            name=experiment.name,
            description=experiment.description,
            status=experiment.status,
            created_by=experiment.created_by,
            tags={"tags": list(experiment.tags)},
            detail=dict(experiment.metadata),
            created_at=experiment.created_at,
            updated_at=experiment.created_at,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def set_status(self, experiment_id: uuid.UUID, status: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                sa.update(ExperimentRecord)
                .where(ExperimentRecord.id == experiment_id)
                .values(status=status, updated_at=datetime.now(tz=UTC))
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0

    async def add_manifest(self, manifest: ExperimentManifest) -> bool:
        """Persist an immutable manifest (duplicate content hash = no-op)."""
        manifest.require_complete()
        payload = manifest.payload
        row = ExperimentManifestRecord(
            id=manifest.manifest_id,
            experiment_id=manifest.experiment_id,
            run_type=manifest.run_type.value,
            schema_version=manifest.schema_version,
            content_hash=manifest.content_hash,
            payload=payload,
            simulation_model_version=str(payload["simulation_model_version"]),
            simulation_configuration_hash=str(payload["simulation_configuration_hash"]),
            strategy_version=str(payload["strategy_version"]),
            risk_model_version=str(payload["risk_model_version"]),
            cost_model_version=str(payload["cost_model_version"]),
            lifecycle_model_version=str(payload["lifecycle_model_version"]),
            fee_schedule_version=str(payload["fee_schedule_version"]),
            execution_assumption_version=str(payload["execution_assumption_version"]),
            replay_ordering_version=str(
                payload["replay_ordering"].get("ordering_version", "rov-1")
            ),
            replay_clock_version=str(payload["replay_clock_version"]),
            metrics_version=str(payload["metrics_version"]),
            disclaimer_version=str(payload["disclaimer_version"]),
            delay_model=str(payload.get("delay_model", "FIXED_SECONDS")),
            random_seed=payload.get("random_seed"),
            start_at=datetime.fromisoformat(str(payload["start_at"])),
            end_at=datetime.fromisoformat(str(payload["end_at"])),
            created_at=manifest.created_at,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False  # identical manifest already recorded

    async def get_manifest(self, manifest_id: uuid.UUID) -> ExperimentManifestRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(ExperimentManifestRecord).where(
                        ExperimentManifestRecord.id == manifest_id
                    )
                )
            ).scalar_one_or_none()

    async def manifest_by_hash(self, content_hash: str) -> ExperimentManifestRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(ExperimentManifestRecord).where(
                        ExperimentManifestRecord.content_hash == content_hash
                    )
                )
            ).scalar_one_or_none()

    async def get(self, experiment_id: uuid.UUID) -> ExperimentRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(ExperimentRecord).where(ExperimentRecord.id == experiment_id)
                )
            ).scalar_one_or_none()

    async def recent(self, limit: int = 20) -> list[ExperimentRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(ExperimentRecord)
                .order_by(ExperimentRecord.created_at.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def manifests_for(self, experiment_id: uuid.UUID) -> list[ExperimentManifestRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(ExperimentManifestRecord)
                .where(ExperimentManifestRecord.experiment_id == experiment_id)
                .order_by(ExperimentManifestRecord.created_at)
            )
            return list(rows.scalars().all())

    async def counts_by_status(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(ExperimentRecord.status, sa.func.count()).group_by(
                    ExperimentRecord.status
                )
            )
            return {status: int(count) for status, count in rows.all()}

    async def manifest_payload(self, manifest_id: uuid.UUID) -> dict[str, Any] | None:
        row = await self.get_manifest(manifest_id)
        return dict(row.payload) if row is not None else None
