"""Causal backtest driver over replayed public data.

The engine owns no market rules of its own: it advances the replay clock,
folds input events into the observable state and then invokes the REAL
phase-8/9/10 cores plus the phase-11 simulation engine in the versioned
stage order.  Nothing is read ahead of the clock, and a configuration
change produces a new run rather than a rewritten result.

Results are hypothetical model output over historical data - never real
performance and never a forecast.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from app.config import (
    BacktestSettings,
    CostSettings,
    RiskSettings,
    ShadowSimulationSettings,
    SignalLifecycleSettings,
    StrategySettings,
)
from app.costs.models import CostModelError, FeeScheduleSnapshot
from app.risk.enums import PlanStatus
from app.risk.evaluation_context import candidate_snapshot_from_record  # noqa: F401 (parity)
from app.risk.models import CandidateSnapshot, SignalEligibilityPlan, TechnicalLevel
from app.risk.risk_engine import evaluate_candidate
from app.signals.enums import SignalState
from app.simulation.backtest_context import ReplayState
from app.simulation.backtest_data_validator import is_excluded
from app.simulation.data_replay import ReplayEventSource
from app.simulation.enums import (
    BacktestRunStatus,
    DataCompleteness,
    ReplayEventCategory,
    SimulationRejectionCode,
)
from app.simulation.follower_delay import realize_delay
from app.simulation.models import (
    DataGapInterval,
    LifecycleObservation,
    MarketReferenceSnapshot,
    ReplayEvent,
    SimulatedPositionResult,
    SimulationInputs,
    SimulationPlanReference,
    SimulationRejection,
)
from app.simulation.replay_clock import LookaheadError, ReplayClock
from app.simulation.simulated_lifecycle import exit_reason_for_event, exit_reason_for_state
from app.simulation.simulation_engine import simulate
from app.simulation.simulation_rejection import build_run_rejection
from app.strategy.enums import CandidateState
from app.strategy.models import SetupCandidate
from app.strategy.strategy_engine import evaluate_context


class BacktestCancelled(Exception):
    """Cooperative cancellation of a running backtest."""


@dataclass
class BacktestCheckpoint:
    """Resumable progress marker of one run (no event applied twice)."""

    run_id: uuid.UUID
    events_replayed: int
    last_event_at: datetime | None
    last_event_sequence: int
    simulations_completed: int
    rejections: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "events_replayed": self.events_replayed,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "last_event_sequence": self.last_event_sequence,
            "simulations_completed": self.simulations_completed,
            "rejections": self.rejections,
        }


@dataclass
class BacktestResult:
    """Outcome of one causal replay run - hypothetical model output."""

    run_id: uuid.UUID
    status: BacktestRunStatus
    events_replayed: int
    candidates_detected: int
    plans_eligible: int
    lifecycles_started: int
    simulations: tuple[SimulatedPositionResult, ...]
    rejections: tuple[SimulationRejection, ...]
    excluded_intervals: tuple[DataGapInterval, ...]
    warnings: tuple[str, ...]
    checkpoint: BacktestCheckpoint
    started_at: datetime
    finished_at: datetime
    detail: str = ""

    @property
    def completed_simulations(self) -> tuple[SimulatedPositionResult, ...]:
        return tuple(item for item in self.simulations if item.completed)


@dataclass
class _Lifecycle:
    """In-memory phase-10 lifecycle instance during a replay."""

    signal_id: uuid.UUID
    plan: SignalEligibilityPlan
    state: SignalState
    entry_confirmed_at: datetime | None = None
    entry_observation: LifecycleObservation | None = None
    terminal_at: datetime | None = None
    simulated: bool = False
    #: modelled follower delay: when the hypothetical entry becomes due
    entry_due_at: datetime | None = None
    #: public market reference captured AT the delayed entry instant
    entry_market: MarketReferenceSnapshot | None = None
    entry_capture_at: datetime | None = None


@dataclass
class BacktestEngine:
    """Deterministic causal replay of the phase 7-11 pipeline."""

    backtest_settings: BacktestSettings
    strategy_settings: StrategySettings
    risk_settings: RiskSettings
    cost_settings: CostSettings
    lifecycle_settings: SignalLifecycleSettings
    shadow_settings: ShadowSimulationSettings
    fee_schedule: FeeScheduleSnapshot | None
    simulation_config_hash: str
    excluded_intervals: tuple[DataGapInterval, ...] = ()
    #: ELIGIBLE phase-9 plans replayed as lifecycle inputs.  Used when the
    #: historical window carries plans (or persisted plan history) rather
    #: than enough candle history to re-derive them; the strategy/risk
    #: stages still run and contribute any plans the data supports.
    seed_plans: tuple[SignalEligibilityPlan, ...] = ()
    seed_base: int = 0
    checkpoint_interval: int = 500
    _cancelled: bool = field(default=False, init=False)
    _levels_by_symbol: dict[str, tuple[TechnicalLevel, ...]] = field(
        default_factory=dict, init=False
    )
    _clock_now: datetime = field(default=datetime.min.replace(tzinfo=UTC), init=False)

    def cancel(self) -> None:
        """Request cooperative cancellation (checked between events)."""
        self._cancelled = True

    # ----------------------------------------------------------------- replay

    def run(
        self,
        source: ReplayEventSource,
        clock: ReplayClock,
        *,
        run_id: uuid.UUID,
        resume_from: BacktestCheckpoint | None = None,
        on_checkpoint: Any | None = None,
    ) -> BacktestResult:
        started_at = clock.start_at
        state = ReplayState(clock)
        candidates: dict[str, SetupCandidate] = {}
        plans: list[SignalEligibilityPlan] = list(self.seed_plans)
        lifecycles: dict[uuid.UUID, _Lifecycle] = {}
        simulations: list[SimulatedPositionResult] = []
        rejections: list[SimulationRejection] = []
        warnings: list[str] = []
        checkpoint = resume_from or BacktestCheckpoint(
            run_id=run_id,
            events_replayed=0,
            last_event_at=None,
            last_event_sequence=-1,
            simulations_completed=0,
            rejections=0,
        )
        resume_marker = (
            (checkpoint.last_event_at, checkpoint.last_event_sequence)
            if resume_from is not None and checkpoint.last_event_at is not None
            else None
        )
        status = BacktestRunStatus.RUNNING
        detail = ""

        try:
            for event in source:
                if self._cancelled:
                    raise BacktestCancelled("cancelled by operator")
                if resume_marker is not None and (event.as_of, event.sequence) <= resume_marker:
                    continue  # already applied before the checkpoint
                if is_excluded(event.as_of, self.excluded_intervals, event.symbol):
                    continue  # inside an explicitly excluded data gap
                self._apply_event(event, state)
                checkpoint = BacktestCheckpoint(
                    run_id=run_id,
                    events_replayed=checkpoint.events_replayed + 1,
                    last_event_at=event.as_of,
                    last_event_sequence=event.sequence,
                    simulations_completed=len(simulations),
                    rejections=len(rejections),
                )
                if event.category == ReplayEventCategory.CLOSED_CANDLE.value:
                    self._run_pipeline(
                        symbol=event.symbol,
                        state=state,
                        clock=clock,
                        candidates=candidates,
                        plans=plans,
                        lifecycles=lifecycles,
                        simulations=simulations,
                        rejections=rejections,
                        warnings=warnings,
                        run_id=run_id,
                    )
                if (
                    on_checkpoint is not None
                    and checkpoint.events_replayed % max(1, self.checkpoint_interval) == 0
                ):
                    on_checkpoint(checkpoint)
            if source.truncated:
                warnings.append("MAX_EVENTS_PER_RUN_REACHED")
        except BacktestCancelled as error:
            status = BacktestRunStatus.CANCELLED
            detail = str(error)
        except LookaheadError as error:
            status = BacktestRunStatus.REJECTED
            detail = error.detail
            rejections.append(
                build_run_rejection(
                    run_id=run_id,
                    symbol="-",
                    code=SimulationRejectionCode.LOOKAHEAD_GUARD_TRIGGERED,
                    detail=error.detail,
                    simulation_model_version=self.shadow_settings.simulation_model_version,
                    simulation_config_hash=self.simulation_config_hash,
                    as_of=clock.now(),
                )
            )
        else:
            status = (
                BacktestRunStatus.COMPLETED_WITH_GAPS
                if self.excluded_intervals
                else BacktestRunStatus.COMPLETED
            )
            detail = (
                "replay finished over the remaining segments - not a complete backtest"
                if self.excluded_intervals
                else "replay finished over the full requested window"
            )

        return BacktestResult(
            run_id=run_id,
            status=status,
            events_replayed=checkpoint.events_replayed,
            candidates_detected=len(candidates),
            plans_eligible=len(plans),
            lifecycles_started=len(lifecycles),
            simulations=tuple(simulations),
            rejections=tuple(rejections),
            excluded_intervals=self.excluded_intervals,
            warnings=tuple(dict.fromkeys(warnings)),
            checkpoint=checkpoint,
            started_at=started_at,
            finished_at=clock.now(),
            detail=detail,
        )

    # ------------------------------------------------------------ pipeline

    def _apply_event(self, event: ReplayEvent, state: ReplayState) -> None:
        state.apply(event.category, event.symbol, event.as_of, event.payload)

    def _run_pipeline(
        self,
        *,
        symbol: str,
        state: ReplayState,
        clock: ReplayClock,
        candidates: dict[str, SetupCandidate],
        plans: list[SignalEligibilityPlan],
        lifecycles: dict[uuid.UUID, _Lifecycle],
        simulations: list[SimulatedPositionResult],
        rejections: list[SimulationRejection],
        warnings: list[str],
        run_id: uuid.UUID,
    ) -> None:
        """Stages 6-9 of the ordering policy, in order, at one instant."""
        self._clock_now = clock.now()
        self._stage_strategy(symbol, state, candidates)
        self._stage_risk(symbol, state, candidates, plans, warnings)
        self._stage_lifecycle(symbol, state, clock, plans, lifecycles)
        self._stage_entry_capture(symbol, state, clock, lifecycles)
        self._stage_simulation(
            symbol=symbol,
            state=state,
            clock=clock,
            lifecycles=lifecycles,
            simulations=simulations,
            rejections=rejections,
            run_id=run_id,
        )

    def _stage_strategy(
        self, symbol: str, state: ReplayState, candidates: dict[str, SetupCandidate]
    ) -> None:
        context = state.strategy_context(symbol, self.strategy_settings)
        if context is None:
            return
        outcome = evaluate_context(context, self.strategy_settings)
        # the same structural swings the live feature store would have persisted
        self._levels_by_symbol[symbol] = self._levels_from_structure(outcome)
        for candidate in outcome.candidates:
            existing = candidates.get(candidate.dedupe_key)
            if existing is None:
                candidates[candidate.dedupe_key] = candidate
            elif (
                existing.state is CandidateState.DETECTED
                and candidate.state is CandidateState.CONFIRMED
            ):
                # same promotion rule as the live phase-8 job - no new logic
                candidates[candidate.dedupe_key] = candidate

    def _stage_risk(
        self,
        symbol: str,
        state: ReplayState,
        candidates: dict[str, SetupCandidate],
        plans: list[SignalEligibilityPlan],
        warnings: list[str],
    ) -> None:
        known_plan_candidates = {plan.candidate_id for plan in plans}
        now = state.state_for(symbol)  # touch state so the symbol exists
        del now
        for key, candidate in list(candidates.items()):
            if candidate.symbol != symbol or candidate.state is not CandidateState.CONFIRMED:
                continue
            if candidate.candidate_id in known_plan_candidates:
                continue
            if candidate.expiry_at <= self._clock_now:
                # expired candidates are dropped, exactly as the live job does
                candidates.pop(key, None)
                continue
            snapshot = self._candidate_snapshot(candidate)
            context = state.risk_context(
                snapshot,
                fee_schedule=self.fee_schedule,
                risk_settings=self.risk_settings,
                cost_settings=self.cost_settings,
                strategy_settings=self.strategy_settings,
                levels=self._levels_for(candidate, symbol),
            )
            try:
                outcome = evaluate_candidate(context, self.risk_settings, self.cost_settings)
            except CostModelError as error:
                warnings.append(f"RISK_COST_MODEL:{error.code}")
                continue
            if outcome.plan is not None:
                plans.append(outcome.plan)

    def _stage_lifecycle(
        self,
        symbol: str,
        state: ReplayState,
        clock: ReplayClock,
        plans: list[SignalEligibilityPlan],
        lifecycles: dict[uuid.UUID, _Lifecycle],
    ) -> None:
        """Phase-10 semantics on replayed data (closed candles only)."""
        now = clock.now()
        tracked = {lifecycle.plan.plan_id for lifecycle in lifecycles.values()}
        for plan in plans:
            if plan.symbol != symbol or plan.plan_id in tracked:
                continue
            if plan.status is not PlanStatus.ELIGIBLE or plan.expiry_at <= now:
                continue
            if plan.as_of > now:
                continue  # a plan is not observable before it existed
            lifecycles[uuid.uuid4()] = _Lifecycle(
                signal_id=uuid.uuid4(), plan=plan, state=SignalState.WATCHING_ENTRY
            )

        candle = state.last_closed_5m(symbol)
        if candle is None:
            return
        symbol_state = state.state_for(symbol)
        for lifecycle in lifecycles.values():
            if lifecycle.plan.symbol != symbol or lifecycle.terminal_at is not None:
                continue
            bullish = lifecycle.plan.direction == "BULLISH"
            entry_low = lifecycle.plan.entry_zone.entry_low
            entry_high = lifecycle.plan.entry_zone.entry_high
            invalidation = lifecycle.plan.invalidation.invalidation_price
            targets = [target.price for target in lifecycle.plan.targets]
            close = candle.close

            if lifecycle.state is SignalState.WATCHING_ENTRY:
                if now >= lifecycle.plan.expiry_at:
                    lifecycle.state = SignalState.EXPIRED
                    lifecycle.terminal_at = now
                    continue
                breached = close <= invalidation if bullish else close >= invalidation
                if breached:
                    lifecycle.state = SignalState.INVALIDATED
                    lifecycle.terminal_at = now
                    continue
                touched = candle.low <= entry_high and candle.high >= entry_low
                confirmed = touched and entry_low <= close <= entry_high
                if confirmed and symbol_state.session_allowed and symbol_state.data_quality_ok:
                    lifecycle.state = SignalState.ACTIVE_RESEARCH
                    lifecycle.entry_confirmed_at = now
                    lifecycle.entry_observation = LifecycleObservation(
                        signal_id=lifecycle.signal_id,
                        plan_id=lifecycle.plan.plan_id,
                        candidate_id=lifecycle.plan.candidate_id,
                        state="ENTRY_CONFIRMED",
                        event_type="ENTRY_CONFIRMED",
                        state_version=3,
                        as_of=now,
                        detail={"basis": "closed 5m candle confirmed the reference zone"},
                    )
                    # a hypothetical follower reacts only after the modelled delay
                    delay = realize_delay(
                        lifecycle.entry_observation,
                        self.shadow_settings,
                        seed_base=self.seed_base,
                    )
                    lifecycle.entry_due_at = delay.scheduled_entry_at
                continue

            if lifecycle.state is SignalState.ACTIVE_RESEARCH:
                breached = close <= invalidation if bullish else close >= invalidation
                if breached:
                    lifecycle.state = SignalState.INVALIDATED
                    lifecycle.terminal_at = now
                    continue
                if targets:
                    reached_2 = len(targets) > 1 and (
                        close >= targets[1] if bullish else close <= targets[1]
                    )
                    reached_1 = close >= targets[0] if bullish else close <= targets[0]
                    if reached_2:
                        lifecycle.state = SignalState.TARGET_2_REACHED
                        lifecycle.terminal_at = now
                        continue
                    if reached_1:
                        lifecycle.state = SignalState.TARGET_1_REACHED
                        lifecycle.terminal_at = now
                        continue
                if now >= lifecycle.plan.expiry_at:
                    lifecycle.state = SignalState.EXPIRED
                    lifecycle.terminal_at = now

    def _stage_entry_capture(
        self,
        symbol: str,
        state: ReplayState,
        clock: ReplayClock,
        lifecycles: dict[uuid.UUID, _Lifecycle],
    ) -> None:
        """Capture the public market AT the modelled delayed entry instant.

        A follower who reacts late trades a different book, so the entry is
        never modelled from the lifecycle-event instant or - worse - from a
        later exit instant.
        """
        now = clock.now()
        for lifecycle in lifecycles.values():
            if (
                lifecycle.plan.symbol != symbol
                or lifecycle.entry_due_at is None
                or lifecycle.entry_market is not None
                or now < lifecycle.entry_due_at
            ):
                continue
            lifecycle.entry_market = state.market_reference(symbol)
            lifecycle.entry_capture_at = now

    def _stage_simulation(
        self,
        *,
        symbol: str,
        state: ReplayState,
        clock: ReplayClock,
        lifecycles: dict[uuid.UUID, _Lifecycle],
        simulations: list[SimulatedPositionResult],
        rejections: list[SimulationRejection],
        run_id: uuid.UUID,
    ) -> None:
        for lifecycle in lifecycles.values():
            if (
                lifecycle.plan.symbol != symbol
                or lifecycle.simulated
                or lifecycle.terminal_at is None
                or lifecycle.entry_observation is None
            ):
                continue
            # the lifecycle EVENT that closed the research window decides the
            # modelled exit reason; terminal states fall back to the state map
            reason = exit_reason_for_event(lifecycle.state.value) or exit_reason_for_state(
                lifecycle.state.value
            )
            if reason is None:
                continue
            lifecycle.simulated = True
            exit_observation = LifecycleObservation(
                signal_id=lifecycle.signal_id,
                plan_id=lifecycle.plan.plan_id,
                candidate_id=lifecycle.plan.candidate_id,
                state=lifecycle.state.value,
                event_type=lifecycle.state.value,
                state_version=5,
                as_of=lifecycle.terminal_at,
            )
            symbol_state = state.state_for(symbol)
            inputs = SimulationInputs(
                lifecycle_signal_id=lifecycle.signal_id,
                plan=self._plan_reference(lifecycle.plan),
                entry_observation=lifecycle.entry_observation,
                exit_observation=exit_observation,
                entry_market=lifecycle.entry_market,
                exit_market=state.market_reference(symbol),
                fee_schedule=self.fee_schedule,
                funding=symbol_state.funding_snapshot(clock.now()),
                funding_interval_hours=symbol_state.funding_interval_hours,
                session_allowed=symbol_state.session_allowed,
                session_state=symbol_state.session_state,
                data_quality_ok=symbol_state.data_quality_ok,
                bot_paused=False,
                duplicate_exists=False,
                plan_audit_available=True,
                candidate_audit_available=True,
                run_id=run_id,
                as_of=clock.now(),
                exit_reason=reason,
                metadata={"mode": "BACKTEST"},
            )
            outcome = simulate(
                inputs,
                self.shadow_settings,
                self.cost_settings,
                config_hash=self.simulation_config_hash,
                seed_base=self.seed_base,
            )
            if outcome.result is not None:
                result = outcome.result
                approximation = self._entry_capture_warning(lifecycle)
                if approximation is not None:
                    result = replace(
                        result,
                        warnings=(*result.warnings, approximation),
                        data_completeness=(
                            DataCompleteness.PARTIAL
                            if result.data_completeness is DataCompleteness.COMPLETE
                            else result.data_completeness
                        ),
                    )
                simulations.append(result)
            elif outcome.rejection is not None:
                rejections.append(outcome.rejection)

    def _entry_capture_warning(self, lifecycle: _Lifecycle) -> str | None:
        """Flag when candle cadence pushed the modelled entry past its slot."""
        if lifecycle.entry_due_at is None or lifecycle.entry_capture_at is None:
            return None
        drift = (lifecycle.entry_capture_at - lifecycle.entry_due_at).total_seconds()
        if drift > self.shadow_settings.delay_max_entry_staleness_seconds:
            return f"ENTRY_INSTANT_APPROXIMATED_BY_REPLAY_CADENCE:{drift:.0f}s"
        return None

    # -------------------------------------------------------------- helpers

    def _candidate_snapshot(self, candidate: SetupCandidate) -> CandidateSnapshot:
        return CandidateSnapshot(
            candidate_id=candidate.candidate_id,
            candidate_type=candidate.candidate_type.value,
            direction=candidate.direction.value,
            state=candidate.state.value,
            instrument_pk=candidate.instrument_pk,
            instrument_id=candidate.instrument_id,
            symbol=candidate.symbol,
            asset_class=candidate.asset_class,
            as_of=candidate.as_of,
            expiry_at=candidate.expiry_at,
            setup_score=candidate.setup_score,
            primary_regime=candidate.primary_regime.value,
            referenced_levels=tuple(candidate.referenced_levels),
            invalidation_conditions=tuple(candidate.invalidation_conditions),
            strategy_name=candidate.strategy_name,
            strategy_version=candidate.strategy_version,
            strategy_config_hash=candidate.config_hash,
            features=dict(candidate.features),
        )

    def _levels_from_structure(self, outcome: Any) -> tuple[TechnicalLevel, ...]:
        """Structural swings from THIS phase-8 evaluation (no new analysis)."""
        levels: list[TechnicalLevel] = []
        seen: set[tuple[str, float]] = set()
        for timeframe, analysis in (outcome.structure_by_timeframe or {}).items():
            for swing in analysis.swings:
                kind = f"SWING_{swing.swing_type.value}"
                price = float(swing.price)
                if (kind, price) in seen:
                    continue
                seen.add((kind, price))
                levels.append(
                    TechnicalLevel(
                        price=price,
                        kind=kind,
                        timeframe=str(timeframe),
                        relevance=float(swing.relevance),
                        source_id=f"replay-swing:{swing.swing_id}",
                    )
                )
        return tuple(levels)

    def _levels_for(self, candidate: SetupCandidate, symbol: str) -> tuple[TechnicalLevel, ...]:
        """Phase-8 structural levels plus the candidate's own references."""
        levels: list[TechnicalLevel] = list(self._levels_by_symbol.get(symbol, ()))
        known = {(level.kind, level.price) for level in levels}
        for index, level in enumerate(candidate.referenced_levels):
            price = level.get("price") if isinstance(level, dict) else None
            if price is None:
                continue
            kind = str(level.get("type") or level.get("kind") or "REFERENCED_LEVEL")
            if (kind, float(price)) in known:
                continue
            levels.append(
                TechnicalLevel(
                    price=float(price),
                    kind=kind,
                    timeframe=str(level.get("timeframe", "15m")),
                    relevance=float(level.get("relevance", 0.6)),
                    source_id=f"replay:{candidate.candidate_id}:{index}",
                )
            )
        return tuple(levels)

    def _plan_reference(self, plan: SignalEligibilityPlan) -> SimulationPlanReference:
        return SimulationPlanReference(
            plan_id=plan.plan_id,
            candidate_id=plan.candidate_id,
            instrument_pk=plan.instrument_pk,
            instrument_id=plan.instrument_id,
            symbol=plan.symbol,
            asset_class=plan.asset_class,
            candidate_type=plan.candidate_type,
            direction=plan.direction,
            entry_reference_price=plan.entry_zone.entry_reference_price,
            invalidation_price=plan.invalidation.invalidation_price,
            target_prices=tuple(target.price for target in plan.targets),
            reference_quantity=plan.position.reference_quantity,
            reference_notional=plan.position.reference_notional,
            risk_per_unit=plan.position.risk_per_unit,
            virtual_account_pusd=plan.position.virtual_account_pusd,
            strategy_version=plan.strategy_version,
            risk_model_version=plan.risk_model_version,
            cost_model_version=plan.cost_model_version,
            fee_schedule_version=plan.fee_schedule_version,
            execution_assumption_version=plan.execution_assumption_version,
        )


def default_window(settings: BacktestSettings, fallback_days: int = 7) -> tuple[datetime, datetime]:
    """Configured default replay window (empty config -> recent window)."""
    if settings.default_start_at and settings.default_end_at:
        return (
            datetime.fromisoformat(settings.default_start_at),
            datetime.fromisoformat(settings.default_end_at),
        )
    raise ValueError(
        "no default backtest window configured - a run manifest must carry "
        f"explicit start_at/end_at (fallback of {fallback_days}d is not assumed)"
    )
