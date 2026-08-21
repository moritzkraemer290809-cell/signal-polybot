"""Persistence for order book snapshots (audit data for later cost models)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import DataSource
from app.repositories.orm import OrderbookSnapshot


class OrderbookSnapshotRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_snapshots(self, snapshots: list[dict[str, Any]]) -> int:
        """Insert prepared snapshot dicts (built by the market data service)."""
        if not snapshots:
            return 0
        async with self._session_factory() as session:
            for snapshot in snapshots:
                session.add(OrderbookSnapshot(**snapshot))
            await session.commit()
        return len(snapshots)

    @staticmethod
    def prepare(
        instrument_pk: int,
        ts: datetime,
        sequence: int | None,
        depth_level: int,
        bids: list[list[str]],
        asks: list[list[str]],
        best_bid: Decimal | None,
        best_ask: Decimal | None,
        spread_bps: Decimal | None,
        source: DataSource = DataSource.WEBSOCKET,
    ) -> dict[str, Any]:
        return {
            "instrument_pk": instrument_pk,
            "ts": ts,
            "sequence": sequence,
            "depth_level": depth_level,
            "bids": {"levels": bids},
            "asks": {"levels": asks},
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_bps": spread_bps,
            "source": source.value,
        }
