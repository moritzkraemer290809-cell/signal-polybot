"""Persistence and lifecycle for setup candidates.

Current state lives in setup_candidates (one row per candidate), the
immutable lifecycle history in setup_candidate_events.  The ``active_key``
unique constraint (dedupe_key while DETECTED/CONFIRMED, NULL otherwise)
prevents duplicate active candidates even under race conditions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import SetupCandidateEvent, SetupCandidateRecord
from app.strategy.enums import CandidateState
from app.strategy.explainability import summarize_components
from app.strategy.models import SetupCandidate

ACTIVE_STATES = (CandidateState.DETECTED.value, CandidateState.CONFIRMED.value)


def _now() -> datetime:
    return datetime.now(tz=UTC)


class SetupCandidateRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ---------------------------------------------------------------- write

    async def insert_new(self, candidate: SetupCandidate) -> bool:
        """Insert a new candidate.  Returns False when an active candidate
        with the same dedupe key already exists (unique active_key)."""
        row = SetupCandidateRecord(
            id=candidate.candidate_id,
            instrument_pk=candidate.instrument_pk,
            instrument_id=candidate.instrument_id,
            symbol=candidate.symbol,
            asset_class=candidate.asset_class,
            candidate_type=candidate.candidate_type.value,
            direction=candidate.direction.value,
            state=candidate.state.value,
            as_of=candidate.as_of,
            expiry_at=candidate.expiry_at,
            setup_score=candidate.setup_score,
            confidence=candidate.confidence,
            primary_regime=candidate.primary_regime.value,
            regime_confidence=candidate.regime_confidence,
            higher_timeframe_structure=candidate.higher_timeframe_structure.value,
            local_structure=candidate.local_structure.value,
            session_state=candidate.session_state,
            market_quality_score=candidate.market_quality_score,
            data_quality_status=candidate.data_quality_status,
            retest_status=candidate.retest_status.value,
            reason_summary=candidate.reason_summary,
            details={
                "referenced_levels": list(candidate.referenced_levels),
                "structure_events": list(candidate.structure_events),
                "liquidity_events": list(candidate.liquidity_events),
                "reclaim_status": candidate.reclaim_status,
                "rejection_status": candidate.rejection_status,
                "score_components": summarize_components(list(candidate.score_components)),
                "invalidation_conditions": list(candidate.invalidation_conditions),
                "features": candidate.features,
                "candle_window_metadata": candidate.candle_window_metadata,
            },
            dedupe_key=candidate.dedupe_key,
            active_key=candidate.dedupe_key,
            feature_snapshot_id=candidate.feature_snapshot_id,
            strategy_name=candidate.strategy_name,
            strategy_version=candidate.strategy_version,
            feature_schema_version=candidate.feature_schema_version,
            config_hash=candidate.config_hash,
            ruleset_hash=candidate.ruleset_hash,
            created_at=candidate.as_of,
            updated_at=candidate.as_of,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            session.add(
                SetupCandidateEvent(
                    candidate_id=candidate.candidate_id,
                    event_type=candidate.state.value,
                    from_state=None,
                    to_state=candidate.state.value,
                    detail={"score": candidate.setup_score},
                )
            )
            await session.commit()
        return True

    async def transition(
        self,
        candidate_id: uuid.UUID,
        new_state: CandidateState,
        detail: dict[str, Any] | None = None,
        *,
        score: int | None = None,
    ) -> bool:
        """Transition a candidate; history preserved in events.  Terminal
        states clear active_key so the dedupe slot frees up."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(SetupCandidateRecord).where(SetupCandidateRecord.id == candidate_id)
                )
            ).scalar_one_or_none()
            if row is None or row.state == new_state.value:
                return False
            from_state = row.state
            row.state = new_state.value
            row.updated_at = _now()
            if score is not None:
                row.setup_score = score
            if new_state.value not in ACTIVE_STATES:
                row.active_key = None
            session.add(
                SetupCandidateEvent(
                    candidate_id=candidate_id,
                    event_type=new_state.value,
                    from_state=from_state,
                    to_state=new_state.value,
                    detail=detail,
                )
            )
            await session.commit()
        return True

    # ----------------------------------------------------------------- read

    async def get(self, candidate_id: uuid.UUID) -> SetupCandidateRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(SetupCandidateRecord).where(SetupCandidateRecord.id == candidate_id)
                )
            ).scalar_one_or_none()

    async def active_by_dedupe_key(self, dedupe_key: str) -> SetupCandidateRecord | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(SetupCandidateRecord).where(
                        SetupCandidateRecord.active_key == dedupe_key
                    )
                )
            ).scalar_one_or_none()

    async def active_candidates(
        self, instrument_pk: int | None = None
    ) -> list[SetupCandidateRecord]:
        query = sa.select(SetupCandidateRecord).where(SetupCandidateRecord.state.in_(ACTIVE_STATES))
        if instrument_pk is not None:
            query = query.where(SetupCandidateRecord.instrument_pk == instrument_pk)
        async with self._session_factory() as session:
            rows = await session.execute(query.order_by(SetupCandidateRecord.created_at))
            return list(rows.scalars().all())

    async def recent(self, limit: int = 30) -> list[SetupCandidateRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SetupCandidateRecord)
                .order_by(SetupCandidateRecord.updated_at.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def events_for(self, candidate_id: uuid.UUID) -> list[SetupCandidateEvent]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SetupCandidateEvent)
                .where(SetupCandidateEvent.candidate_id == candidate_id)
                .order_by(SetupCandidateEvent.created_at, SetupCandidateEvent.id)
            )
            return list(rows.scalars().all())

    async def counts_by_state(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(SetupCandidateRecord.state, sa.func.count()).group_by(
                    SetupCandidateRecord.state
                )
            )
            return {state: int(count) for state, count in rows.all()}
