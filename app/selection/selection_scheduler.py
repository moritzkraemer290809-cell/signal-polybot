"""Periodic market selection refresh with overlap protection.

Runs the full evaluation cycle (sessions -> per-instrument evaluation ->
watchlist reconciliation -> persistence) on a configurable interval, and can
be triggered immediately (universe refresh, session transition).  Uses only
cached live data - never forces REST calls per evaluation.  The global
PAUSED mode does not stop the technical refresh; it only suppresses optional
notifications (handled by the notifier).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.config import MarketSelectionSettings
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.market_selection_repository import MarketSelectionRepository
from app.repositories.session_event_repository import SessionEventRepository
from app.selection.enums import (
    InstrumentEligibilityStatus,
    SelectionSubsystemState,
)
from app.selection.market_selection_engine import (
    MarketSelectionService,
    evaluate_instrument,
)
from app.selection.models import SelectionDecisionData, SelectionInputs
from app.selection.selection_events import SelectionNotifier
from app.selection.watchlist_service import WatchlistService
from app.sessions.session_manager import CryptoSessionManager, EquitySessionManager


class MarketSelectionCoordinator:
    def __init__(
        self,
        settings: MarketSelectionSettings,
        selection_service: MarketSelectionService,
        watchlist: WatchlistService,
        equity_sessions: EquitySessionManager,
        crypto_sessions: CryptoSessionManager,
        instrument_repo: InstrumentRepository,
        selection_repo: MarketSelectionRepository,
        session_event_repo: SessionEventRepository,
        notifier: SelectionNotifier,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._settings = settings
        self._service = selection_service
        self._watchlist = watchlist
        self._equity_sessions = equity_sessions
        self._crypto_sessions = crypto_sessions
        self._instrument_repo = instrument_repo
        self._selection_repo = selection_repo
        self._session_event_repo = session_event_repo
        self._notifier = notifier
        self._now = now_fn
        self._log = get_logger("market_selection")

        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._last_equity_state: str | None = None
        self._last_crypto_state: str | None = None
        self._last_outcome: dict[int, tuple[str, str]] = {}
        self._last_classification: dict[int, str] = {}

        self.last_run_at: datetime | None = None
        self.last_run_summary: dict[str, Any] = {}
        self.last_equity_snapshot: Any = None
        self.last_crypto_snapshot: Any = None
        self.degraded = False

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is None:
            await self._watchlist.restore()
            self._task = asyncio.create_task(self._run(), name="market_selection")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def trigger(self) -> None:
        """Request an immediate refresh (universe refresh, session change)."""
        self._wakeup.set()

    @property
    def state(self) -> SelectionSubsystemState:
        if not self._settings.enabled:
            return SelectionSubsystemState.DISABLED
        if self._task is None or self._task.done():
            return SelectionSubsystemState.UNAVAILABLE
        if self.degraded or self._watchlist.degraded:
            return SelectionSubsystemState.DEGRADED
        return SelectionSubsystemState.HEALTHY

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.degraded = True
                self._log.error("selection_cycle_failed", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._settings.refresh_seconds)
            self._wakeup.clear()

    # ----------------------------------------------------------------- cycle

    async def run_once(self) -> dict[str, Any]:
        """One full evaluation cycle.  Overlapping runs are serialised."""
        if self._lock.locked():
            metrics.increment("selection.overlap_skipped")
            self._log.info("selection_run_skipped_overlap")
            return self.last_run_summary
        async with self._lock:
            return await self._cycle()

    async def _cycle(self) -> dict[str, Any]:
        equity = self._equity_sessions.evaluate(self._now())
        crypto = self._crypto_sessions.evaluate(self._now())
        self.last_equity_snapshot = equity
        self.last_crypto_snapshot = crypto
        await self._handle_session_transitions(equity, crypto)

        rows = await self._instrument_repo.list_all()
        evaluations: list[tuple[SelectionInputs, SelectionDecisionData]] = []
        for row in rows:
            inputs = self._service.build_inputs(row, equity, crypto)
            decision = evaluate_instrument(inputs)
            evaluations.append((inputs, decision))
            await self._persist_classification(inputs)
        metrics.increment("selection.evaluations", float(len(evaluations)))

        final = await self._watchlist.reconcile(evaluations)
        await self._persist_changed_decisions(final)

        eligible = sum(1 for decision in final.values() if decision.eligible)
        calendar_unavailable = sum(
            1
            for decision in final.values()
            if decision.eligibility_status is InstrumentEligibilityStatus.CALENDAR_UNAVAILABLE
        )
        if calendar_unavailable and self._last_outcome:  # notify once via transition logic
            pass
        self.last_run_at = self._now()
        self.last_run_summary = {
            "evaluated": len(final),
            "eligible": eligible,
            "ineligible": len(final) - eligible,
            "active_watchlist": self._watchlist.active_count(),
            "paused_watchlist": self._watchlist.paused_count(),
            "calendar_unavailable": calendar_unavailable,
            "equity_session": equity.state.value,
            "crypto_session": crypto.state.value,
            "run_at": self.last_run_at.isoformat(),
        }
        metrics.set_gauge("selection.instruments_eligible", float(eligible))
        metrics.set_gauge("selection.instruments_ineligible", float(len(final) - eligible))
        self.degraded = self._watchlist.degraded
        return self.last_run_summary

    async def _handle_session_transitions(self, equity: Any, crypto: Any) -> None:
        equity_state = equity.state.value if equity.calendar_available else "CALENDAR_UNAVAILABLE"
        if equity_state != self._last_equity_state:
            if self._last_equity_state is not None:
                with contextlib.suppress(Exception):
                    await self._session_event_repo.add(
                        "EQUITY",
                        self._last_equity_state,
                        equity_state,
                        equity.calendar_version,
                        {"detail": equity.detail},
                    )
                await self._notifier.session_transition(
                    "equity", self._last_equity_state, equity_state, equity.detail
                )
                if equity_state == "CALENDAR_UNAVAILABLE":
                    await self._notifier.calendar_unavailable(
                        equity.detail or "calendar unavailable"
                    )
            self._last_equity_state = equity_state
        crypto_state = crypto.state.value
        if crypto_state != self._last_crypto_state:
            if self._last_crypto_state is not None:
                with contextlib.suppress(Exception):
                    await self._session_event_repo.add(
                        "CRYPTO", self._last_crypto_state, crypto_state, None, None
                    )
                await self._notifier.session_transition(
                    "crypto", self._last_crypto_state, crypto_state, crypto.window_detail
                )
            self._last_crypto_state = crypto_state

    async def _persist_classification(self, inputs: SelectionInputs) -> None:
        """Append to classification history only when the class changed."""
        current = inputs.classification.asset_class.value
        if self._last_classification.get(inputs.instrument_pk) == current:
            return
        try:
            await self._selection_repo.add_classification(
                inputs.instrument_pk, inputs.symbol, inputs.classification
            )
            self._last_classification[inputs.instrument_pk] = current
            metrics.increment("selection.instruments_classified")
        except Exception as exc:
            self.degraded = True
            self._log.warning("classification_persist_failed", error=type(exc).__name__)

    async def _persist_changed_decisions(self, final: dict[int, SelectionDecisionData]) -> None:
        """Persist immutable decisions - only when the outcome changed
        (idempotent re-evaluation produces no redundant history rows)."""
        for pk, decision in final.items():
            outcome = (decision.eligibility_status.value, decision.selection_state.value)
            if self._last_outcome.get(pk) == outcome:
                continue
            try:
                await self._selection_repo.add_decision(decision)
                self._last_outcome[pk] = outcome
                self._log.info(
                    "selection_decision",
                    symbol=decision.symbol,
                    eligibility=decision.eligibility_status.value,
                    state=decision.selection_state.value,
                    score=decision.market_quality_score,
                )
            except Exception as exc:
                self.degraded = True
                self._log.error("decision_persist_failed", error=type(exc).__name__)

    # -------------------------------------------------------------- reports

    async def health_stats(self) -> dict[str, Any]:
        equity = self.last_equity_snapshot
        return {
            "state": self.state.value,
            "job_alive": self.alive,
            "active_watchlist": self._watchlist.active_count(),
            "paused_watchlist": self._watchlist.paused_count(),
            "calendar_available": bool(equity.calendar_available) if equity else None,
            "calendar_version": equity.calendar_version if equity else None,
            "calendar_unavailable_count": self.last_run_summary.get("calendar_unavailable"),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
        }

    async def active_watchlist_rows(self) -> list[Any]:
        """Raw active-watchlist rows for downstream research phases."""
        try:
            rows = await self._watchlist.repository.load_all()
        except Exception:
            return []
        return [row for row in rows if row.state == "WATCHLIST_ACTIVE"]

    async def watchlist_details(self) -> dict[str, Any] | None:
        """Active/paused watchlist rows for the status endpoint (no direction)."""
        try:
            entries = await self._watchlist.repository.load_all()
        except Exception:
            return None
        return {
            "active": [
                {
                    "symbol": row.symbol,
                    "asset_class": row.asset_class,
                    "quality_score": row.quality_score,
                    "eligibility_status": row.eligibility_status,
                    "state": row.state,
                }
                for row in entries
                if row.state == "WATCHLIST_ACTIVE"
            ],
            "paused": [
                {
                    "symbol": row.symbol,
                    "asset_class": row.asset_class,
                    "eligibility_status": row.eligibility_status,
                    "state": row.state,
                    "reasons": (row.reasons or {}).get("reasons", []),
                }
                for row in entries
                if row.state == "WATCHLIST_PAUSED"
            ],
        }

    async def dashboard_details(self) -> dict[str, Any] | None:
        """Recent decisions + watchlist events for the dashboard endpoint."""
        try:
            decisions = await self._selection_repo.recent_decisions(limit=30)
            events = await self._watchlist.repository.recent_events(limit=30)
        except Exception:
            return None
        return {
            "summary": self.last_run_summary,
            "recent_decisions": [
                {
                    "symbol": row.symbol,
                    "asset_class": row.asset_class,
                    "selection_state": row.selection_state,
                    "eligibility_status": row.eligibility_status,
                    "quality_score": row.market_quality_score,
                    "quality_components": row.quality_components,
                    "reasons": (row.reasons or {}).get("reasons", []),
                    "session_state": row.session_state,
                    "evaluated_at": row.evaluated_at.isoformat() if row.evaluated_at else None,
                }
                for row in decisions
            ],
            "recent_watchlist_events": [
                {
                    "symbol": row.symbol,
                    "event_type": row.event_type,
                    "from_state": row.from_state,
                    "to_state": row.to_state,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in events
            ],
        }

    async def status_stats(self) -> dict[str, Any]:
        stats = await self.health_stats()
        equity, crypto = self.last_equity_snapshot, self.last_crypto_snapshot
        stats.update(
            {
                "equity_session": equity.state.value if equity else None,
                "equity_next_transition_at": (
                    equity.next_transition_at.isoformat()
                    if equity and equity.next_transition_at
                    else None
                ),
                "equity_next_transition_state": (
                    equity.next_transition_state.value
                    if equity and equity.next_transition_state
                    else None
                ),
                "crypto_session": crypto.state.value if crypto else None,
                "last_run": self.last_run_summary,
                "config": {
                    "min_quality_score": self._settings.min_quality_score,
                    "max_watchlist_size": self._settings.max_watchlist_size,
                    "allowed_asset_classes": self._settings.allowed_asset_classes,
                    "allowlist": self._settings.default_allowlist,
                    "denylist": self._settings.denylist,
                    "require_volume": self._settings.require_volume,
                    "require_fresh_orderbook": self._settings.require_fresh_orderbook,
                    "allow_degraded_data": self._settings.allow_degraded_data,
                },
            }
        )
        return stats
