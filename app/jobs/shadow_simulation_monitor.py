"""Shadow simulation monitor job (phase 11).

Consumes ONLY internal, persisted phase-10 lifecycles and models a
delayed hypothetical follower over public market data.  No orders, no
wallet, no account data, no Telegram output.  Global PAUSED mode admits
no new simulation; a DB outage degrades the subsystem and never reports
an unpersisted simulation as completed.
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
from app.config import CostSettings, ShadowSimulationSettings, SimulationReportingSettings
from app.costs.fee_schedule import snapshot_from_schedule
from app.costs.models import CostModelError, FeeScheduleSnapshot
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.repositories.fee_schedule_repository import FeeScheduleRepository
from app.repositories.orm import SignalLifecycleRecord
from app.repositories.performance_metric_repository import PerformanceMetricRepository
from app.repositories.signal_lifecycle_repository import SignalLifecycleRepository
from app.repositories.simulated_execution_repository import SimulatedExecutionRepository
from app.repositories.simulated_position_repository import SimulatedPositionRepository
from app.repositories.simulation_event_repository import (
    SimulationEventRepository,
    SimulationRejectionRepository,
)
from app.repositories.simulation_run_repository import SimulationRunRepository
from app.simulation.enums import (
    SimulatedPositionState,
    SimulationSubsystemState,
)
from app.simulation.explainability import (
    SIMULATION_DISCLAIMER,
    metric_set_view,
    simulation_row,
)
from app.simulation.metrics import compute_metrics
from app.simulation.models import MetricSet
from app.simulation.shadow_event_consumer import ShadowEventQueue, ShadowWorkItem
from app.simulation.shadow_mode import event_for_result, is_simulatable, run_counts
from app.simulation.simulation_context import ShadowSimulationContextBuilder
from app.simulation.simulation_engine import simulate
from app.simulation.version import shadow_configuration_hash


class ShadowSimulationMonitorJob:
    def __init__(
        self,
        settings: ShadowSimulationSettings,
        cost_settings: CostSettings,
        reporting_settings: SimulationReportingSettings,
        context_builder: ShadowSimulationContextBuilder,
        lifecycle_repo: SignalLifecycleRepository,
        run_repo: SimulationRunRepository,
        position_repo: SimulatedPositionRepository,
        execution_repo: SimulatedExecutionRepository,
        event_repo: SimulationEventRepository,
        rejection_repo: SimulationRejectionRepository,
        metric_repo: PerformanceMetricRepository,
        fee_repo: FeeScheduleRepository,
        bot_state: BotStateService | None,
        *,
        run_id: uuid.UUID | None = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._settings = settings
        self._costs = cost_settings
        self._reporting = reporting_settings
        self._builder = context_builder
        self._lifecycles = lifecycle_repo
        self._runs = run_repo
        self._positions = position_repo
        self._executions = execution_repo
        self._events = event_repo
        self._rejections = rejection_repo
        self._metrics_repo = metric_repo
        self._fees = fee_repo
        self._bot_state = bot_state
        self._now = now_fn
        self._log = get_logger("shadow_simulation")

        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._queue = ShadowEventQueue(settings.simulation_queue_size)
        self._fee_snapshot: FeeScheduleSnapshot | None = None
        self._seen: dict[uuid.UUID, datetime] = {}
        self.run_id = run_id or uuid.uuid4()
        self.config_hash = shadow_configuration_hash(settings)
        self.lease_owner = f"shadow-{uuid.uuid4().hex[:12]}"

        self.degraded = False
        self.last_run_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_run_summary: dict[str, Any] = {}
        self.simulations_created = 0
        self.simulations_completed = 0
        self.simulations_incomplete = 0
        self.simulations_rejected = 0
        self.lease_recoveries = 0

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="shadow_simulation_monitor")

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
        self._wakeup.set()

    @property
    def state(self) -> SimulationSubsystemState:
        if not self._settings.mode_enabled:
            return SimulationSubsystemState.DISABLED
        if self._task is None or self._task.done():
            return SimulationSubsystemState.UNAVAILABLE
        if self.degraded:
            return SimulationSubsystemState.DEGRADED
        return SimulationSubsystemState.HEALTHY

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.degraded = True
                metrics.increment("shadow_simulation_cycle_failures")
                self._log.error("shadow_cycle_failed", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wakeup.wait(),
                    timeout=self._settings.simulation_monitor_refresh_seconds,
                )
            self._wakeup.clear()

    # ---------------------------------------------------------------- cycle

    async def run_once(self) -> dict[str, Any]:
        if self._lock.locked():
            metrics.increment("shadow_simulation_overlap_skipped")
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

        if not self._settings.mode_enabled:
            self.last_run_summary = {
                "run_at": now.isoformat(),
                "enabled": False,
                "note": "shadow simulation disabled",
            }
            return self.last_run_summary

        fee_snapshot = await self._ensure_fee_schedule()
        await self._enqueue_lifecycles(now)

        created = completed = incomplete = rejected = 0
        if paused:
            metrics.increment("shadow_simulation_paused_suppressed")
        else:
            batch = self._queue.drain(self._settings.simulation_max_concurrent)
            semaphore = asyncio.Semaphore(max(1, self._settings.simulation_max_concurrent))

            async def process(item: ShadowWorkItem) -> None:
                nonlocal created, completed, incomplete, rejected
                async with semaphore:
                    outcome = await self._simulate_lifecycle(item, fee_snapshot, now)
                    if outcome == "created":
                        created += 1
                    elif outcome == "completed":
                        created += 1
                        completed += 1
                    elif outcome == "incomplete":
                        created += 1
                        incomplete += 1
                    elif outcome == "rejected":
                        rejected += 1

            await asyncio.gather(*(process(item) for item in batch))

        self.last_run_at = now
        duration = time.monotonic() - started
        metrics.increment("shadow_simulation_duration_seconds", duration)
        self.last_run_summary = {
            "run_at": now.isoformat(),
            "enabled": True,
            "paused": paused,
            "queued": len(self._queue),
            "queue_dropped": self._queue.dropped,
            "simulations_created": created,
            "simulations_completed": completed,
            "simulations_incomplete": incomplete,
            "simulations_rejected": rejected,
            "duration_seconds": round(duration, 3),
            "note": SIMULATION_DISCLAIMER,
        }
        if not self.degraded:
            self.last_success_at = now
        return self.last_run_summary

    # -------------------------------------------------------------- helpers

    async def _ensure_fee_schedule(self) -> FeeScheduleSnapshot | None:
        try:
            active = await self._fees.active_schedule()
            if active is None:
                metrics.increment("shadow_simulation_fee_schedule_unavailable")
                return self._fee_snapshot
            snapshot = snapshot_from_schedule(
                active.schedule, self._costs, active=True, effective_at=active.effective_at
            )
            self._fee_snapshot = snapshot
            return snapshot
        except CostModelError:
            metrics.increment("shadow_simulation_fee_schedule_unavailable")
            return self._fee_snapshot
        except Exception as exc:
            self.degraded = True
            self._log.error("shadow_fee_schedule_failed", error=type(exc).__name__)
            return self._fee_snapshot

    async def _enqueue_lifecycles(self, now: datetime) -> None:
        """Queue persisted lifecycles whose entry is already confirmed."""
        try:
            rows = await self._lifecycles.recent(limit=self._settings.simulation_queue_size)
        except Exception as exc:
            self.degraded = True
            self._log.error("shadow_lifecycle_load_failed", error=type(exc).__name__)
            return
        dedupe_window = timedelta(seconds=self._settings.simulation_dedupe_seconds)
        for row in rows:
            if not is_simulatable(row.state, row.entry_confirmed_at):
                continue
            seen_at = self._seen.get(row.id)
            if seen_at is not None and now - seen_at < dedupe_window:
                metrics.increment("simulation_dedupe_suppressed")
                continue
            self._queue.offer(
                ShadowWorkItem(
                    signal_id=row.id,
                    state=row.state,
                    state_version=int(row.state_version),
                    observed_at=now,
                )
            )

    async def _lifecycle_row(self, signal_id: uuid.UUID) -> SignalLifecycleRecord | None:
        try:
            return await self._lifecycles.get(signal_id)
        except Exception as exc:
            self.degraded = True
            self._log.error("shadow_lifecycle_read_failed", error=type(exc).__name__)
            return None

    async def _simulate_lifecycle(
        self, item: ShadowWorkItem, fee_snapshot: FeeScheduleSnapshot | None, now: datetime
    ) -> str:
        """ "completed" | "incomplete" | "created" | "rejected" | "skipped"."""
        row = await self._lifecycle_row(item.signal_id)
        if row is None:
            return "skipped"
        try:
            duplicate = await self._positions.exists_for_lifecycle(self.run_id, row.id)
        except Exception as exc:
            self.degraded = True
            self._log.error("shadow_duplicate_check_failed", error=type(exc).__name__)
            return "skipped"
        inputs = await self._builder.build_inputs(
            row,
            run_id=self.run_id,
            fee_schedule=fee_snapshot,
            bot_paused=False,
            duplicate_exists=duplicate,
            session_allowed=str(row.last_session_state or "UNKNOWN") != "BLOCKED",
            session_state=str(row.last_session_state or "UNKNOWN"),
            as_of=now,
        )
        if inputs is None:
            return "skipped"

        outcome = simulate(inputs, self._settings, self._costs, config_hash=self.config_hash)
        if outcome.rejection is not None:
            self.simulations_rejected += 1
            metrics.increment(
                f"shadow_simulations_rejected.{outcome.rejection.primary_code.value.lower()}"
            )
            metrics.increment("shadow_simulations_rejected")
            try:
                await self._rejections.record_rejection(
                    outcome.rejection, self._settings.simulation_dedupe_seconds
                )
            except Exception as exc:
                self.degraded = True
                self._log.error("shadow_rejection_persist_failed", error=type(exc).__name__)
            self._seen[row.id] = now
            return "rejected"

        result = outcome.result
        assert result is not None
        if result.state is SimulatedPositionState.OPEN_SIMULATION:
            # the lifecycle is still running - re-evaluate on a later cycle
            return "skipped"

        try:
            stored = await self._positions.add_position(
                result, disclaimer_version=self._reporting.disclaimer_version
            )
            if not stored:
                metrics.increment("simulation_dedupe_suppressed")
                self._seen[row.id] = now
                return "skipped"
            await self._executions.add_executions(
                run_id=self.run_id,
                simulated_position_id=result.simulation_position_id,
                executions=(result.entry, result.partial_reduction, result.exit_execution),
            )
            await self._events.add_event(
                run_id=self.run_id,
                event_type=event_for_result(result),
                as_of=now,
                simulated_position_id=result.simulation_position_id,
                detail=simulation_row(result),
            )
        except Exception as exc:
            # never report an unpersisted simulation as completed
            self.degraded = True
            metrics.increment("shadow_simulation_persist_failures")
            self._log.error("shadow_simulation_persist_failed", error=type(exc).__name__)
            return "skipped"

        self._seen[row.id] = now
        self.simulations_created += 1
        metrics.increment("shadow_simulations_created")
        if result.delay is not None:
            metrics.increment("shadow_simulation_entry_delay_seconds", result.delay.delay_seconds)
        if result.duration_seconds is not None:
            metrics.increment("shadow_simulation_duration_seconds", result.duration_seconds)
        if result.costs is not None:
            metrics.increment("simulation_costs_total_modeled", result.costs.total)
        if result.funding is not None and not result.funding.data_available:
            metrics.increment("simulation_funding_unavailable")
        if result.state is SimulatedPositionState.CLOSED_SIMULATION:
            self.simulations_completed += 1
            metrics.increment("shadow_simulations_completed")
            self._log.info(
                "shadow_simulation_modelled",
                symbol=result.symbol,
                exit_reason=result.exit_reason.value if result.exit_reason else None,
                note="hypothetical simulation - no execution and no real position",
            )
            return "completed"
        self.simulations_incomplete += 1
        metrics.increment("shadow_simulations_incomplete")
        return "incomplete"

    # -------------------------------------------------------------- reports

    async def health_stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        rejections_last_hour = 0
        with contextlib.suppress(Exception):
            counts = await self._positions.counts_by_state(self.run_id)
        with contextlib.suppress(Exception):
            rejections_last_hour = await self._rejections.count_since(
                self._now() - timedelta(hours=1)
            )
        return {
            "state": self.state.value,
            "job_alive": self.alive,
            "queued": len(self._queue),
            "queue_capacity": self._queue.capacity,
            "simulation_counts_by_state": counts,
            "simulations_created": self.simulations_created,
            "simulations_completed": self.simulations_completed,
            "simulations_incomplete": self.simulations_incomplete,
            "simulations_rejected": self.simulations_rejected,
            "rejections_last_hour": rejections_last_hour,
            "last_success_at": (self.last_success_at.isoformat() if self.last_success_at else None),
            "note": SIMULATION_DISCLAIMER,
        }

    async def status_stats(self) -> dict[str, Any]:
        stats = await self.health_stats()
        stats.update(
            {
                "simulation_model": (
                    f"{self._settings.simulation_model_name}"
                    f"@{self._settings.simulation_model_version}"
                ),
                "simulation_config_hash": self.config_hash,
                "delay_model": self._settings.delay_model,
                "run_id": str(self.run_id)[:8],
                "last_run": self.last_run_summary,
                "disclaimer": SIMULATION_DISCLAIMER,
            }
        )
        return stats

    async def dashboard_details(self) -> dict[str, Any] | None:
        try:
            positions = await self._positions.positions_for_run(self.run_id)
            rejections = await self._rejections.recent(limit=20)
            counts = await self._positions.counts_by_state(self.run_id)
        except Exception:
            return None
        payloads = [row.payload or {} for row in positions]
        return {
            "disclaimer": SIMULATION_DISCLAIMER,
            "run_id": str(self.run_id),
            "simulation_counts_by_state": counts,
            "simulations": payloads,
            "recent_rejections": [
                {
                    "symbol": row.symbol,
                    "code": row.primary_code,
                    "count": row.count,
                    "detail": row.detail,
                    "last_as_of": row.last_as_of.isoformat(),
                }
                for row in rejections
            ],
        }

    def metrics_for(
        self, results: list[Any], *, virtual_account_pusd: float | None = None
    ) -> MetricSet:
        """Hypothetical metric set over in-memory results (local reports)."""
        return compute_metrics(
            results,
            rejected=self.simulations_rejected,
            min_complete_simulations=1,
            virtual_account_pusd=virtual_account_pusd,
            metrics_version=self._reporting.metrics_version,
            disclaimer_version=self._reporting.disclaimer_version,
        )

    @staticmethod
    def metric_view(metric_set: Any) -> dict[str, Any]:
        return metric_set_view(metric_set)

    @staticmethod
    def counts(results: list[Any], rejected: int) -> dict[str, int]:
        return run_counts(results, rejected)
