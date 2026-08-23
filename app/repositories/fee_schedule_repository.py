"""Versioned fee schedule persistence (administered assumptions only).

Schedules are only ever written from administered configuration, seeds or
migrations - never from unverified runtime data sources.  Versions are
immutable: a changed schedule requires a new version string.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import ExecutionAssumptionVersion, FeeScheduleVersion


class FeeScheduleRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def seed(self, version: str, schedule: dict[str, Any], source: str) -> bool:
        """Insert a schedule version if missing.  Returns True when created.

        An existing version is never overwritten - immutability by design.
        """
        async with self._session_factory() as session:
            existing = (
                await session.execute(
                    sa.select(FeeScheduleVersion.id).where(FeeScheduleVersion.version == version)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return False
            session.add(
                FeeScheduleVersion(version=version, schedule=schedule, source=source, active=False)
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def activate(self, version: str) -> bool:
        """Activate exactly one schedule version."""
        async with self._session_factory() as session:
            target = (
                await session.execute(
                    sa.select(FeeScheduleVersion).where(FeeScheduleVersion.version == version)
                )
            ).scalar_one_or_none()
            if target is None:
                return False
            rows = await session.execute(
                sa.select(FeeScheduleVersion).where(FeeScheduleVersion.active.is_(True))
            )
            for row in rows.scalars():
                row.active = False
            target.active = True
            await session.commit()
            return True

    async def active_schedule(self) -> FeeScheduleVersion | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(FeeScheduleVersion)
                    .where(FeeScheduleVersion.active.is_(True))
                    .order_by(FeeScheduleVersion.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def list_versions(self) -> list[FeeScheduleVersion]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(FeeScheduleVersion).order_by(FeeScheduleVersion.created_at)
            )
            return list(rows.scalars().all())


class ExecutionAssumptionRepository:
    """Versioned execution assumptions - administered and immutable."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def seed_and_activate(self, version: str, assumptions: dict[str, Any]) -> None:
        """Insert the version if missing (never overwritten) and activate it."""
        async with self._session_factory() as session:
            existing = (
                await session.execute(
                    sa.select(ExecutionAssumptionVersion).where(
                        ExecutionAssumptionVersion.version == version
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    ExecutionAssumptionVersion(
                        version=version, assumptions=assumptions, active=False
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
            rows = await session.execute(sa.select(ExecutionAssumptionVersion))
            for row in rows.scalars():
                row.active = row.version == version
            await session.commit()

    async def active_version(self) -> ExecutionAssumptionVersion | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(ExecutionAssumptionVersion)
                    .where(ExecutionAssumptionVersion.active.is_(True))
                    .limit(1)
                )
            ).scalar_one_or_none()
