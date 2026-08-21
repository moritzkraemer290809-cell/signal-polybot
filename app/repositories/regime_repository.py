"""Regime persistence into the existing market_regimes table.

Only regime CHANGES are appended (per instrument+timeframe) - idempotent
re-evaluations create no redundant history rows."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import MarketRegimeRecord
from app.strategy.models import RegimeResult


class RegimeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def latest_regime(self, instrument_pk: int, timeframe: str) -> str | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(MarketRegimeRecord.regime)
                    .where(
                        MarketRegimeRecord.instrument_pk == instrument_pk,
                        MarketRegimeRecord.timeframe == timeframe,
                    )
                    .order_by(MarketRegimeRecord.ts.desc(), MarketRegimeRecord.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row

    async def record_if_changed(
        self,
        instrument_pk: int,
        timeframe: str,
        result: RegimeResult,
        as_of: datetime,
        strategy_version: str,
    ) -> bool:
        latest = await self.latest_regime(instrument_pk, timeframe)
        if latest == result.regime.value:
            return False
        async with self._session_factory() as session:
            session.add(
                MarketRegimeRecord(
                    instrument_pk=instrument_pk,
                    timeframe=timeframe,
                    ts=as_of,
                    regime=result.regime.value,
                    confidence=Decimal(result.confidence),
                    features={
                        "reasons": list(result.reasons),
                        "secondary_flags": list(result.secondary_flags),
                        "features_used": {
                            key: value for key, value in result.features_used.items()
                        },
                    },
                    strategy_version=strategy_version,
                )
            )
            await session.commit()
        return True

    async def regime_counts(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(MarketRegimeRecord.regime, sa.func.count()).group_by(
                    MarketRegimeRecord.regime
                )
            )
            return {regime: int(count) for regime, count in rows.all()}
