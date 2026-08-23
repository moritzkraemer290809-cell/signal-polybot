"""Aggregated signal lifecycle rejection persistence."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import SignalLifecycleRejectionRecord
from app.signals.models import SignalRejection


def _bucket(as_of: datetime, window_seconds: float) -> str:
    epoch = int(as_of.timestamp())
    return str(epoch - epoch % max(1, int(window_seconds)))


class SignalRejectionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_rejection(self, rejection: SignalRejection, window_seconds: float) -> bool:
        bucket = _bucket(rejection.as_of, window_seconds)
        context_key = str(rejection.signal_id or rejection.plan_id or "-")
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(SignalLifecycleRejectionRecord).where(
                        SignalLifecycleRejectionRecord.context_key == context_key,
                        SignalLifecycleRejectionRecord.primary_code == rejection.primary_code.value,
                        SignalLifecycleRejectionRecord.window_bucket == bucket,
                        SignalLifecycleRejectionRecord.lifecycle_model_version
                        == rejection.lifecycle_model_version,
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
                SignalLifecycleRejectionRecord(
                    plan_id=rejection.plan_id,
                    candidate_id=rejection.candidate_id,
                    signal_id=rejection.signal_id,
                    context_key=context_key,
                    instrument_pk=rejection.instrument_pk,
                    symbol=rejection.symbol,
                    primary_code=rejection.primary_code.value,
                    codes={"codes": [code.value for code in rejection.codes]},
                    detail=rejection.detail,
                    count=1,
                    first_as_of=rejection.as_of,
                    last_as_of=rejection.as_of,
                    window_bucket=bucket,
                    lifecycle_model_version=rejection.lifecycle_model_version,
                    lifecycle_config_hash=rejection.lifecycle_config_hash,
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def recent(self, limit: int = 30) -> list[SignalLifecycleRejectionRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SignalLifecycleRejectionRecord)
                .order_by(
                    SignalLifecycleRejectionRecord.last_as_of.desc(),
                    SignalLifecycleRejectionRecord.id.desc(),
                )
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def counts_by_code(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(
                    SignalLifecycleRejectionRecord.primary_code,
                    sa.func.sum(SignalLifecycleRejectionRecord.count),
                ).group_by(SignalLifecycleRejectionRecord.primary_code)
            )
            return {code: int(total) for code, total in rows.all()}

    async def count_since(self, since: datetime) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(
                    sa.func.coalesce(sa.func.sum(SignalLifecycleRejectionRecord.count), 0)
                ).where(SignalLifecycleRejectionRecord.last_as_of >= since)
            )
            return int(value.scalar_one())
