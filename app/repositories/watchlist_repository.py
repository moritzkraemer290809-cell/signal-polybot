"""Persistence for the active watchlist snapshot and its event history.

PostgreSQL is the source of truth; Redis may cache read-only projections.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import ActiveWatchlistEntry, WatchlistEvent
from app.selection.enums import MarketSelectionState, WatchlistEventType


class WatchlistRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_all(self) -> list[ActiveWatchlistEntry]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(ActiveWatchlistEntry).order_by(ActiveWatchlistEntry.symbol)
            )
            return list(rows.scalars().all())

    async def upsert_entry(
        self,
        instrument_pk: int,
        symbol: str,
        state: MarketSelectionState,
        asset_class: str,
        quality_score: int | None,
        eligibility_status: str,
        reasons: list[dict[str, Any]],
        decision_id: uuid.UUID | None,
    ) -> None:
        now = datetime.now(tz=UTC)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(ActiveWatchlistEntry).where(
                        ActiveWatchlistEntry.instrument_pk == instrument_pk
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = ActiveWatchlistEntry(
                    instrument_pk=instrument_pk,
                    symbol=symbol,
                    state=state.value,
                    asset_class=asset_class,
                    activated_at=now if state is MarketSelectionState.WATCHLIST_ACTIVE else None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            previous_state = row.state
            row.symbol = symbol
            row.state = state.value
            row.asset_class = asset_class
            row.quality_score = quality_score
            row.eligibility_status = eligibility_status
            row.reasons = {"reasons": reasons}
            row.last_decision_id = decision_id
            row.updated_at = now
            if state is MarketSelectionState.WATCHLIST_ACTIVE:
                if previous_state != MarketSelectionState.WATCHLIST_ACTIVE.value:
                    row.activated_at = now
                row.paused_at = None
            elif state is MarketSelectionState.WATCHLIST_PAUSED:
                if previous_state != MarketSelectionState.WATCHLIST_PAUSED.value:
                    row.paused_at = now
            await session.commit()

    async def remove_entry(self, instrument_pk: int) -> None:
        async with self._session_factory() as session:
            await session.execute(
                sa.delete(ActiveWatchlistEntry).where(
                    ActiveWatchlistEntry.instrument_pk == instrument_pk
                )
            )
            await session.commit()

    async def add_event(
        self,
        instrument_pk: int,
        symbol: str,
        event_type: WatchlistEventType,
        from_state: str | None,
        to_state: str | None,
        reasons: list[dict[str, Any]],
        decision_id: uuid.UUID | None,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                WatchlistEvent(
                    instrument_pk=instrument_pk,
                    symbol=symbol,
                    event_type=event_type.value,
                    from_state=from_state,
                    to_state=to_state,
                    reasons={"reasons": reasons},
                    decision_id=decision_id,
                    created_at=datetime.now(tz=UTC),
                )
            )
            await session.commit()

    async def recent_events(self, limit: int = 50) -> list[WatchlistEvent]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(WatchlistEvent)
                .order_by(WatchlistEvent.created_at.desc(), WatchlistEvent.id.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())
