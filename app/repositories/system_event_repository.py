"""Persistence for system events (operational audit trail)."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import SystemEventLevel
from app.repositories.orm import SystemEvent


class SystemEventRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(
        self,
        level: SystemEventLevel,
        event_type: str,
        message: str,
        context: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                SystemEvent(
                    level=level.value,
                    event_type=event_type,
                    message=message,
                    context=context,
                    correlation_id=correlation_id,
                )
            )
            await session.commit()

    async def latest(self, limit: int = 50) -> list[SystemEvent]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(limit)
            )
            return list(rows.scalars().all())
