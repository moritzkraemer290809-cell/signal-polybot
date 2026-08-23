"""Risk plan evaluation refresh job (phase 9).

Evaluates only CONFIRMED phase-8 research candidates on ACTIVE watchlist
instruments, with per-candidate re-evaluation discipline (new or materially
updated candidates, forced triggers, or the dedupe window elapsing).
Overlap lock, bounded concurrency, no REST calls, no Telegram output.  In
global PAUSED mode no new eligible plans are created.  DB outages degrade
the subsystem; unpersisted plan state is never reported as successful.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.bot_state import BotStateService
from app.config import CostSettings, RiskSettings
from app.costs.enums import CostSubsystemState
from app.costs.execution_assumptions import build_execution_assumptions
from app.costs.fee_schedule import build_default_schedule, snapshot_from_schedule
from app.costs.models import CostModelError, FeeScheduleSnapshot
from app.domain.enums import AssetClass
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.repositories.cost_estimate_repository import CostEstimateRepository
from app.repositories.fee_schedule_repository import (
    ExecutionAssumptionRepository,
    FeeScheduleRepository,
)
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.instrument_risk_repository import InstrumentRiskRepository
from app.repositories.risk_plan_rejection_repository import RiskPlanRejectionRepository
from app.repositories.risk_plan_repository import RiskPlanRepository
from app.repositories.setup_candidate_repository import SetupCandidateRepository
from app.risk.enums import PlanRejectionCode, PlanStatus, RiskSubsystemState
from app.risk.evaluation_context import (
    ContextBuildError,
    RiskPlanEvaluationContextBuilder,
    candidate_snapshot_from_record,
)
from app.risk.explainability import DISCLAIMER
from app.risk.models import RiskPlanRejection, SignalEligibilityPlan
from app.risk.risk_engine import evaluate_candidate
from app.selection.market_selection_engine import session_verdict_for_asset_class
from app.selection.selection_scheduler import MarketSelectionCoordinator

_DATA_CODES = {
    PlanRejectionCode.DATA_QUALITY_INSUFFICIENT,
    PlanRejectionCode.ORDERBOOK_NOT_FRESH,
    PlanRejectionCode.INSTRUMENT_RISK_DATA_MISSING,
}
_MODEL_REJECTION_METRICS = {
    PlanRejectionCode.SLIPPAGE_MODEL_UNAVAILABLE: "risk.slippage_model_rejections",
    PlanRejectionCode.SLIPPAGE_EXCESSIVE: "risk.slippage_model_rejections",
    PlanRejectionCode.ORDERBOOK_DEPTH_INSUFFICIENT: "risk.slippage_model_rejections",
    PlanRejectionCode.FUNDING_MODEL_UNAVAILABLE: "risk.funding_model_rejections",
    PlanRejectionCode.FUNDING_COST_EXCESSIVE: "risk.funding_model_rejections",
    PlanRejectionCode.MARGIN_MODEL_UNAVAILABLE: "risk.margin_model_rejections",
    PlanRejectionCode.MAINTENANCE_MARGIN_UNAVAILABLE: "risk.margin_model_rejections",
    PlanRejectionCode.LIQUIDATION_BUFFER_INSUFFICIENT: "risk.liquidation_buffer_rejections",
    PlanRejectionCode.FEE_SCHEDULE_UNAVAILABLE: "risk.fee_schedule_unavailable",
}


def _bucket(value: float, edges: list[float]) -> str:
    for edge in edges:
        if value < edge:
            return f"lt_{edge:g}"
    return f"ge_{edges[-1]:g}"


class RiskPlanEvaluationJob:
    def __init__(
        self,
        risk_settings: RiskSettings,
        cost_settings: CostSettings,
        context_builder: RiskPlanEvaluationContextBuilder,
        plan_repo: RiskPlanRepository,
        rejection_repo: RiskPlanRejectionRepository,
        cost_repo: CostEstimateRepository,
        snapshot_repo: InstrumentRiskRepository,
        fee_repo: FeeScheduleRepository,
        assumption_repo: ExecutionAssumptionRepository,
        candidate_repo: SetupCandidateRepository,
        instrument_repo: InstrumentRepository,
        selection: MarketSelectionCoordinator | None,
        bot_state: BotStateService | None,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._risk = risk_settings
        self._costs = cost_settings
        self._builder = context_builder
        self._plans = plan_repo
        self._rejections = rejection_repo
        self._cost_estimates = cost_repo
        self._snapshots = snapshot_repo
        self._fees = fee_repo
        self._assumption_versions = assumption_repo
        self._candidates = candidate_repo
        self._instruments = instrument_repo
        self._selection = selection
        self._bot_state = bot_state
        self._now = now_fn
        self._log = get_logger("risk_plan_evaluation")

        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._force_all = False
        #: candidate_id -> (last evaluated at, candidate updated_at marker)
        self._last_evaluated: dict[uuid.UUID, tuple[datetime, str]] = {}
        self._fee_snapshot: FeeScheduleSnapshot | None = None

        self.degraded = False
        self.fee_schedule_available = False
        self.last_run_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_run_summary: dict[str, Any] = {}
        self.evaluations_total = 0
        self.evaluations_skipped = 0
        self.evaluations_failed = 0

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="risk_plan_evaluation")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def trigger(self, *, force: bool = True) -> None:
        """Immediate re-evaluation (candidate/BBO/fee/config change)."""
        self._force_all = self._force_all or force
        self._wakeup.set()

    @property
    def state(self) -> RiskSubsystemState:
        if not self._risk.engine_enabled:
            return RiskSubsystemState.DISABLED
        if self._task is None or self._task.done():
            return RiskSubsystemState.UNAVAILABLE
        if self.degraded:
            return RiskSubsystemState.DEGRADED
        return RiskSubsystemState.HEALTHY

    @property
    def cost_state(self) -> CostSubsystemState:
        if not self._costs.engine_enabled:
            return CostSubsystemState.DISABLED
        if not self.fee_schedule_available:
            return CostSubsystemState.UNAVAILABLE
        if self.degraded:
            return CostSubsystemState.DEGRADED
        return CostSubsystemState.HEALTHY

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.degraded = True
                self.evaluations_failed += 1
                metrics.increment("risk.plan_evaluations_failed")
                self._log.error("risk_cycle_failed", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wakeup.wait(), timeout=self._risk.plan_evaluation_refresh_seconds
                )
            self._wakeup.clear()

    # ----------------------------------------------------------------- cycle

    async def run_once(self) -> dict[str, Any]:
        if self._lock.locked():
            metrics.increment("risk.overlap_skipped")
            return self.last_run_summary
        async with self._lock:
            return await self._cycle()

    async def _cycle(self) -> dict[str, Any]:
        started = time.monotonic()
        now = self._now()
        self.degraded = False
        force = self._force_all
        self._force_all = False
        paused = False
        if self._bot_state is not None:
            with contextlib.suppress(Exception):
                paused = await self._bot_state.is_paused()

        fee_snapshot = await self._ensure_fee_schedule()
        watchlist = await self._watchlist_entries(now)
        candidates = await self._eligible_candidates()
        evaluated = skipped = plans_created = 0
        semaphore = asyncio.Semaphore(max(1, self._risk.max_concurrent_evaluations))

        async def evaluate_one(row: Any) -> None:
            nonlocal evaluated, skipped, plans_created
            async with semaphore:
                marker = str(row.updated_at)
                if not force and not self._needs_evaluation(row.id, marker, now):
                    skipped += 1
                    self.evaluations_skipped += 1
                    metrics.increment("risk.plan_evaluations_skipped")
                    return
                try:
                    result = await self._evaluate_candidate_row(
                        row, watchlist, fee_snapshot, now, paused
                    )
                except Exception as exc:
                    # one bad candidate must never abort the whole cycle
                    result = "failed"
                    self.evaluations_failed += 1
                    metrics.increment("risk.plan_evaluations_failed")
                    self._log.error(
                        "risk_candidate_evaluation_failed",
                        symbol=getattr(row, "symbol", "?"),
                        error=type(exc).__name__,
                    )
                    with contextlib.suppress(Exception):
                        await self._record_rejection_direct(
                            candidate_snapshot_from_record(row),
                            PlanRejectionCode.EVALUATION_ERROR,
                            f"evaluation failed: {type(exc).__name__}",
                            now,
                        )
                evaluated += 1
                self.evaluations_total += 1
                metrics.increment("risk.plan_evaluations_total")
                if result == "created":
                    plans_created += 1
                if result != "failed":
                    # a persistence failure keeps the candidate due for retry
                    self._last_evaluated[row.id] = (now, marker)

        await asyncio.gather(*(evaluate_one(row) for row in candidates))
        await self._expire_and_mirror_plans(now)
        self._prune_memory(now)

        self.last_run_at = now
        duration = time.monotonic() - started
        metrics.increment("risk.plan_evaluation_duration_seconds", duration)
        self.last_run_summary = {
            "run_at": now.isoformat(),
            "confirmed_candidates": len(candidates),
            "evaluated": evaluated,
            "skipped": skipped,
            "plans_created": plans_created,
            "paused": paused,
            "fee_schedule": fee_snapshot.version if fee_snapshot else None,
            "duration_seconds": round(duration, 3),
        }
        if not self.degraded:
            self.last_success_at = now
        return self.last_run_summary

    # ------------------------------------------------------------- helpers

    def _needs_evaluation(self, candidate_id: uuid.UUID, marker: str, now: datetime) -> bool:
        previous = self._last_evaluated.get(candidate_id)
        if previous is None:
            return True
        last_at, last_marker = previous
        if last_marker != marker:
            return True  # candidate materially updated
        return (now - last_at).total_seconds() >= self._risk.plan_dedupe_seconds

    async def _ensure_fee_schedule(self) -> FeeScheduleSnapshot | None:
        """Seed/activate the administered fee schedule and snapshot it."""
        try:
            active = await self._fees.active_schedule()
            if active is None or active.version != self._costs.fee_schedule_version:
                await self._fees.seed(
                    self._costs.fee_schedule_version,
                    build_default_schedule(self._costs),
                    self._costs.fee_schedule_source,
                )
                await self._fees.activate(self._costs.fee_schedule_version)
                active = await self._fees.active_schedule()
            if active is None:
                self.fee_schedule_available = False
                metrics.increment("risk.fee_schedule_unavailable")
                return None
            snapshot = snapshot_from_schedule(
                active.schedule, self._costs, active=True, effective_at=active.effective_at
            )
            await self._assumption_versions.seed_and_activate(
                self._costs.execution_assumption_version,
                build_execution_assumptions(self._costs).as_dict(),
            )
            self._fee_snapshot = snapshot
            self.fee_schedule_available = True
            return snapshot
        except CostModelError:
            self.fee_schedule_available = False
            metrics.increment("risk.fee_schedule_unavailable")
            return None
        except Exception as exc:
            self.degraded = True
            self.fee_schedule_available = self._fee_snapshot is not None
            self._log.error("fee_schedule_load_failed", error=type(exc).__name__)
            return self._fee_snapshot

    async def _watchlist_entries(self, now: datetime) -> dict[int, dict[str, Any]]:
        """instrument_pk -> session/watchlist context."""
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
            remaining: float | None = None
            if equity is not None and crypto is not None:
                try:
                    verdict = session_verdict_for_asset_class(
                        AssetClass(row.asset_class), equity, crypto
                    )
                    session_state = verdict.session_state
                    session_allowed = verdict.blocking_status is None
                except ValueError:
                    pass
            if (
                row.asset_class == "EQUITY"
                and equity is not None
                and equity.next_transition_at is not None
            ):
                remaining = max(0.0, (equity.next_transition_at - now).total_seconds() / 60.0)
            entries[row.instrument_pk] = {
                "session_state": session_state,
                "session_allowed": session_allowed,
                "equity_session_remaining_minutes": remaining,
            }
        return entries

    async def _eligible_candidates(self) -> list[Any]:
        try:
            rows = await self._candidates.active_candidates()
        except Exception:
            self.degraded = True
            return []
        if self._risk.require_confirmed_candidate:
            rows = [row for row in rows if row.state == "CONFIRMED"]
        return rows

    async def _evaluate_candidate_row(
        self,
        row: Any,
        watchlist: dict[int, dict[str, Any]],
        fee_snapshot: FeeScheduleSnapshot | None,
        now: datetime,
        paused: bool,
    ) -> str:
        """\"created\" | \"suppressed\" | \"rejected\" | \"failed\"."""
        candidate = candidate_snapshot_from_record(row)
        entry = watchlist.get(candidate.instrument_pk)
        try:
            instrument = await self._instruments.get_by_symbol(candidate.symbol)
        except Exception:
            self.degraded = True
            return "failed"
        if instrument is None:
            await self._record_rejection_direct(
                candidate,
                PlanRejectionCode.INSTRUMENT_INACTIVE,
                "instrument no longer present in discovery",
                now,
            )
            return "rejected"
        try:
            context = await self._builder.build(
                candidate=candidate,
                instrument=instrument,
                watchlist_active=entry is not None,
                bot_paused=paused,
                session_state=entry["session_state"] if entry else "UNKNOWN",
                session_allowed=entry["session_allowed"] if entry else False,
                equity_session_remaining_minutes=(
                    entry["equity_session_remaining_minutes"] if entry else None
                ),
                fee_schedule=fee_snapshot,
                as_of=now,
            )
        except ContextBuildError as error:
            await self._record_rejection_direct(candidate, error.code, error.detail, now)
            return "rejected"
        except Exception as exc:
            self.evaluations_failed += 1
            metrics.increment("risk.plan_evaluations_failed")
            self._log.error(
                "risk_context_build_failed", symbol=candidate.symbol, error=type(exc).__name__
            )
            return "failed"

        outcome = evaluate_candidate(context, self._risk, self._costs)
        if outcome.rejection is not None:
            await self._handle_rejection(outcome.rejection, now)
            return "rejected"
        assert outcome.plan is not None
        if paused:
            # defensive double gate: never admit new eligible plans while paused
            return "suppressed"
        return await self._admit_plan(outcome.plan, context, now)

    async def _admit_plan(self, plan: SignalEligibilityPlan, context: Any, now: datetime) -> str:
        """Admit an eligible plan: "created" | "suppressed" | "failed".

        Order matters: the replacement plan is INSERTED first; only a
        persisted replacement may supersede its predecessors.  A suppressed
        or failed insert leaves existing plans untouched."""
        try:
            existing = await self._plans.active_by_dedupe_key(plan.dedupe_key)
            if existing is not None:
                metrics.increment("risk.plan_dedupe_suppressed")
                return "suppressed"
            actives = await self._plans.active_plans(plan.instrument_pk)
            predecessors = [row for row in actives if row.candidate_id == plan.candidate_id]
            others = [row for row in actives if row.candidate_id != plan.candidate_id]
            # predecessors will be superseded by this plan, so only OTHER
            # candidates' plans count against the per-instrument cap
            if len(others) >= self._risk.max_active_plans_per_instrument:
                metrics.increment("risk.plan_dedupe_suppressed")
                return "suppressed"
            snapshot_id = uuid.uuid4()
            if not await self._plans.insert_new(plan, instrument_snapshot_id=snapshot_id):
                # distinguish a dedupe race from a real persistence failure
                if await self._plans.active_by_dedupe_key(plan.dedupe_key) is not None:
                    metrics.increment("risk.plan_dedupe_suppressed")
                    return "suppressed"
                raise RuntimeError("plan insert rejected without a dedupe conflict")
            # replacement is persisted - now retire the superseded predecessors
            for old in predecessors:
                if await self._plans.transition(
                    old.id, PlanStatus.SUPERSEDED, {"superseded_by": str(plan.plan_id)}
                ):
                    metrics.increment("risk.plans_superseded")
            await self._snapshots.add_snapshot(snapshot_id, context.instrument)
            await self._cost_estimates.add(
                plan.costs,
                estimate_id=uuid.uuid4(),
                plan_id=plan.plan_id,
                candidate_id=plan.candidate_id,
                instrument_pk=plan.instrument_pk,
                symbol=plan.symbol,
                as_of=plan.as_of,
            )
            metrics.increment("risk.plans_eligible")
            metrics.increment("risk.cost_estimates_created")
            if plan.net_rr_primary is not None:
                metrics.increment(
                    f"risk.net_rr_bucket.{_bucket(plan.net_rr_primary, [1.8, 2.5, 3.5])}"
                )
            metrics.increment(
                f"risk.cost_to_risk_bucket.{_bucket(plan.costs.cost_to_risk_pct, [5, 10, 15])}"
            )
            self._log.info(
                "risk_plan_recorded",
                symbol=plan.symbol,
                candidate_type=plan.candidate_type,
                eligibility_score=plan.eligibility_score,
                note="research eligibility plan - not a trade signal",
            )
            return "created"
        except Exception as exc:
            # DB down: never report an unpersisted plan as successful
            self.degraded = True
            self._log.error("risk_plan_persist_failed", error=type(exc).__name__)
            return "failed"

    async def _handle_rejection(self, rejection: RiskPlanRejection, now: datetime) -> None:
        metrics.increment(f"risk.plan_rejections.{rejection.primary_code.value.lower()}")
        metric = _MODEL_REJECTION_METRICS.get(rejection.primary_code)
        if metric is not None:
            metrics.increment(metric)
        try:
            await self._rejections.record_rejection(rejection, self._risk.rejection_persist_seconds)
        except Exception as exc:
            self.degraded = True
            self._log.error("risk_rejection_persist_failed", error=type(exc).__name__)
        if rejection.candidate_id is None:
            return
        mirror = {
            PlanRejectionCode.CANDIDATE_EXPIRED: PlanStatus.EXPIRED,
            PlanRejectionCode.CANDIDATE_SUPERSEDED: PlanStatus.SUPERSEDED,
        }.get(rejection.primary_code)
        if mirror is None and rejection.primary_code in _DATA_CODES:
            mirror = PlanStatus.DATA_INVALID
        if mirror is None:
            return
        with contextlib.suppress(Exception):
            for plan in await self._plans.plans_for_candidate(rejection.candidate_id):
                if plan.status == PlanStatus.ELIGIBLE.value and await self._plans.transition(
                    plan.id, mirror, {"reason": rejection.primary_code.value}
                ):
                    metrics.increment(
                        "risk.plans_expired"
                        if mirror is PlanStatus.EXPIRED
                        else "risk.plans_superseded"
                        if mirror is PlanStatus.SUPERSEDED
                        else "risk.plans_data_invalid"
                    )

    async def _record_rejection_direct(
        self, candidate: Any, code: PlanRejectionCode, detail: str, now: datetime
    ) -> None:
        rejection = RiskPlanRejection(
            rejection_id=uuid.uuid4(),
            candidate_id=candidate.candidate_id,
            instrument_pk=candidate.instrument_pk,
            instrument_id=candidate.instrument_id,
            symbol=candidate.symbol,
            as_of=now,
            primary_code=code,
            codes=(code,),
            detail=detail,
            risk_model_name=self._risk.model_name,
            risk_model_version=self._risk.model_version,
            risk_config_hash=self._builder.risk_config_hash,
            cost_model_version=self._costs.model_version,
            cost_config_hash=self._builder.cost_config_hash,
            fee_schedule_version=(
                self._fee_snapshot.version if self._fee_snapshot is not None else None
            ),
        )
        await self._handle_rejection(rejection, now)

    async def _expire_and_mirror_plans(self, now: datetime) -> None:
        try:
            candidate_states: dict[uuid.UUID, str] = {}
            for plan in await self._plans.active_plans():
                expiry = (
                    plan.expiry_at if plan.expiry_at.tzinfo else plan.expiry_at.replace(tzinfo=UTC)
                )
                if expiry <= now:
                    if await self._plans.transition(
                        plan.id, PlanStatus.EXPIRED, {"reason": "plan expiry"}
                    ):
                        metrics.increment("risk.plans_expired")
                    continue
                state = candidate_states.get(plan.candidate_id)
                if state is None:
                    rows = await self._candidates.events_for(plan.candidate_id)
                    state = (rows[-1].to_state or "UNKNOWN") if rows else "UNKNOWN"
                    candidate_states[plan.candidate_id] = state
                if state in ("EXPIRED", "REJECTED"):
                    if await self._plans.transition(
                        plan.id, PlanStatus.EXPIRED, {"reason": f"candidate {state}"}
                    ):
                        metrics.increment("risk.plans_expired")
                elif state == "SUPERSEDED":
                    if await self._plans.transition(
                        plan.id, PlanStatus.SUPERSEDED, {"reason": "candidate superseded"}
                    ):
                        metrics.increment("risk.plans_superseded")
        except Exception as exc:
            self.degraded = True
            self._log.error("risk_plan_expiry_failed", error=type(exc).__name__)

    def _prune_memory(self, now: datetime) -> None:
        horizon = self._risk.plan_dedupe_seconds * 4
        self._last_evaluated = {
            key: value
            for key, value in self._last_evaluated.items()
            if (now - value[0]).total_seconds() < horizon
        }

    # -------------------------------------------------------------- reports

    async def health_stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        rejections_last_hour = 0
        with contextlib.suppress(Exception):
            counts = await self._plans.counts_by_status()
        with contextlib.suppress(Exception):
            from datetime import timedelta

            rejections_last_hour = await self._rejections.count_since(
                self._now() - timedelta(hours=1)
            )
        return {
            "state": self.state.value,
            "cost_state": self.cost_state.value,
            "job_alive": self.alive,
            "fee_schedule_available": self.fee_schedule_available,
            "plan_counts_by_status": counts,
            "rejections_last_hour": rejections_last_hour,
            "evaluations_total": self.evaluations_total,
            "evaluations_failed": self.evaluations_failed,
            "last_success_at": (self.last_success_at.isoformat() if self.last_success_at else None),
        }

    async def status_stats(self) -> dict[str, Any]:
        stats = await self.health_stats()
        stats.update(
            {
                "risk_model": f"{self._risk.model_name}@{self._risk.model_version}",
                "cost_model": f"{self._costs.model_name}@{self._costs.model_version}",
                "risk_config_hash": self._builder.risk_config_hash,
                "cost_config_hash": self._builder.cost_config_hash,
                "fee_schedule_version": (
                    f"{self._fee_snapshot.version} (assumption only)"
                    if self._fee_snapshot
                    else None
                ),
                "execution_assumption_version": self._costs.execution_assumption_version,
                "last_run": self.last_run_summary,
                "note": (
                    "Research eligibility plans only - no trade signals, no real "
                    "account or position data."
                ),
            }
        )
        return stats

    async def dashboard_details(self) -> dict[str, Any] | None:
        try:
            plans = await self._plans.recent(limit=10)
            rejections = await self._rejections.recent(limit=20)
            counts = await self._plans.counts_by_status()
        except Exception:
            return None
        return {
            "disclaimer": DISCLAIMER,
            "plan_counts_by_status": counts,
            "recent_plans": [row.payload or {"plan_id": str(row.id)} for row in plans],
            "recent_rejections": [
                {
                    "symbol": row.symbol,
                    "code": row.primary_code,
                    "count": row.count,
                    "detail": row.detail,
                    "last_as_of": row.last_as_of.isoformat(),
                    "risk_model_version": row.risk_model_version,
                }
                for row in rejections
            ],
        }
