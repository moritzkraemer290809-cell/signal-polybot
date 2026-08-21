"""Aggregated setup rejection persistence.

Repetitive "no setup" outcomes are aggregated per
(instrument, code, candidate_type, time bucket) so the database never floods
with per-candle rejection rows; counts and first/last timestamps are kept."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import SetupRejectionRecord
from app.strategy.models import SetupRejection


def _bucket(as_of: datetime, window_seconds: float) -> str:
    epoch = int(as_of.timestamp())
    return str(epoch - epoch % max(1, int(window_seconds)))


class StrategyDecisionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_rejection(self, rejection: SetupRejection, window_seconds: float) -> bool:
        """Insert or aggregate a rejection.  Returns True for a new row."""
        bucket = _bucket(rejection.as_of, window_seconds)
        candidate_type = rejection.candidate_type.value if rejection.candidate_type else ""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(SetupRejectionRecord).where(
                        SetupRejectionRecord.instrument_pk == rejection.instrument_pk,
                        SetupRejectionRecord.primary_code == rejection.primary_code.value,
                        SetupRejectionRecord.candidate_type == candidate_type,
                        SetupRejectionRecord.window_bucket == bucket,
                        SetupRejectionRecord.strategy_version == rejection.strategy_version,
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.count += 1
                row.last_as_of = rejection.as_of
                row.detail = rejection.detail
                await session.commit()
                return False
            session.add(
                SetupRejectionRecord(
                    instrument_pk=rejection.instrument_pk,
                    symbol=rejection.symbol,
                    primary_code=rejection.primary_code.value,
                    candidate_type=candidate_type,
                    codes={"codes": [code.value for code in rejection.codes]},
                    detail=rejection.detail,
                    count=1,
                    first_as_of=rejection.as_of,
                    last_as_of=rejection.as_of,
                    window_bucket=bucket,
                    strategy_version=rejection.strategy_version,
                    config_hash=rejection.config_hash,
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def recent(self, limit: int = 30) -> list[SetupRejectionRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SetupRejectionRecord)
                .order_by(SetupRejectionRecord.last_as_of.desc(), SetupRejectionRecord.id.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def counts_by_code(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(
                    SetupRejectionRecord.primary_code, sa.func.sum(SetupRejectionRecord.count)
                ).group_by(SetupRejectionRecord.primary_code)
            )
            return {code: int(total) for code, total in rows.all()}
