"""Internal signal lifecycle monitor job (phase 10).

Admits ELIGIBLE phase-9 plans into the research lifecycle (never
unchecked - every admission gate applies) and evaluates every
non-terminal signal against fresh public market data.  Overlap lock,
per-signal leases, bounded concurrency, optimistic locking, no REST
calls, no Telegram output.  In global PAUSED mode no signals are admitted
and no entries are confirmed.  DB outages degrade the subsystem; an
unpersisted transition is never reported as applied.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.bot_state import BotStateService
from app.config import SignalLifecycleSettings
from app.domain.enums import AssetClass
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.repositories.orm import RiskPlanRecord, SignalLifecycleRecord
from app.repositories.risk_plan_repository import RiskPlanRepository
from app.repositories.signal_event_repository import SignalEventRepository
from app.repositories.signal_lifecycle_repository import (
    SignalLifecycleRepository,
    TransitionOutcome,
)
from app.repositories.signal_rejection_repository import SignalRejectionRepository
from app.repositories.signal_update_repository import SignalUpdateRepository
from app.selection.market_selection_engine import session_verdict_for_asset_class
from app.selection.selection_scheduler import MarketSelectionCoordinator
from app.signals.admission import AdmissionContext, admit
from app.signals.enums import SignalRejectionCode, SignalSubsystemState, SignalUpdateType
from app.signals.event_builder import build_transition_event
from app.signals.explainability import (
    DISCLAIMER,
    dashboard_row,
    state_machine_document,
    status_row,
)
from app.signals.lifecycle import new_signal_from_plan
from app.signals.lifecycle_context import (
    SignalLifecycleContextBuilder,
    plan_snapshot_from_record,
)
from app.signals.lifecycle_engine import evaluate_signal
from app.signals.models import SignalRejection
from app.signals.rejection import build_admission_rejection, build_lifecycle_rejection
from app.signals.signal_update_builder import build_update
from app.signals.state_machine import TERMINAL_STATES
from app.signals.version import signal_dedupe_key

_TERMINAL_VALUES = {state.value for state in TERMINAL_STATES}


class SignalLifecycleMonitorJob:
    def __init__(
        self,
        settings: SignalLifecycleSettings,
        context_builder: SignalLifecycleContextBuilder,
        lifecycle_repo: SignalLifecycleRepository,
        event_repo: SignalEventRepository,
        update_repo: SignalUpdateRepository,
        rejection_repo: SignalRejectionRepository,
        plan_repo: RiskPlanRepository,
        selection: MarketSelectionCoordinator | None,
        bot_state: BotStateService | None,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._settings = settings
        self._builder = context_builder
        self._signals = lifecycle_repo
        self._events = event_repo
        self._updates = update_repo
        self._rejections = rejection_repo
        self._plans = plan_repo
        self._selection = selection
        self._bot_state = bot_state
        self._now = now_fn
        self._log = get_logger("signal_lifecycle")

        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        #: stable per-process lease owner; crash recovery reclaims by expiry
        self.lease_owner = f"lifecycle-{uuid.uuid4().hex[:12]}"

        self.degraded = False
        self.last_run_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_run_summary: dict[str, Any] = {}
        self.admissions_total = 0
        self.admission_rejections_total = 0
        self.transitions_total = 0
        self.conflicts_total = 0
        self.monitor_failures_total = 0

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="signal_lifecycle_monitor")

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
        """Immediate re-evaluation (new plan, market data burst, config)."""
        self._wakeup.set()

    @property
    def state(self) -> SignalSubsystemState:
        if not self._settings.lifecycle_enabled:
            return SignalSubsystemState.DISABLED
        if self._task is None or self._task.done():
            return SignalSubsystemState.UNAVAILABLE
        if self.degraded:
            return SignalSubsystemState.DEGRADED
        return SignalSubsystemState.HEALTHY

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.degraded = True
                self.monitor_failures_total += 1
                metrics.increment("signal.monitor_cycle_failures")
                self._log.error("signal_cycle_failed", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wakeup.wait(),
                    timeout=self._settings.lifecycle_monitor_refresh_seconds,
                )
            self._wakeup.clear()

    # ----------------------------------------------------------------- cycle

    async def run_once(self) -> dict[str, Any]:
        if self._lock.locked():
            metrics.increment("signal.overlap_skipped")
            return self.last_run_summary
        async with self._lock:
            return await self._cycle()

    async def _cycle(self) -> dict[str, Any]:
        started = time.monotonic()
        now = self._now()
        self.degraded = False
        paused = False
        if self._bot_state is not None:
            with contextlib.suppress(Exception):
                paused = await self._bot_state.is_paused()

        watchlist = await self._watchlist_entries()
        admitted = admission_rejected = admission_skipped = 0
        if paused:
            metrics.increment("signal.admission_paused_suppressed")
        else:
            admitted, admission_rejected, admission_skipped = await self._admission_phase(
                watchlist, now
            )

        evaluated, transitions, conflicts = await self._monitor_phase(watchlist, paused, now)

        self.last_run_at = now
        duration = time.monotonic() - started
        metrics.increment("signal.monitor_cycles")
        metrics.increment("signal.monitor_cycle_duration_seconds", duration)
        self.last_run_summary = {
            "run_at": now.isoformat(),
            "paused": paused,
            "admitted": admitted,
            "admission_rejected": admission_rejected,
            "admission_skipped": admission_skipped,
            "signals_evaluated": evaluated,
            "transitions_applied": transitions,
            "version_conflicts": conflicts,
            "duration_seconds": round(duration, 3),
        }
        if not self.degraded:
            self.last_success_at = now
        return self.last_run_summary

    # ------------------------------------------------------------- admission

    async def _admission_phase(
        self, watchlist: dict[int, dict[str, Any]], now: datetime
    ) -> tuple[int, int, int]:
        try:
            plans = await self._plans.active_plans()
        except Exception as exc:
            self.degraded = True
            self._log.error("signal_plan_load_failed", error=type(exc).__name__)
            return 0, 0, 0
        admitted = rejected = skipped = 0
        for row in plans:
            try:
                outcome = await self._admit_plan_row(row, watchlist, now)
            except Exception as exc:
                self.degraded = True
                self._log.error(
                    "signal_admission_failed",
                    symbol=getattr(row, "symbol", "?"),
                    error=type(exc).__name__,
                )
                with contextlib.suppress(Exception):
                    await self._record_plan_rejection(
                        row,
                        SignalRejectionCode.ADMISSION_ERROR,
                        f"admission failed: {type(exc).__name__}",
                        now,
                    )
                outcome = "rejected"
            if outcome == "created":
                admitted += 1
            elif outcome == "rejected":
                rejected += 1
            else:
                skipped += 1
        return admitted, rejected, skipped

    async def _admit_plan_row(
        self, row: RiskPlanRecord, watchlist: dict[int, dict[str, Any]], now: datetime
    ) -> str:
        """ "created" | "rejected" | "skipped" (already admitted / duplicate)."""
        if await self._signals.signal_exists_for_plan(row.id):
            return "skipped"  # one lifecycle per plan - idempotent across cycles
        plan = plan_snapshot_from_record(row)
        if plan is None:
            await self._record_plan_rejection(
                row,
                SignalRejectionCode.ADMISSION_CONFIGURATION_INVALID,
                "plan payload carries no complete model reference levels",
                now,
            )
            return "rejected"
        candidate = await self._builder.candidate_state(plan.candidate_id)
        market = await self._builder.market_state(plan.instrument_pk, plan.instrument_id, now)
        entry = watchlist.get(plan.instrument_pk)
        dedupe_key = signal_dedupe_key(
            plan_id=str(plan.plan_id),
            candidate_id=str(plan.candidate_id),
            lifecycle_model_version=self._settings.lifecycle_model_version,
            lifecycle_config_hash=self._builder.lifecycle_config_hash,
        )
        context = AdmissionContext(
            plan=plan,
            candidate=candidate,
            market=market,
            session_state=entry["session_state"] if entry else "UNKNOWN",
            session_allowed=bool(entry["session_allowed"]) if entry else False,
            watchlist_active=entry is not None,
            market_quality_score=entry["quality_score"] if entry else None,
            bot_paused=False,  # the paused gate short-circuits the whole phase
            active_signal_count=await self._signals.active_count_for_instrument(plan.instrument_pk),
            duplicate_exists=await self._signals.active_by_dedupe_key(dedupe_key) is not None,
            instrument_snapshot_age_seconds=(
                await self._builder.instrument_snapshot_age_seconds(plan.instrument_pk, now)
            ),
            as_of=now,
        )
        decision = admit(context, self._settings)
        if not decision.admitted:
            assert decision.code is not None
            rejection = build_admission_rejection(
                plan,
                decision.code,
                decision.detail,
                lifecycle_model_version=self._settings.lifecycle_model_version,
                lifecycle_config_hash=self._builder.lifecycle_config_hash,
                as_of=now,
            )
            await self._persist_rejection(rejection)
            metrics.increment(f"signal.admission_rejections.{decision.code.value.lower()}")
            return "rejected"
        assert decision.trigger is not None
        new = new_signal_from_plan(
            plan,
            trigger=decision.trigger,
            settings=self._settings,
            lifecycle_config_hash=self._builder.lifecycle_config_hash,
            approximation_flags=decision.approximation_flags,
            now=now,
            correlation_id=uuid.uuid4().hex,
        )
        if not await self._signals.insert_new(new, self._settings.lifecycle_event_schema_version):
            metrics.increment("signal.admission_dedupe_suppressed")
            return "skipped"  # lost a race - the existing signal stands
        self.admissions_total += 1
        metrics.increment("signal.admissions")
        self._log.info(
            "signal_admitted",
            symbol=plan.symbol,
            candidate_type=plan.candidate_type,
            entry_trigger=decision.trigger.value,
            note="internal research lifecycle - no trade signal, no position",
        )
        return "created"

    # --------------------------------------------------------------- monitor

    async def _monitor_phase(
        self, watchlist: dict[int, dict[str, Any]], paused: bool, now: datetime
    ) -> tuple[int, int, int]:
        try:
            rows = await self._signals.non_terminal()
        except Exception as exc:
            self.degraded = True
            self._log.error("signal_load_failed", error=type(exc).__name__)
            return 0, 0, 0
        metrics.set_gauge("signal.active_count", float(len(rows)))
        with contextlib.suppress(Exception):
            expired = await self._signals.expired_lease_count(now)
            metrics.set_gauge("signal.expired_leases", float(expired))

        evaluated = 0
        transitions = 0
        conflicts = 0
        semaphore = asyncio.Semaphore(max(1, self._settings.lifecycle_max_concurrent_monitors))

        async def monitor_one(row: SignalLifecycleRecord) -> None:
            nonlocal evaluated, transitions, conflicts
            async with semaphore:
                claimed = False
                try:
                    claimed = await self._signals.claim(
                        row.id, self.lease_owner, now, self._settings.lifecycle_lease_seconds
                    )
                    if not claimed:
                        metrics.increment("signal.lease_contention")
                        return
                    applied, conflicted = await self._evaluate_row(row, watchlist, paused, now)
                    evaluated += 1
                    transitions += applied
                    conflicts += conflicted
                except Exception as exc:
                    self.degraded = True
                    self.monitor_failures_total += 1
                    metrics.increment("signal.monitor_failures")
                    self._log.error(
                        "signal_monitor_failed",
                        signal_id=str(row.id),
                        error=type(exc).__name__,
                    )
                finally:
                    if claimed:
                        with contextlib.suppress(Exception):
                            await self._signals.release(row.id, self.lease_owner)

        await asyncio.gather(*(monitor_one(row) for row in rows))
        return evaluated, transitions, conflicts

    async def _evaluate_row(
        self,
        row: SignalLifecycleRecord,
        watchlist: dict[int, dict[str, Any]],
        paused: bool,
        now: datetime,
    ) -> tuple[int, int]:
        """(#transitions applied, #version conflicts) for one signal."""
        entry = watchlist.get(row.instrument_pk)
        context = await self._builder.monitor_context(
            row,
            session_state=entry["session_state"] if entry else "UNKNOWN",
            session_allowed=bool(entry["session_allowed"]) if entry else False,
            watchlist_active=entry is not None,
            market_quality_score=entry["quality_score"] if entry else None,
            bot_paused=paused,
            as_of=now,
        )
        result = evaluate_signal(context, self._settings)

        if self._settings.lifecycle_persist_updates:
            await self._persist_updates(row.id, result.updates, now)
        if result.suppressed_findings:
            metrics.increment("signal.invalid_transitions_suppressed")
            await self._persist_rejection(
                build_lifecycle_rejection(
                    signal_id=row.id,
                    plan_id=row.plan_id,
                    candidate_id=row.candidate_id,
                    instrument_pk=row.instrument_pk,
                    symbol=row.symbol,
                    code=SignalRejectionCode.INVALID_TRANSITION,
                    detail=result.suppressed_findings[0],
                    lifecycle_model_version=self._settings.lifecycle_model_version,
                    lifecycle_config_hash=self._builder.lifecycle_config_hash,
                    as_of=now,
                )
            )
        for warning in result.warnings:
            self._log.warning("signal_engine_warning", signal_id=str(row.id), detail=warning)

        applied = 0
        conflicts = 0
        expected = int(row.state_version)
        for step in result.transitions:
            event = build_transition_event(
                signal_id=row.id,
                step=step,
                expected_state_version=expected,
                event_schema_version=self._settings.lifecycle_event_schema_version,
                as_of=now,
                correlation_id=row.correlation_id,
            )
            outcome = await self._signals.apply_transition(row.id, expected, step, event, as_of=now)
            if outcome is TransitionOutcome.APPLIED:
                expected += 1
                applied += 1
                self.transitions_total += 1
                metrics.increment("signal.transitions")
                metrics.increment(f"signal.transitions.{step.to_state.value.lower()}")
                if step.to_state.value in _TERMINAL_VALUES:
                    metrics.increment(f"signal.terminal.{step.to_state.value.lower()}")
                self._log.info(
                    "signal_transition",
                    signal_id=str(row.id),
                    symbol=row.symbol,
                    from_state=step.from_state.value,
                    to_state=step.to_state.value,
                    reason=step.reason,
                    note="research lifecycle state - no order, no fill, no position",
                )
            elif outcome is TransitionOutcome.CONFLICT:
                conflicts += 1
                self.conflicts_total += 1
                metrics.increment("signal.state_version_conflicts")
                await self._persist_rejection(
                    build_lifecycle_rejection(
                        signal_id=row.id,
                        plan_id=row.plan_id,
                        candidate_id=row.candidate_id,
                        instrument_pk=row.instrument_pk,
                        symbol=row.symbol,
                        code=SignalRejectionCode.STATE_VERSION_CONFLICT,
                        detail=(
                            f"optimistic lock lost at version {expected} for "
                            f"{step.from_state.value} -> {step.to_state.value}"
                        ),
                        lifecycle_model_version=self._settings.lifecycle_model_version,
                        lifecycle_config_hash=self._builder.lifecycle_config_hash,
                        as_of=now,
                    )
                )
                break  # re-read next cycle; the winner's state stands
            elif outcome is TransitionOutcome.FAILED:
                self.degraded = True
                metrics.increment("signal.transition_persist_failures")
                self._log.error(
                    "signal_transition_persist_failed",
                    signal_id=str(row.id),
                    to_state=step.to_state.value,
                )
                break  # never report an unpersisted transition as applied
            else:  # NOT_FOUND
                break

        market = context.market
        healthy = market.data_quality_ok and not market.book_resyncing
        with contextlib.suppress(Exception):
            await self._signals.update_monitoring_fields(
                row.id,
                last_evaluated_at=now,
                last_market_data_at=market.snapshot_at,
                data_quality_status=market.data_quality_status,
                session_state=context.session_state,
                data_degraded_since=(
                    None if healthy else (context.signal.data_degraded_since or now)
                ),
                clear_degraded=healthy,
            )
        return applied, conflicts

    # ------------------------------------------------------------- helpers

    async def _persist_updates(
        self,
        signal_id: uuid.UUID,
        updates: tuple[tuple[SignalUpdateType, str], ...],
        now: datetime,
    ) -> None:
        for update_type, detail in updates:
            try:
                created = await self._updates.add_update(
                    build_update(
                        signal_id=signal_id,
                        update_type=update_type,
                        detail=detail,
                        update_schema_version=self._settings.lifecycle_update_schema_version,
                        as_of=now,
                        dedupe_window_seconds=self._settings.lifecycle_dedupe_seconds,
                    )
                )
                if created:
                    metrics.increment("signal.updates")
                else:
                    metrics.increment("signal.updates_aggregated")
            except Exception as exc:
                self.degraded = True
                self._log.error("signal_update_persist_failed", error=type(exc).__name__)
                return

    async def _persist_rejection(self, rejection: SignalRejection) -> None:
        self.admission_rejections_total += 1 if rejection.signal_id is None else 0
        try:
            await self._rejections.record_rejection(
                rejection, self._settings.lifecycle_rejection_persist_seconds
            )
        except Exception as exc:
            self.degraded = True
            self._log.error("signal_rejection_persist_failed", error=type(exc).__name__)

    async def _record_plan_rejection(
        self, row: RiskPlanRecord, code: SignalRejectionCode, detail: str, now: datetime
    ) -> None:
        """Rejection for a plan row that never became a PlanSnapshot."""
        rejection = SignalRejection(
            rejection_id=uuid.uuid4(),
            plan_id=row.id,
            candidate_id=row.candidate_id,
            signal_id=None,
            instrument_pk=row.instrument_pk,
            symbol=row.symbol,
            as_of=now,
            primary_code=code,
            codes=(code,),
            detail=detail,
            lifecycle_model_version=self._settings.lifecycle_model_version,
            lifecycle_config_hash=self._builder.lifecycle_config_hash,
        )
        await self._persist_rejection(rejection)
        metrics.increment(f"signal.admission_rejections.{code.value.lower()}")

    async def _watchlist_entries(self) -> dict[int, dict[str, Any]]:
        """instrument_pk -> session/watchlist context for this cycle."""
        entries: dict[int, dict[str, Any]] = {}
        if self._selection is None:
            return entries
        try:
            rows = await self._selection.active_watchlist_rows()
        except Exception:
            self.degraded = True
            return entries
        equity = self._selection.last_equity_snapshot
        crypto = self._selection.last_crypto_snapshot
        for row in rows:
            session_state, session_allowed = "UNKNOWN", False
            if equity is not None and crypto is not None:
                try:
                    verdict = session_verdict_for_asset_class(
                        AssetClass(row.asset_class), equity, crypto
                    )
                    session_state = verdict.session_state
                    session_allowed = verdict.blocking_status is None
                except ValueError:
                    pass
            entries[row.instrument_pk] = {
                "session_state": session_state,
                "session_allowed": session_allowed,
                "quality_score": (
                    int(row.quality_score) if row.quality_score is not None else None
                ),
            }
        return entries

    # -------------------------------------------------------------- reports

    async def health_stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        rejections_last_hour = 0
        with contextlib.suppress(Exception):
            counts = await self._signals.counts_by_state()
        with contextlib.suppress(Exception):
            rejections_last_hour = await self._rejections.count_since(
                self._now() - timedelta(hours=1)
            )
        return {
            "state": self.state.value,
            "job_alive": self.alive,
            "signal_counts_by_state": counts,
            "rejections_last_hour": rejections_last_hour,
            "admissions_total": self.admissions_total,
            "transitions_total": self.transitions_total,
            "version_conflicts_total": self.conflicts_total,
            "monitor_failures_total": self.monitor_failures_total,
            "last_success_at": (self.last_success_at.isoformat() if self.last_success_at else None),
        }

    async def status_stats(self) -> dict[str, Any]:
        """Status view - lifecycle states and versions, NO price levels."""
        stats = await self.health_stats()
        recent: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            recent = [status_row(row) for row in await self._signals.recent(limit=10)]
        stats.update(
            {
                "lifecycle_model": (
                    f"{self._settings.lifecycle_model_name}"
                    f"@{self._settings.lifecycle_model_version}"
                ),
                "lifecycle_config_hash": self._builder.lifecycle_config_hash,
                "event_schema_version": self._settings.lifecycle_event_schema_version,
                "update_schema_version": self._settings.lifecycle_update_schema_version,
                "recent_signals": recent,
                "last_run": self.last_run_summary,
                "note": DISCLAIMER,
            }
        )
        return stats

    async def dashboard_details(self) -> dict[str, Any] | None:
        """Local dashboard - model reference values clearly labelled."""
        try:
            rows = await self._signals.recent(limit=10)
            details = []
            for row in rows:
                events = await self._events.events_for(row.id)
                updates = await self._updates.updates_for(row.id)
                details.append(dashboard_row(row, events, updates))
            rejections = await self._rejections.recent(limit=20)
            counts = await self._signals.counts_by_state()
        except Exception:
            return None
        return {
            "disclaimer": DISCLAIMER,
            "signal_counts_by_state": counts,
            "signals": details,
            "state_machine": state_machine_document(),
            "recent_rejections": [
                {
                    "symbol": row.symbol,
                    "code": row.primary_code,
                    "count": row.count,
                    "detail": row.detail,
                    "last_as_of": row.last_as_of.isoformat(),
                    "lifecycle_model_version": row.lifecycle_model_version,
                }
                for row in rejections
            ],
        }
