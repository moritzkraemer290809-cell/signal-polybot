"""Persistence for strategy decisions (sent and rejected setups)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import StrategyDecision


class DecisionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, decision: StrategyDecision) -> None:
        async with self._session_factory() as session:
            session.add(decision)
            await session.commit()

    async def latest_for_instrument(
        self, instrument_pk: int, limit: int = 20
    ) -> list[StrategyDecision]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(StrategyDecision)
                .where(StrategyDecision.instrument_pk == instrument_pk)
                .order_by(StrategyDecision.created_at.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())
