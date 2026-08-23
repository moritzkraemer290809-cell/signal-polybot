"""Hypothetical simulated positions (never real positions).

The dedupe key blocks a second simulation of the same lifecycle inside
one run; completed rows are immutable history.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import SimulatedPositionRecord
from app.simulation.enums import SimulatedPositionState
from app.simulation.explainability import simulation_row
from app.simulation.models import SimulatedPositionResult


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(float(value), 12)))


class SimulatedPositionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_position(
        self, result: SimulatedPositionResult, *, disclaimer_version: str
    ) -> bool:
        """Persist one hypothetical simulation; False on duplicate."""
        costs = result.costs
        row = SimulatedPositionRecord(
            id=result.simulation_position_id,
            run_id=result.run_id,
            lifecycle_signal_id=result.lifecycle_signal_id,
            plan_id=result.plan_id,
            candidate_id=result.candidate_id,
            symbol=result.symbol,
            asset_class=result.asset_class,
            candidate_type=result.candidate_type,
            direction=result.direction,
            state=result.state.value,
            exit_reason=result.exit_reason.value if result.exit_reason else None,
            delay_model=result.delay.model.value if result.delay else "UNKNOWN",
            delay_seconds=_decimal(result.delay.delay_seconds if result.delay else None),
            event_reference_at=result.event_reference_at,
            scheduled_entry_at=result.delay.scheduled_entry_at if result.delay else None,
            modelled_entry_at=result.entry.as_of if result.entry else None,
            modelled_exit_at=result.exit_execution.as_of if result.exit_execution else None,
            modelled_entry_price=_decimal(result.entry.modelled_price if result.entry else None),
            modelled_exit_price=_decimal(
                result.exit_execution.modelled_price if result.exit_execution else None
            ),
            modelled_quantity=_decimal(result.entry.quantity if result.entry else None),
            gross_result=_decimal(result.gross_result),
            net_result=_decimal(result.net_result),
            gross_r=_decimal(result.gross_r),
            net_r=_decimal(result.net_r),
            fees_total=_decimal(costs.fees_total if costs else None),
            slippage_total=_decimal(costs.slippage_total if costs else None),
            funding_total=_decimal(costs.funding_cost if costs else None),
            duration_seconds=_decimal(result.duration_seconds),
            data_completeness=result.data_completeness.value,
            warnings={"warnings": list(result.warnings)},
            payload=simulation_row(result),
            dedupe_key=result.dedupe_key,
            disclaimer_version=disclaimer_version,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def get(self, position_id: uuid.UUID) -> SimulatedPositionRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(SimulatedPositionRecord).where(
                        SimulatedPositionRecord.id == position_id
                    )
                )
            ).scalar_one_or_none()

    async def exists_for_lifecycle(self, run_id: uuid.UUID, lifecycle_signal_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count()).where(
                    SimulatedPositionRecord.run_id == run_id,
                    SimulatedPositionRecord.lifecycle_signal_id == lifecycle_signal_id,
                )
            )
            return int(value.scalar_one()) > 0

    async def active_for_lifecycle(
        self, lifecycle_signal_id: uuid.UUID
    ) -> SimulatedPositionRecord | None:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SimulatedPositionRecord).where(
                    SimulatedPositionRecord.lifecycle_signal_id == lifecycle_signal_id,
                    SimulatedPositionRecord.state.in_(
                        (
                            SimulatedPositionState.PENDING_DELAY.value,
                            SimulatedPositionState.MODELLED_ENTRY.value,
                            SimulatedPositionState.OPEN_SIMULATION.value,
                            SimulatedPositionState.PARTIAL_REDUCTION_MODELLED.value,
                        )
                    ),
                )
            )
            return rows.scalars().first()

    async def positions_for_run(self, run_id: uuid.UUID) -> list[SimulatedPositionRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SimulatedPositionRecord)
                .where(SimulatedPositionRecord.run_id == run_id)
                .order_by(SimulatedPositionRecord.event_reference_at)
            )
            return list(rows.scalars().all())

    async def recent(self, limit: int = 20) -> list[SimulatedPositionRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SimulatedPositionRecord)
                .order_by(SimulatedPositionRecord.created_at.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def counts_by_state(self, run_id: uuid.UUID | None = None) -> dict[str, int]:
        query = sa.select(SimulatedPositionRecord.state, sa.func.count()).group_by(
            SimulatedPositionRecord.state
        )
        if run_id is not None:
            query = query.where(SimulatedPositionRecord.run_id == run_id)
        async with self._session_factory() as session:
            rows = await session.execute(query)
            return {state: int(count) for state, count in rows.all()}

    async def count(self) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count()).select_from(SimulatedPositionRecord)
            )
            return int(value.scalar_one())
