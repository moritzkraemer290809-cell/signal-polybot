"""Persistence for strategy research artefacts: feature snapshots, swings,
structure events, liquidity levels/events.  Inserts are idempotent via DB
unique constraints (IntegrityError -> already recorded)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import (
    FeatureSnapshot,
    LiquidityEventRecord,
    LiquidityLevelRecord,
    StructureEventRecord,
    SwingPoint,
)
from app.strategy.models import (
    EvaluationOutcome,
    LiquidityLevel,
    StructureEvent,
    SweepEvent,
    Swing,
)


def _price_key(price: float) -> str:
    return f"{price:.8g}"


class FeatureRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_snapshot(
        self,
        snapshot_id: uuid.UUID,
        instrument_pk: int,
        symbol: str,
        outcome: EvaluationOutcome,
        *,
        strategy_name: str,
        strategy_version: str,
        feature_schema_version: str,
        config_hash: str,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                FeatureSnapshot(
                    id=snapshot_id,
                    instrument_pk=instrument_pk,
                    symbol=symbol,
                    as_of=outcome.as_of,
                    candle_window_metadata=outcome.candle_window_metadata,
                    feature_values={
                        key: value
                        for key, value in outcome.feature_values.items()
                        if value is None or isinstance(value, (int, float, str, bool))
                    },
                    feature_validity=dict(outcome.feature_validity),
                    warnings={"warnings": list(outcome.warnings)},
                    strategy_name=strategy_name,
                    strategy_version=strategy_version,
                    feature_schema_version=feature_schema_version,
                    config_hash=config_hash,
                )
            )
            await session.commit()

    async def _insert_ignoring_duplicates(self, rows: list[object]) -> int:
        written = 0
        for row in rows:
            async with self._session_factory() as session:
                session.add(row)
                try:
                    await session.commit()
                    written += 1
                except IntegrityError:
                    await session.rollback()
        return written

    async def add_swings(
        self, instrument_pk: int, swings: list[Swing], strategy_version: str
    ) -> int:
        return await self._insert_ignoring_duplicates(
            [
                SwingPoint(
                    instrument_pk=instrument_pk,
                    timeframe=swing.timeframe.value,
                    swing_type=swing.swing_type.value,
                    price=swing.price,
                    candle_open_time=swing.candle_open_time,
                    confirmed_at=swing.confirmed_at,
                    strength=swing.strength,
                    relevance=swing.relevance,
                    params=swing.params,
                    strategy_version=strategy_version,
                )
                for swing in swings
            ]
        )

    async def add_structure_events(
        self, instrument_pk: int, events: list[StructureEvent], strategy_version: str
    ) -> int:
        return await self._insert_ignoring_duplicates(
            [
                StructureEventRecord(
                    instrument_pk=instrument_pk,
                    timeframe=event.timeframe.value,
                    event_type=event.event_type.value,
                    price=event.price,
                    reference_swing=event.reference_swing_id,
                    confirm_close_time=event.confirm_close_time,
                    detail=event.detail,
                    strategy_version=strategy_version,
                )
                for event in events
            ]
        )

    async def add_liquidity_levels(
        self, instrument_pk: int, levels: list[LiquidityLevel], strategy_version: str
    ) -> int:
        return await self._insert_ignoring_duplicates(
            [
                LiquidityLevelRecord(
                    instrument_pk=instrument_pk,
                    timeframe=level.timeframe.value,
                    level_type=level.level_type.value,
                    price=level.price,
                    price_key=_price_key(level.price),
                    relevance=level.relevance,
                    touches=level.touches,
                    is_major=level.is_major,
                    detail=level.detail,
                    strategy_version=strategy_version,
                )
                for level in levels
            ]
        )

    async def add_liquidity_events(
        self, instrument_pk: int, sweeps: list[SweepEvent], strategy_version: str
    ) -> int:
        return await self._insert_ignoring_duplicates(
            [
                LiquidityEventRecord(
                    instrument_pk=instrument_pk,
                    timeframe=sweep.level.timeframe.value,
                    event_type="SWEEP",
                    direction=sweep.direction.value,
                    level_price=sweep.level.price,
                    level_price_key=_price_key(sweep.level.price),
                    event_candle_open_time=sweep.sweep_candle_open_time,
                    overshoot_bps=sweep.overshoot_bps,
                    confirmed=sweep.confirmed,
                    confidence=sweep.confidence,
                    detail=sweep.detail,
                    strategy_version=strategy_version,
                )
                for sweep in sweeps
            ]
        )

    async def cleanup_snapshots(self, retention_days: int) -> int:
        cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
        async with self._session_factory() as session:
            result = await session.execute(
                sa.delete(FeatureSnapshot).where(FeatureSnapshot.created_at < cutoff)
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def snapshot_count(self) -> int:
        async with self._session_factory() as session:
            value = await session.execute(sa.select(sa.func.count()).select_from(FeatureSnapshot))
            return int(value.scalar_one())

    async def recent_swings(
        self, instrument_pk: int, timeframe: str, limit: int = 20
    ) -> list[SwingPoint]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SwingPoint)
                .where(
                    SwingPoint.instrument_pk == instrument_pk,
                    SwingPoint.timeframe == timeframe,
                )
                .order_by(SwingPoint.confirmed_at.desc(), SwingPoint.id.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def recent_liquidity_levels(
        self, instrument_pk: int, timeframe: str, limit: int = 30
    ) -> list[LiquidityLevelRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(LiquidityLevelRecord)
                .where(
                    LiquidityLevelRecord.instrument_pk == instrument_pk,
                    LiquidityLevelRecord.timeframe == timeframe,
                )
                .order_by(LiquidityLevelRecord.created_at.desc(), LiquidityLevelRecord.id.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def structure_events_since(
        self, instrument_pk: int, since: datetime, limit: int = 50
    ) -> list[StructureEventRecord]:
        """Structure events confirmed at/after ``since`` (all timeframes)."""
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(StructureEventRecord)
                .where(
                    StructureEventRecord.instrument_pk == instrument_pk,
                    StructureEventRecord.confirm_close_time >= since,
                )
                .order_by(
                    StructureEventRecord.confirm_close_time.desc(),
                    StructureEventRecord.id.desc(),
                )
                .limit(limit)
            )
            return list(rows.scalars().all())
