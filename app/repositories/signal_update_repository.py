"""Aggregated signal update persistence (observation stream)."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import SignalUpdateRecord


class SignalUpdateRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_update(self, update: dict[str, Any]) -> bool:
        """Insert or aggregate.  Repetitive updates inside the dedupe window
        share an idempotency key and only increment the counter."""
        async with self._session_factory() as session:
            session.add(
                SignalUpdateRecord(
                    signal_id=update["signal_id"],
                    update_type=update["update_type"],
                    idempotency_key=update["idempotency_key"],
                    detail=update["detail"],
                    count=1,
                    first_as_of=update["as_of"],
                    last_as_of=update["as_of"],
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
            row = (
                await session.execute(
                    sa.select(SignalUpdateRecord).where(
                        SignalUpdateRecord.idempotency_key == update["idempotency_key"]
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.count += 1
                row.last_as_of = update["as_of"]
                await session.commit()
            return False

    async def updates_for(self, signal_id: uuid.UUID) -> list[SignalUpdateRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SignalUpdateRecord)
                .where(SignalUpdateRecord.signal_id == signal_id)
                .order_by(SignalUpdateRecord.last_as_of, SignalUpdateRecord.id)
            )
            return list(rows.scalars().all())

    async def recent(self, limit: int = 30) -> list[SignalUpdateRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SignalUpdateRecord)
                .order_by(SignalUpdateRecord.last_as_of.desc(), SignalUpdateRecord.id.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())
