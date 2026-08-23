"""Persistence for instrument risk snapshots (public data only)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import InstrumentRiskSnapshotRecord
from app.risk.models import InstrumentRiskSnapshot


class InstrumentRiskRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_snapshot(self, snapshot_id: uuid.UUID, snapshot: InstrumentRiskSnapshot) -> None:
        async with self._session_factory() as session:
            session.add(
                InstrumentRiskSnapshotRecord(
                    id=snapshot_id,
                    instrument_pk=snapshot.instrument_pk,
                    instrument_id=snapshot.instrument_id,
                    symbol=snapshot.symbol,
                    as_of=snapshot.as_of,
                    snapshot_version=snapshot.snapshot_version,
                    mark_price=snapshot.mark_price,
                    max_leverage=snapshot.max_leverage,
                    min_notional=snapshot.min_notional,
                    data_quality_status=snapshot.data_quality_status,
                    orderbook_fresh=snapshot.orderbook_fresh,
                    payload=snapshot.as_payload(),
                )
            )
            await session.commit()

    async def latest_for(self, instrument_pk: int) -> InstrumentRiskSnapshotRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(InstrumentRiskSnapshotRecord)
                    .where(InstrumentRiskSnapshotRecord.instrument_pk == instrument_pk)
                    .order_by(
                        InstrumentRiskSnapshotRecord.as_of.desc(),
                        InstrumentRiskSnapshotRecord.created_at.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def count(self) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count()).select_from(InstrumentRiskSnapshotRecord)
            )
            return int(value.scalar_one())
