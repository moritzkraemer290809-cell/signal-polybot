"""Persistence for classifications and immutable selection decisions."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import AssetClassification, MarketSelectionDecision
from app.selection.models import ClassificationResult, SelectionDecisionData


class MarketSelectionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_classification(
        self, instrument_pk: int, symbol: str, result: ClassificationResult
    ) -> None:
        """Append to the classification history (only when changed is decided
        by the caller; history itself is immutable)."""
        async with self._session_factory() as session:
            session.add(
                AssetClassification(
                    instrument_pk=instrument_pk,
                    symbol=symbol,
                    asset_class=result.asset_class.value,
                    source=result.source,
                    rule=result.rule,
                    confidence=result.confidence,
                    classified_at=result.classified_at,
                )
            )
            await session.commit()

    async def latest_classification(self, instrument_pk: int) -> AssetClassification | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(AssetClassification)
                    .where(AssetClassification.instrument_pk == instrument_pk)
                    .order_by(AssetClassification.created_at.desc(), AssetClassification.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def add_decision(self, decision: SelectionDecisionData) -> None:
        async with self._session_factory() as session:
            session.add(
                MarketSelectionDecision(
                    id=decision.selection_id,
                    instrument_pk=decision.instrument_pk,
                    symbol=decision.symbol,
                    asset_class=decision.asset_class.value,
                    classification_source=decision.classification_source,
                    session_state=decision.session_state,
                    calendar_version=decision.calendar_version,
                    market_status=decision.market_status,
                    data_quality_status=decision.data_quality_status,
                    selection_state=decision.selection_state.value,
                    eligibility_status=decision.eligibility_status.value,
                    market_quality_score=decision.market_quality_score,
                    quality_components=decision.quality_components,
                    reasons={"reasons": decision.reasons_json()},
                    configuration_version=decision.configuration_version,
                    evaluated_at=decision.evaluated_at,
                    expires_at=decision.expires_at,
                    previous_selection_id=decision.previous_selection_id,
                )
            )
            await session.commit()

    async def recent_decisions(self, limit: int = 50) -> list[MarketSelectionDecision]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(MarketSelectionDecision)
                .order_by(MarketSelectionDecision.created_at.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())
