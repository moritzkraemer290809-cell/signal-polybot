"""Immutable, idempotent signal lifecycle event persistence."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import SignalLifecycleEvent


class SignalEventRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_event(self, event: dict[str, Any]) -> bool:
        """Insert one event.  Returns False when the idempotency key already
        exists (retry/restart) - never a duplicate row."""
        async with self._session_factory() as session:
            session.add(SignalLifecycleEvent(**event))
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def events_for(self, signal_id: uuid.UUID) -> list[SignalLifecycleEvent]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SignalLifecycleEvent)
                .where(SignalLifecycleEvent.signal_id == signal_id)
                .order_by(SignalLifecycleEvent.state_version, SignalLifecycleEvent.id)
            )
            return list(rows.scalars().all())

    async def count(self) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count()).select_from(SignalLifecycleEvent)
            )
            return int(value.scalar_one())
