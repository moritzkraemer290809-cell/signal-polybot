"""Persistence for conservative cost estimates."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.costs.explainability import summarize_costs
from app.costs.models import CostEstimate
from app.repositories.orm import CostEstimateRecord


class CostEstimateRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(
        self,
        estimate: CostEstimate,
        *,
        estimate_id: uuid.UUID,
        plan_id: uuid.UUID | None,
        candidate_id: uuid.UUID,
        instrument_pk: int,
        symbol: str,
        as_of: datetime,
    ) -> None:
        primary = estimate.primary_target
        async with self._session_factory() as session:
            session.add(
                CostEstimateRecord(
                    id=estimate_id,
                    plan_id=plan_id,
                    candidate_id=candidate_id,
                    instrument_pk=instrument_pk,
                    symbol=symbol,
                    as_of=as_of,
                    cost_model_name=estimate.cost_model_name,
                    cost_model_version=estimate.cost_model_version,
                    cost_config_hash=estimate.cost_config_hash,
                    fee_schedule_version=estimate.fee_schedule.version,
                    execution_assumption_version=estimate.execution_assumptions.version,
                    total_cost=estimate.total_invalidation_path_cost,
                    cost_to_risk_pct=estimate.cost_to_risk_pct,
                    net_rr_primary=(
                        primary.net_rr
                        if primary is not None and primary.net_rr is not None
                        else None
                    ),
                    payload={
                        "summary": summarize_costs(estimate),
                        "targets": [
                            {
                                "index": target.target_index,
                                "gross_pnl": target.gross_target_pnl,
                                "net_pnl": target.net_target_pnl,
                                "gross_rr": target.gross_rr,
                                "net_rr": target.net_rr,
                            }
                            for target in estimate.targets
                        ],
                        "funding": {
                            "state": estimate.funding.state.value,
                            "expected_cost": estimate.funding.expected_cost,
                            "basis": estimate.funding.basis,
                        },
                        "warnings": list(estimate.warnings),
                    },
                )
            )
            await session.commit()

    async def recent(self, limit: int = 30) -> list[CostEstimateRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(CostEstimateRecord)
                .order_by(CostEstimateRecord.created_at.desc(), CostEstimateRecord.as_of.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def count(self) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count()).select_from(CostEstimateRecord)
            )
            return int(value.scalar_one())
