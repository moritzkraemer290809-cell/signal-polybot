"""Persistence for funding rates (idempotent on instrument + timestamp)."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import DataSource
from app.domain.models import FundingRateData
from app.repositories.orm import FundingRate


class FundingRateRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_rates(
        self,
        rates: list[tuple[int, FundingRateData]],
        source: DataSource = DataSource.WEBSOCKET,
    ) -> int:
        """Insert (instrument_pk, rate) pairs, skipping already known timestamps."""
        if not rates:
            return 0
        written = 0
        async with self._session_factory() as session:
            for instrument_pk, rate in rates:
                exists = (
                    await session.execute(
                        sa.select(FundingRate.id).where(
                            FundingRate.instrument_pk == instrument_pk,
                            FundingRate.ts == rate.ts,
                        )
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    continue
                session.add(
                    FundingRate(
                        instrument_pk=instrument_pk,
                        ts=rate.ts,
                        funding_rate=rate.funding_rate,
                        source=source.value,
                    )
                )
                written += 1
            await session.commit()
        return written

    async def recent_rates(
        self, instrument_pk: int, since: datetime
    ) -> list[tuple[datetime, float]]:
        """(ts, rate) pairs since the cutoff, oldest first - public data."""
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(FundingRate.ts, FundingRate.funding_rate)
                .where(FundingRate.instrument_pk == instrument_pk, FundingRate.ts >= since)
                .order_by(FundingRate.ts)
            )
            return [(ts, float(rate)) for ts, rate in rows.all()]
