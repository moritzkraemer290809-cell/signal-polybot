"""Persistence for signals (minimal API for phase <= 4: status reporting)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.lifecycle import OPEN_STATES
from app.repositories.orm import Signal


class SignalRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def count_open(self) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count())
                .select_from(Signal)
                .where(Signal.status.in_([state.value for state in OPEN_STATES]))
            )
            return int(value.scalar_one())

    async def list_open(self) -> list[Signal]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(Signal)
                .where(Signal.status.in_([state.value for state in OPEN_STATES]))
                .order_by(Signal.created_at)
            )
            return list(rows.scalars().all())
