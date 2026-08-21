"""Persistent active watchlist reconciliation.

The watchlist is exclusively a list of technically analysable instruments -
it contains no trade ideas and no direction.  PostgreSQL is the source of
truth; on restart the list is restored and revalidated on the next cycle.
A database outage degrades the service: state changes that could not be
persisted are NOT reported as successful and are retried on the next cycle.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from app.config import MarketSelectionSettings
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.repositories.watchlist_repository import WatchlistRepository
from app.selection.enums import (
    REASON_NOT_IN_ALLOWLIST,
    REASON_WATCHLIST_FULL,
    MarketSelectionState,
    WatchlistEventType,
)
from app.selection.models import Reason, SelectionDecisionData, SelectionInputs
from app.selection.selection_events import SelectionNotifier


@dataclass
class _Entry:
    symbol: str
    state: MarketSelectionState


class WatchlistService:
    def __init__(
        self,
        repository: WatchlistRepository,
        settings: MarketSelectionSettings,
        notifier: SelectionNotifier,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._notifier = notifier
        self._log = get_logger("watchlist")
        self._entries: dict[int, _Entry] = {}
        self.degraded = False
        self.restored = False

    async def restore(self) -> None:
        """Restore the persisted watchlist; revalidation happens next cycle."""
        try:
            rows = await self._repository.load_all()
        except Exception as exc:
            self.degraded = True
            self._log.warning("watchlist_restore_failed", error=type(exc).__name__)
            return
        self._entries = {
            row.instrument_pk: _Entry(row.symbol, MarketSelectionState(row.state)) for row in rows
        }
        self.restored = True
        self.degraded = False
        self._log.info("watchlist_restored", entries=len(self._entries))

    # ------------------------------------------------------------ reconcile

    async def reconcile(
        self, evaluations: list[tuple[SelectionInputs, SelectionDecisionData]]
    ) -> dict[int, SelectionDecisionData]:
        """Reconcile the watchlist against fresh evaluations.

        Returns the final decisions (selection_state adjusted to the actual
        watchlist outcome) keyed by instrument_pk.
        """
        evaluated_pks = {inputs.instrument_pk for inputs, _ in evaluations}

        # rank eligible + allowlisted candidates: quality first, symbol as tiebreak
        candidates = sorted(
            (
                (inputs, decision)
                for inputs, decision in evaluations
                if decision.eligible and inputs.allowlisted
            ),
            key=lambda pair: (-(pair[1].market_quality_score or 0), pair[0].symbol),
        )
        target_active = {
            inputs.instrument_pk for inputs, _ in candidates[: self._settings.max_watchlist_size]
        }

        final: dict[int, SelectionDecisionData] = {}
        for inputs, decision in evaluations:
            pk = inputs.instrument_pk
            current = self._entries.get(pk)
            if pk in target_active:
                final[pk] = await self._to_active(pk, inputs, decision, current)
            elif decision.eligible:
                reason = (
                    Reason(REASON_NOT_IN_ALLOWLIST, "symbol not in allowlist")
                    if not inputs.allowlisted
                    else Reason(REASON_WATCHLIST_FULL, "no free watchlist slot")
                )
                final[pk] = await self._to_not_active(pk, inputs, decision, current, reason)
            else:
                final[pk] = await self._to_ineligible(pk, inputs, decision, current)

        # entries whose instruments vanished from the evaluated universe
        for pk in [pk for pk in self._entries if pk not in evaluated_pks]:
            await self._remove(pk)

        metrics.set_gauge(
            "selection.active_watchlist_size",
            float(
                sum(
                    1
                    for entry in self._entries.values()
                    if entry.state is MarketSelectionState.WATCHLIST_ACTIVE
                )
            ),
        )
        return final

    # ------------------------------------------------------------- helpers

    def _with_state(
        self, decision: SelectionDecisionData, state: MarketSelectionState, *extra: Reason
    ) -> SelectionDecisionData:
        return dataclasses.replace(
            decision, selection_state=state, reasons=[*decision.reasons, *extra]
        )

    async def _persist_entry(
        self, pk: int, decision: SelectionDecisionData, state: MarketSelectionState
    ) -> bool:
        try:
            await self._repository.upsert_entry(
                instrument_pk=pk,
                symbol=decision.symbol,
                state=state,
                asset_class=decision.asset_class.value,
                quality_score=decision.market_quality_score,
                eligibility_status=decision.eligibility_status.value,
                reasons=decision.reasons_json(),
                decision_id=decision.selection_id,
            )
            self.degraded = False
            return True
        except Exception as exc:
            # unpersisted state changes are never reported as successful
            self.degraded = True
            self._log.error("watchlist_persist_failed", error=type(exc).__name__)
            return False

    async def _emit_event(
        self,
        pk: int,
        decision: SelectionDecisionData,
        event: WatchlistEventType,
        from_state: str | None,
        to_state: str | None,
    ) -> None:
        try:
            await self._repository.add_event(
                instrument_pk=pk,
                symbol=decision.symbol,
                event_type=event,
                from_state=from_state,
                to_state=to_state,
                reasons=decision.reasons_json(),
                decision_id=decision.selection_id,
            )
        except Exception as exc:
            self.degraded = True
            self._log.error("watchlist_event_persist_failed", error=type(exc).__name__)

    async def _to_active(
        self,
        pk: int,
        inputs: SelectionInputs,
        decision: SelectionDecisionData,
        current: _Entry | None,
    ) -> SelectionDecisionData:
        final = self._with_state(decision, MarketSelectionState.WATCHLIST_ACTIVE)
        if current is not None and current.state is MarketSelectionState.WATCHLIST_ACTIVE:
            # idempotent refresh: no event, just updated metrics/score
            await self._persist_entry(pk, final, MarketSelectionState.WATCHLIST_ACTIVE)
            return final
        if not await self._persist_entry(pk, final, MarketSelectionState.WATCHLIST_ACTIVE):
            return decision  # not persisted -> no state change reported
        if current is None:
            event = WatchlistEventType.WATCHLIST_ADDED
            metrics.increment("selection.watchlist_added")
        else:
            event = WatchlistEventType.WATCHLIST_RESTORED
            await self._notifier.watchlist_restored(inputs.symbol, str(decision.selection_id))
        await self._emit_event(
            pk, final, event, current.state.value if current else None, final.selection_state.value
        )
        self._entries[pk] = _Entry(inputs.symbol, MarketSelectionState.WATCHLIST_ACTIVE)
        self._log.info(
            "watchlist_state_change",
            symbol=inputs.symbol,
            change=event.value,
            score=decision.market_quality_score,
        )
        return final

    async def _to_not_active(
        self,
        pk: int,
        inputs: SelectionInputs,
        decision: SelectionDecisionData,
        current: _Entry | None,
        reason: Reason,
    ) -> SelectionDecisionData:
        """Eligible but no slot / not allowlisted."""
        if current is None:
            return self._with_state(decision, MarketSelectionState.WATCHLIST_ELIGIBLE, reason)
        final = self._with_state(decision, MarketSelectionState.WATCHLIST_PAUSED, reason)
        if current.state is MarketSelectionState.WATCHLIST_PAUSED:
            await self._persist_entry(pk, final, MarketSelectionState.WATCHLIST_PAUSED)
            return final
        if await self._persist_entry(pk, final, MarketSelectionState.WATCHLIST_PAUSED):
            await self._emit_event(
                pk,
                final,
                WatchlistEventType.WATCHLIST_PAUSED,
                current.state.value,
                MarketSelectionState.WATCHLIST_PAUSED.value,
            )
            self._entries[pk] = _Entry(inputs.symbol, MarketSelectionState.WATCHLIST_PAUSED)
            metrics.increment("selection.watchlist_paused")
        return final

    async def _to_ineligible(
        self,
        pk: int,
        inputs: SelectionInputs,
        decision: SelectionDecisionData,
        current: _Entry | None,
    ) -> SelectionDecisionData:
        if current is None:
            metrics.increment(f"selection.rejections.{decision.eligibility_status.value.lower()}")
            return decision  # never listed: decision history only, no event
        final = self._with_state(decision, MarketSelectionState.WATCHLIST_PAUSED)
        if current.state is MarketSelectionState.WATCHLIST_PAUSED:
            await self._persist_entry(pk, final, MarketSelectionState.WATCHLIST_PAUSED)
            return final
        if await self._persist_entry(pk, final, MarketSelectionState.WATCHLIST_PAUSED):
            await self._emit_event(
                pk,
                final,
                WatchlistEventType.WATCHLIST_PAUSED,
                current.state.value,
                MarketSelectionState.WATCHLIST_PAUSED.value,
            )
            self._entries[pk] = _Entry(inputs.symbol, MarketSelectionState.WATCHLIST_PAUSED)
            detail = decision.reasons[0].detail if decision.reasons else ""
            await self._notifier.watchlist_paused(
                inputs.symbol,
                decision.eligibility_status.value,
                detail,
                str(decision.selection_id),
            )
        return final

    async def _remove(self, pk: int) -> None:
        entry = self._entries[pk]
        try:
            await self._repository.remove_entry(pk)
            await self._repository.add_event(
                instrument_pk=pk,
                symbol=entry.symbol,
                event_type=WatchlistEventType.WATCHLIST_REMOVED,
                from_state=entry.state.value,
                to_state=MarketSelectionState.REMOVED.value,
                reasons=[{"code": "INSTRUMENT_DELISTED", "detail": "left the universe"}],
                decision_id=None,
            )
        except Exception as exc:
            self.degraded = True
            self._log.error("watchlist_remove_failed", error=type(exc).__name__)
            return
        del self._entries[pk]
        metrics.increment("selection.watchlist_removed")
        self._log.info("watchlist_state_change", symbol=entry.symbol, change="WATCHLIST_REMOVED")

    # ---------------------------------------------------------------- reads

    @property
    def repository(self) -> WatchlistRepository:
        return self._repository

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {entry.symbol: {"state": entry.state.value} for entry in self._entries.values()}

    def active_count(self) -> int:
        return sum(
            1
            for entry in self._entries.values()
            if entry.state is MarketSelectionState.WATCHLIST_ACTIVE
        )

    def paused_count(self) -> int:
        return sum(
            1
            for entry in self._entries.values()
            if entry.state is MarketSelectionState.WATCHLIST_PAUSED
        )
