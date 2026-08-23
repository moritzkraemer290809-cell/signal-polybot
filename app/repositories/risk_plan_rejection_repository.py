"""Aggregated risk plan rejection persistence.

Identical rejections (candidate + code + time bucket + risk model version)
aggregate into one row with a count - the database never floods with
per-evaluation rejection rows."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import RiskPlanRejectionRecord
from app.risk.models import RiskPlanRejection


def _bucket(as_of: datetime, window_seconds: float) -> str:
    epoch = int(as_of.timestamp())
    return str(epoch - epoch % max(1, int(window_seconds)))


class RiskPlanRejectionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_rejection(self, rejection: RiskPlanRejection, window_seconds: float) -> bool:
        """Insert or aggregate.  Returns True for a new row."""
        bucket = _bucket(rejection.as_of, window_seconds)
        candidate_key = str(rejection.candidate_id) if rejection.candidate_id else "-"
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(RiskPlanRejectionRecord).where(
                        RiskPlanRejectionRecord.candidate_key == candidate_key,
                        RiskPlanRejectionRecord.primary_code == rejection.primary_code.value,
                        RiskPlanRejectionRecord.window_bucket == bucket,
                        RiskPlanRejectionRecord.risk_model_version == rejection.risk_model_version,
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
                RiskPlanRejectionRecord(
                    candidate_id=rejection.candidate_id,
                    candidate_key=candidate_key,
                    instrument_pk=rejection.instrument_pk,
                    symbol=rejection.symbol,
                    primary_code=rejection.primary_code.value,
                    codes={"codes": [code.value for code in rejection.codes]},
                    detail=rejection.detail,
                    count=1,
                    first_as_of=rejection.as_of,
                    last_as_of=rejection.as_of,
                    window_bucket=bucket,
                    risk_model_version=rejection.risk_model_version,
                    risk_config_hash=rejection.risk_config_hash,
                    cost_model_version=rejection.cost_model_version,
                    fee_schedule_version=rejection.fee_schedule_version,
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def recent(self, limit: int = 30) -> list[RiskPlanRejectionRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(RiskPlanRejectionRecord)
                .order_by(
                    RiskPlanRejectionRecord.last_as_of.desc(), RiskPlanRejectionRecord.id.desc()
                )
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def counts_by_code(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(
                    RiskPlanRejectionRecord.primary_code,
                    sa.func.sum(RiskPlanRejectionRecord.count),
                ).group_by(RiskPlanRejectionRecord.primary_code)
            )
            return {code: int(total) for code, total in rows.all()}

    async def count_since(self, since: datetime) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(RiskPlanRejectionRecord.count), 0)).where(
                    RiskPlanRejectionRecord.last_as_of >= since
                )
            )
            return int(value.scalar_one())
