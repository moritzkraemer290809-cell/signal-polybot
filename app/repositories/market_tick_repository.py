"""Bulk persistence for ticker snapshots (market_ticks table)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import DataSource
from app.domain.models import TickerData
from app.repositories.orm import MarketTick


class MarketTickRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_ticks(
        self,
        ticks: list[tuple[int, TickerData]],
        source: DataSource = DataSource.WEBSOCKET,
    ) -> int:
        """Insert (instrument_pk, ticker) pairs in one transaction."""
        if not ticks:
            return 0
        async with self._session_factory() as session:
            for instrument_pk, ticker in ticks:
                session.add(
                    MarketTick(
                        instrument_pk=instrument_pk,
                        ts=ticker.ts,
                        mark_price=ticker.mark_price,
                        index_price=ticker.index_price,
                        last_price=ticker.last_price,
                        mid_price=ticker.mid_price,
                        open_interest=ticker.open_interest,
                        funding_rate=ticker.funding_rate,
                        next_funding_at=ticker.next_funding_at,
                        source=source.value,
                    )
                )
            await session.commit()
        return len(ticks)
