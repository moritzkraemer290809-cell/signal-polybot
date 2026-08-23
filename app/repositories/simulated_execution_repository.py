"""Modelled execution legs of hypothetical simulations (never fills)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import SimulatedExecutionRecord
from app.simulation.models import ModelledExecution


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), 12)))


class SimulatedExecutionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_executions(
        self,
        *,
        run_id: uuid.UUID,
        simulated_position_id: uuid.UUID,
        executions: tuple[ModelledExecution | None, ...],
    ) -> int:
        rows = [
            SimulatedExecutionRecord(
                id=execution.execution_id,
                simulated_position_id=simulated_position_id,
                run_id=run_id,
                leg=execution.leg.value,
                side_consumed=execution.side_consumed,
                modelled_price=_decimal(execution.modelled_price),
                reference_price=_decimal(execution.reference_price),
                quantity=_decimal(execution.quantity),
                notional=_decimal(execution.notional),
                slippage_cost=_decimal(execution.slippage_cost),
                slippage_bps=_decimal(execution.slippage_bps),
                fee_cost=_decimal(execution.fee_cost),
                levels_consumed=execution.levels_consumed,
                book_snapshot_id=execution.book_snapshot_id,
                book_at=execution.book_at,
                as_of=execution.as_of,
                assumptions=dict(execution.assumptions),
            )
            for execution in executions
            if execution is not None
        ]
        if not rows:
            return 0
        async with self._session_factory() as session:
            session.add_all(rows)
            await session.commit()
        return len(rows)

    async def executions_for(
        self, simulated_position_id: uuid.UUID
    ) -> list[SimulatedExecutionRecord]:
        async with self._session_factory() as session:
            result = await session.execute(
                sa.select(SimulatedExecutionRecord)
                .where(SimulatedExecutionRecord.simulated_position_id == simulated_position_id)
                .order_by(SimulatedExecutionRecord.as_of, SimulatedExecutionRecord.leg)
            )
            return list(result.scalars().all())

    async def count(self) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count()).select_from(SimulatedExecutionRecord)
            )
            return int(value.scalar_one())
