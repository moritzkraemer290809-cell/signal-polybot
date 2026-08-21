"""Persistence for OHLCV candles."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import DataSource, Timeframe
from app.domain.models import CandleData
from app.repositories.orm import Candle


class CandleRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert_candles(
        self,
        instrument_pk: int,
        candles: list[CandleData],
        source: DataSource = DataSource.REST,
    ) -> int:
        """Idempotent upsert keyed by (instrument, timeframe, open_time)."""
        if not candles:
            return 0
        written = 0
        async with self._session_factory() as session:
            for candle in candles:
                existing = (
                    await session.execute(
                        sa.select(Candle).where(
                            Candle.instrument_pk == instrument_pk,
                            Candle.timeframe == candle.timeframe.value,
                            Candle.open_time == candle.open_time,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(
                        Candle(
                            instrument_pk=instrument_pk,
                            timeframe=candle.timeframe.value,
                            open_time=candle.open_time,
                            open=candle.open,
                            high=candle.high,
                            low=candle.low,
                            close=candle.close,
                            volume=candle.volume,
                            trade_count=candle.trade_count,
                            source=source.value,
                        )
                    )
                else:
                    existing.open = candle.open
                    existing.high = candle.high
                    existing.low = candle.low
                    existing.close = candle.close
                    existing.volume = candle.volume
                    existing.trade_count = candle.trade_count
                    existing.source = source.value
                written += 1
            await session.commit()
        return written

    async def get_range(
        self,
        instrument_pk: int,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(Candle)
                .where(
                    Candle.instrument_pk == instrument_pk,
                    Candle.timeframe == timeframe.value,
                    Candle.open_time >= start,
                    Candle.open_time < end,
                )
                .order_by(Candle.open_time)
            )
            return list(rows.scalars().all())

    async def latest_open_time(self, instrument_pk: int, timeframe: Timeframe) -> datetime | None:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.max(Candle.open_time)).where(
                    Candle.instrument_pk == instrument_pk,
                    Candle.timeframe == timeframe.value,
                )
            )
            result: datetime | None = value.scalar_one_or_none()
            return result
