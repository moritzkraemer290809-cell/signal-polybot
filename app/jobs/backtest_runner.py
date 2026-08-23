"""Backtest runner (phase 11).

Runs ONLY explicitly requested local backtests - there is no automatic
recurring re-run.  A run requires a complete immutable manifest, passes
data validation first, replays causally, checkpoints its progress, can be
cancelled and resumes from a checkpoint without applying an event twice.
No network access, no Telegram output, no orders.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.config import (
    BacktestSettings,
    CostSettings,
    RiskSettings,
    ShadowSimulationSettings,
    SignalLifecycleSettings,
    SimulationReportingSettings,
    StrategySettings,
)
from app.costs.models import FeeScheduleSnapshot
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.repositories.backtest_run_repository import BacktestRunRepository
from app.repositories.experiment_repository import ExperimentRepository
from app.repositories.performance_metric_repository import PerformanceMetricRepository
from app.repositories.simulated_execution_repository import SimulatedExecutionRepository
from app.repositories.simulated_position_repository import SimulatedPositionRepository
from app.repositories.simulation_event_repository import (
    SimulationEventRepository,
    SimulationRejectionRepository,
)
from app.repositories.simulation_run_repository import SimulationRunRepository
from app.simulation.analytics import segment_metrics
from app.simulation.backtest_data_validator import validate_coverage
from app.simulation.backtest_engine import BacktestCheckpoint, BacktestEngine, BacktestResult
from app.simulation.data_replay import HistoricalDataProvider, ReplayEventSource
from app.simulation.enums import (
    BacktestRunStatus,
    RunType,
    SimulationEventType,
    SimulationRejectionCode,
    SimulationSubsystemState,
)
from app.simulation.event_ordering import EventOrderingPolicy
from app.simulation.experiment_manifest import ExperimentManifest, ManifestError
from app.simulation.explainability import (
    BACKTEST_DISCLAIMER,
    SIMULATION_DISCLAIMER,
    simulation_row,
)
from app.simulation.metrics import compute_metrics
from app.simulation.replay_clock import REPLAY_CLOCK_VERSION, ReplayClock
from app.simulation.simulation_rejection import build_run_rejection
from app.simulation.version import run_dedupe_key, shadow_configuration_hash


class BacktestRunner:
    def __init__(
        self,
        settings: BacktestSettings,
        shadow_settings: ShadowSimulationSettings,
        strategy_settings: StrategySettings,
        risk_settings: RiskSettings,
        cost_settings: CostSettings,
        lifecycle_settings: SignalLifecycleSettings,
        reporting_settings: SimulationReportingSettings,
        experiment_repo: ExperimentRepository,
        run_repo: SimulationRunRepository,
        backtest_repo: BacktestRunRepository,
        position_repo: SimulatedPositionRepository,
        execution_repo: SimulatedExecutionRepository,
        event_repo: SimulationEventRepository,
        rejection_repo: SimulationRejectionRepository,
        metric_repo: PerformanceMetricRepository,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._settings = settings
        self._shadow = shadow_settings
        self._strategy = strategy_settings
        self._risk = risk_settings
        self._costs = cost_settings
        self._lifecycle = lifecycle_settings
        self._reporting = reporting_settings
        self._experiments = experiment_repo
        self._runs = run_repo
        self._backtests = backtest_repo
        self._positions = position_repo
        self._executions = execution_repo
        self._events = event_repo
        self._rejections = rejection_repo
        self._metrics_repo = metric_repo
        self._now = now_fn
        self._log = get_logger("backtest_runner")

        self._active: dict[uuid.UUID, BacktestEngine] = {}
        self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_runs))
        self.degraded = False
        self.runs_started = 0
        self.runs_completed = 0
        self.runs_rejected = 0
        self.runs_failed = 0
        self.last_run_summary: dict[str, Any] = {}
        self.last_success_at: datetime | None = None

    @property
    def state(self) -> SimulationSubsystemState:
        if not self._settings.enabled:
            return SimulationSubsystemState.DISABLED
        if self.degraded:
            return SimulationSubsystemState.DEGRADED
        return SimulationSubsystemState.HEALTHY

    @property
    def active_runs(self) -> int:
        return len(self._active)

    def cancel(self, run_id: uuid.UUID) -> bool:
        engine = self._active.get(run_id)
        if engine is None:
            return False
        engine.cancel()
        return True

    # ------------------------------------------------------------------ run

    async def run_backtest(
        self,
        *,
        manifest: ExperimentManifest,
        provider: HistoricalDataProvider,
        fee_schedule: FeeScheduleSnapshot | None,
        seed_plans: tuple[Any, ...] = (),
        run_id: uuid.UUID | None = None,
        resume_from: BacktestCheckpoint | None = None,
        created_by: str = "local-admin",
    ) -> dict[str, Any]:
        """Execute ONE explicitly requested local backtest run."""
        if not self._settings.enabled:
            return self._refused(
                "backtest subsystem disabled", SimulationRejectionCode.SIMULATION_DISABLED
            )
        try:
            manifest.require_complete()
        except ManifestError as error:
            metrics.increment("backtest_runs_rejected")
            self.runs_rejected += 1
            return self._refused(str(error), SimulationRejectionCode.MANIFEST_INVALID)

        payload = manifest.payload
        start_at = datetime.fromisoformat(str(payload["start_at"]))
        end_at = datetime.fromisoformat(str(payload["end_at"]))
        run_id = run_id or uuid.uuid4()
        dedupe = run_dedupe_key(
            experiment_id=str(manifest.experiment_id),
            run_type=RunType.BACKTEST.value,
            manifest_hash_value=manifest.content_hash,
        )

        async with self._semaphore:
            existing = await self._safe(self._runs.run_by_dedupe_key(dedupe))
            if existing is not None and resume_from is None:
                metrics.increment("backtest_runs_rejected")
                return self._refused(
                    "a run for this manifest context already exists",
                    SimulationRejectionCode.CONFIGURATION_INVALID,
                )
            await self._safe(self._experiments.add_manifest(manifest))
            created = await self._safe(
                self._runs.create_run(
                    run_id=run_id,
                    experiment_id=manifest.experiment_id,
                    manifest_id=manifest.manifest_id,
                    run_type=RunType.BACKTEST,
                    status=BacktestRunStatus.VALIDATING.value,
                    dedupe_key=dedupe,
                    start_at=start_at,
                    end_at=end_at,
                    created_by=created_by,
                )
            )
            if created is None:
                self.degraded = True
            await self._safe(
                self._backtests.create(
                    run_id=run_id,
                    status=BacktestRunStatus.VALIDATING.value,
                    ordering_version=self._settings.replay_ordering_version,
                    clock_version=REPLAY_CLOCK_VERSION,
                )
            )
            self.runs_started += 1
            metrics.increment("backtest_runs_started")

            # ---- data validation BEFORE any replay ------------------------
            events = list(provider.events(start_at, end_at))
            coverage = validate_coverage(events, self._settings, start_at=start_at, end_at=end_at)
            await self._safe(self._backtests.record_coverage(run_id, coverage))
            if not coverage.complete and not coverage.excluded_intervals:
                self.runs_rejected += 1
                metrics.increment("backtest_runs_rejected")
                metrics.increment("backtest_data_gap_intervals", len(coverage.gaps))
                await self._safe(
                    self._runs.update_progress(
                        run_id,
                        status=BacktestRunStatus.REJECTED.value,
                        detail=coverage.detail,
                        completed=True,
                    )
                )
                await self._record_rejection(
                    run_id,
                    SimulationRejectionCode.BACKTEST_DATA_INCOMPLETE
                    if coverage.missing_channels
                    else SimulationRejectionCode.DATA_GAP,
                    coverage.detail,
                )
                return {
                    "run_id": str(run_id),
                    "status": BacktestRunStatus.REJECTED.value,
                    "detail": coverage.detail,
                    "disclaimers": [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER],
                }
            if coverage.excluded_intervals:
                metrics.increment("backtest_data_gap_intervals", len(coverage.excluded_intervals))

            # ---- causal replay --------------------------------------------
            clock = ReplayClock(start_at, end_at)
            source = ReplayEventSource(
                provider,
                clock,
                EventOrderingPolicy(version=self._settings.replay_ordering_version),
                max_events=self._settings.max_events_per_run,
            )
            engine = BacktestEngine(
                backtest_settings=self._settings,
                strategy_settings=self._strategy,
                risk_settings=self._risk,
                cost_settings=self._costs,
                lifecycle_settings=self._lifecycle,
                shadow_settings=self._shadow,
                fee_schedule=fee_schedule,
                simulation_config_hash=shadow_configuration_hash(self._shadow),
                excluded_intervals=coverage.excluded_intervals,
                seed_plans=tuple(seed_plans),
                seed_base=self._settings.bootstrap_seed,
                checkpoint_interval=self._settings.checkpoint_interval_events,
            )
            self._active[run_id] = engine
            await self._safe(
                self._runs.update_progress(run_id, status=BacktestRunStatus.RUNNING.value)
            )

            checkpoints: list[BacktestCheckpoint] = []

            def on_checkpoint(checkpoint: BacktestCheckpoint) -> None:
                checkpoints.append(checkpoint)

            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        engine.run,
                        source,
                        clock,
                        run_id=run_id,
                        resume_from=resume_from,
                        on_checkpoint=on_checkpoint,
                    ),
                    timeout=self._settings.run_timeout_seconds,
                )
            except TimeoutError:
                self.runs_failed += 1
                metrics.increment("backtest_runs_failed")
                await self._safe(
                    self._runs.update_progress(
                        run_id,
                        status=BacktestRunStatus.FAILED.value,
                        detail="run timeout reached",
                        completed=True,
                    )
                )
                return {
                    "run_id": str(run_id),
                    "status": BacktestRunStatus.FAILED.value,
                    "detail": "run timeout reached",
                    "disclaimers": [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER],
                }
            except Exception as exc:
                self.degraded = True
                self.runs_failed += 1
                metrics.increment("backtest_runs_failed")
                self._log.error("backtest_run_failed", error=type(exc).__name__)
                await self._safe(
                    self._runs.update_progress(
                        run_id,
                        status=BacktestRunStatus.FAILED.value,
                        detail=f"run failed: {type(exc).__name__}",
                        completed=True,
                    )
                )
                return {
                    "run_id": str(run_id),
                    "status": BacktestRunStatus.FAILED.value,
                    "detail": f"run failed: {type(exc).__name__}",
                    "disclaimers": [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER],
                }
            finally:
                self._active.pop(run_id, None)

            metrics.increment("backtest_events_replayed", result.events_replayed)
            summary = await self._persist_result(run_id, result, checkpoints)
            return summary

    # -------------------------------------------------------- persistence

    async def _persist_result(
        self,
        run_id: uuid.UUID,
        result: BacktestResult,
        checkpoints: list[BacktestCheckpoint],
    ) -> dict[str, Any]:
        completed = 0
        for item in result.simulations:
            stored = await self._safe(
                self._positions.add_position(
                    item, disclaimer_version=self._reporting.disclaimer_version
                )
            )
            if not stored:
                metrics.increment("simulation_dedupe_suppressed")
                continue
            await self._safe(
                self._executions.add_executions(
                    run_id=run_id,
                    simulated_position_id=item.simulation_position_id,
                    executions=(item.entry, item.partial_reduction, item.exit_execution),
                )
            )
            await self._safe(
                self._events.add_event(
                    run_id=run_id,
                    event_type=SimulationEventType.SIMULATION_COMPLETED
                    if item.completed
                    else SimulationEventType.SIMULATION_INCOMPLETE,
                    as_of=item.event_reference_at,
                    simulated_position_id=item.simulation_position_id,
                    detail=simulation_row(item),
                )
            )
            if item.completed:
                completed += 1

        for rejection in result.rejections:
            await self._safe(
                self._rejections.record_rejection(rejection, self._shadow.simulation_dedupe_seconds)
            )
            if rejection.primary_code is SimulationRejectionCode.LOOKAHEAD_GUARD_TRIGGERED:
                metrics.increment("backtest_lookahead_guard_rejections")

        overall = compute_metrics(
            list(result.simulations),
            rejected=len(result.rejections),
            min_complete_simulations=self._settings.min_complete_simulations,
            virtual_account_pusd=self._settings.virtual_account_pusd,
            metrics_version=self._reporting.metrics_version,
            disclaimer_version=self._reporting.disclaimer_version,
            bootstrap_resamples=(
                self._settings.bootstrap_resamples if self._settings.enable_bootstrap else 0
            ),
            bootstrap_seed=self._settings.bootstrap_seed,
        )
        segments = segment_metrics(
            list(result.simulations),
            min_complete_simulations=self._settings.min_complete_simulations,
            virtual_account_pusd=self._settings.virtual_account_pusd,
            metrics_version=self._reporting.metrics_version,
            disclaimer_version=self._reporting.disclaimer_version,
        )
        if overall.sample_status == "INSUFFICIENT_SAMPLE":
            metrics.increment("performance_metrics_insufficient_sample")
        await self._safe(self._metrics_repo.add_metric_set(run_id, overall))
        await self._safe(self._metrics_repo.add_metric_sets(run_id, segments))

        checkpoint = checkpoints[-1] if checkpoints else result.checkpoint
        await self._safe(
            self._backtests.save_checkpoint(
                run_id,
                checkpoint=checkpoint.as_dict(),
                events_replayed=result.events_replayed,
                last_event_at=checkpoint.last_event_at,
                status=result.status.value,
            )
        )
        await self._safe(
            self._runs.update_progress(
                run_id,
                status=result.status.value,
                events_replayed=result.events_replayed,
                checkpoint=checkpoint.as_dict(),
                counts={
                    "total": len(result.simulations) + len(result.rejections),
                    "completed": completed,
                    "incomplete": len(result.simulations) - completed,
                    "rejected": len(result.rejections),
                },
                warnings=result.warnings,
                excluded_intervals=[
                    {
                        "symbol": interval.symbol,
                        "channel": interval.channel,
                        "start_at": interval.start_at.isoformat(),
                        "end_at": interval.end_at.isoformat(),
                        "detail": interval.detail,
                    }
                    for interval in result.excluded_intervals
                ],
                detail=result.detail,
                completed=True,
            )
        )
        if result.status in (
            BacktestRunStatus.COMPLETED,
            BacktestRunStatus.COMPLETED_WITH_GAPS,
        ):
            self.runs_completed += 1
            metrics.increment("backtest_runs_completed")
            self.last_success_at = self._now()
        self.last_run_summary = {
            "run_id": str(run_id),
            "status": result.status.value,
            "events_replayed": result.events_replayed,
            "simulations": len(result.simulations),
            "simulations_completed": completed,
            "rejections": len(result.rejections),
            "excluded_intervals": len(result.excluded_intervals),
            "sample_status": overall.sample_status,
            "warnings": list(result.warnings),
            "detail": result.detail,
            "disclaimers": [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER],
        }
        return self.last_run_summary

    async def _record_rejection(
        self, run_id: uuid.UUID, code: SimulationRejectionCode, detail: str
    ) -> None:
        await self._safe(
            self._rejections.record_rejection(
                build_run_rejection(
                    run_id=run_id,
                    symbol="-",
                    code=code,
                    detail=detail,
                    simulation_model_version=self._shadow.simulation_model_version,
                    simulation_config_hash=shadow_configuration_hash(self._shadow),
                    as_of=self._now(),
                ),
                self._shadow.simulation_dedupe_seconds,
            )
        )

    async def _safe(self, awaitable: Any) -> Any:
        """Await a persistence call; a DB outage degrades, never crashes."""
        try:
            return await awaitable
        except Exception as exc:
            self.degraded = True
            self._log.error("backtest_persistence_failed", error=type(exc).__name__)
            return None

    def _refused(self, detail: str, code: SimulationRejectionCode) -> dict[str, Any]:
        return {
            "run_id": None,
            "status": BacktestRunStatus.REJECTED.value,
            "code": code.value,
            "detail": detail,
            "disclaimers": [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER],
        }

    # -------------------------------------------------------------- reports

    async def health_stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        with contextlib.suppress(Exception):
            counts = await self._runs.counts_by_status(RunType.BACKTEST)
        return {
            "state": self.state.value,
            "active_runs": self.active_runs,
            "run_counts_by_status": counts,
            "runs_started": self.runs_started,
            "runs_completed": self.runs_completed,
            "runs_rejected": self.runs_rejected,
            "runs_failed": self.runs_failed,
            "last_success_at": (self.last_success_at.isoformat() if self.last_success_at else None),
            "note": SIMULATION_DISCLAIMER,
        }

    async def status_stats(self) -> dict[str, Any]:
        stats = await self.health_stats()
        recent: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            rows = await self._runs.recent(limit=10)
            recent = [
                {
                    "run": str(row.id)[:8],
                    "run_type": row.run_type,
                    "status": row.run_status,
                    "window": [row.start_at.isoformat(), row.end_at.isoformat()],
                    "events_replayed": row.events_replayed,
                    "simulations_completed": row.simulations_completed,
                    "excluded_intervals": len((row.excluded_intervals or {}).get("intervals", [])),
                }
                for row in rows
            ]
        stats.update(
            {
                "backtest_model": f"{self._settings.model_name}@{self._settings.model_version}",
                "replay_ordering_version": self._settings.replay_ordering_version,
                "replay_clock_version": REPLAY_CLOCK_VERSION,
                "min_complete_simulations": self._settings.min_complete_simulations,
                "recent_runs": recent,
                "last_run": self.last_run_summary,
                "disclaimers": [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER],
            }
        )
        return stats

    async def dashboard_details(self) -> dict[str, Any] | None:
        try:
            runs = await self._runs.recent(limit=10)
            experiments = await self._experiments.recent(limit=10)
        except Exception:
            return None
        details: list[dict[str, Any]] = []
        for row in runs:
            metric_sets = await self._safe(self._metrics_repo.sets_for_run(row.id)) or []
            details.append(
                {
                    "run": str(row.id),
                    "run_type": row.run_type,
                    "status": row.run_status,
                    "window": [row.start_at.isoformat(), row.end_at.isoformat()],
                    "events_replayed": row.events_replayed,
                    "simulations_total": row.simulations_total,
                    "simulations_completed": row.simulations_completed,
                    "simulations_rejected": row.simulations_rejected,
                    "excluded_intervals": (row.excluded_intervals or {}).get("intervals", []),
                    "warnings": (row.warnings or {}).get("warnings", []),
                    "metric_sets": [
                        {
                            "segment": f"{item.segment_kind}:{item.segment_key}",
                            "sample_status": item.sample_status,
                            "complete_simulations": item.complete_simulations,
                            "metrics_version": item.metrics_version,
                        }
                        for item in metric_sets
                    ],
                }
            )
        return {
            "disclaimers": [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER],
            "experiments": [
                {
                    "experiment": str(row.id)[:8],
                    "name": row.name,
                    "status": row.status,
                    "created_at": row.created_at.isoformat(),
                }
                for row in experiments
            ],
            "runs": details,
        }
