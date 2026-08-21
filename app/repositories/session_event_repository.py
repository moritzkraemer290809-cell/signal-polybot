"""Persistence for session state transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import SessionEvent


class SessionEventRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(
        self,
        scope: str,
        from_state: str | None,
        to_state: str,
        calendar_version: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                SessionEvent(
                    scope=scope,
                    from_state=from_state,
                    to_state=to_state,
                    calendar_version=calendar_version,
                    occurred_at=datetime.now(tz=UTC),
                    details=details,
                )
            )
            await session.commit()

    async def recent(self, limit: int = 20) -> list[SessionEvent]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SessionEvent)
                .order_by(SessionEvent.created_at.desc(), SessionEvent.id.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())
