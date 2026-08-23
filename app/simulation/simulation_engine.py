"""Deterministic hypothetical simulation of one research lifecycle.

Pure logic over immutable snapshots: admission gates, follower delay,
modelled entry, modelled exit, modelled funding and the resulting
hypothetical net figures.  The same engine serves shadow mode and
historical backtests so both use identical assumptions.

Nothing in here executes, fills, holds or closes anything.  Every output
is a model value derived from public data under explicitly documented
assumptions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.config import CostSettings, ShadowSimulationSettings
from app.simulation.entry_simulator import model_entry
from app.simulation.enums import (
    DataCompleteness,
    SimulatedPositionState,
    SimulationExitReason,
    SimulationRejectionCode,
)
from app.simulation.exit_simulator import model_exit, model_partial_reduction
from app.simulation.follower_delay import realize_delay
from app.simulation.models import (
    SimulatedPositionResult,
    SimulationInputs,
    SimulationRejection,
)
from app.simulation.simulated_costs import cost_to_risk_pct
from app.simulation.simulated_execution import ExecutionModelError
from app.simulation.simulated_funding import model_funding
from app.simulation.simulated_lifecycle import is_admissible_state
from app.simulation.simulated_position import (
    cost_breakdown,
    gross_result,
    net_result,
    risk_units,
)
from app.simulation.simulation_rejection import build_rejection
from app.simulation.version import simulation_dedupe_key


@dataclass(frozen=True)
class SimulationOutcome:
    """Either a modelled simulation result or a structured rejection."""

    result: SimulatedPositionResult | None
    rejection: SimulationRejection | None

    @property
    def admitted(self) -> bool:
        return self.result is not None


def _rejected(
    inputs: SimulationInputs,
    code: SimulationRejectionCode,
    detail: str,
    *,
    model_version: str,
    config_hash: str,
) -> SimulationOutcome:
    return SimulationOutcome(
        result=None,
        rejection=build_rejection(
            inputs,
            code,
            detail,
            simulation_model_version=model_version,
            simulation_config_hash=config_hash,
        ),
    )


def check_admission(
    inputs: SimulationInputs, settings: ShadowSimulationSettings
) -> tuple[SimulationRejectionCode | None, str]:
    """Admission gates for one hypothetical simulation (conservative)."""
    if not settings.mode_enabled:
        return SimulationRejectionCode.SIMULATION_DISABLED, "shadow simulation disabled"
    if inputs.bot_paused:
        return SimulationRejectionCode.BOT_PAUSED, "global paused mode - no new simulation"
    if not is_admissible_state(inputs.entry_observation.state):
        return (
            SimulationRejectionCode.LIFECYCLE_NOT_ELIGIBLE,
            f"lifecycle state {inputs.entry_observation.state} is not simulatable",
        )
    if not inputs.plan_audit_available:
        return SimulationRejectionCode.PLAN_AUDIT_MISSING, "underlying plan not auditable"
    if not inputs.candidate_audit_available:
        return (
            SimulationRejectionCode.CANDIDATE_AUDIT_MISSING,
            "underlying candidate not auditable",
        )
    if inputs.duplicate_exists:
        return (
            SimulationRejectionCode.DUPLICATE_SIMULATION,
            "an active hypothetical simulation for this lifecycle already exists",
        )
    if not inputs.session_allowed:
        return (
            SimulationRejectionCode.SESSION_NOT_ALLOWED,
            f"session {inputs.session_state} not allowed at the reference instant",
        )
    if not inputs.data_quality_ok:
        return (
            SimulationRejectionCode.ENTRY_BOOK_STALE,
            "data quality insufficient at the reference instant",
        )
    if inputs.fee_schedule is None:
        return (
            SimulationRejectionCode.FEE_SCHEDULE_UNAVAILABLE,
            "no administered fee schedule for the reference instant",
        )
    if inputs.plan.reference_quantity <= 0 or inputs.plan.risk_per_unit <= 0:
        return (
            SimulationRejectionCode.CONFIGURATION_INVALID,
            "plan reference quantity/risk incomplete",
        )
    return None, "admitted for hypothetical simulation"


def simulate(
    inputs: SimulationInputs,
    shadow_settings: ShadowSimulationSettings,
    cost_settings: CostSettings,
    *,
    config_hash: str,
    seed_base: int = 0,
) -> SimulationOutcome:
    """Model one hypothetical delayed-follower simulation end to end."""
    model_version = shadow_settings.simulation_model_version
    code, detail = check_admission(inputs, shadow_settings)
    if code is not None:
        return _rejected(inputs, code, detail, model_version=model_version, config_hash=config_hash)

    plan = inputs.plan
    dedupe_key = simulation_dedupe_key(
        lifecycle_signal_id=str(inputs.lifecycle_signal_id),
        run_id=str(inputs.run_id),
        simulation_model_version=model_version,
        simulation_config_hash=config_hash,
    )
    delay = realize_delay(inputs.entry_observation, shadow_settings, seed_base=seed_base)

    try:
        entry = model_entry(
            plan=plan,
            delay=delay,
            market=inputs.entry_market,
            schedule=inputs.fee_schedule,
            cost_settings=cost_settings,
            shadow_settings=shadow_settings,
        )
    except ExecutionModelError as error:
        return _rejected(
            inputs,
            error.code,
            error.detail,
            model_version=model_version,
            config_hash=config_hash,
        )

    warnings: list[str] = []
    exit_reason = inputs.exit_reason
    if inputs.exit_observation is None or exit_reason is None:
        # the lifecycle is still running - the modelled entry stands alone
        return SimulationOutcome(
            result=SimulatedPositionResult(
                simulation_position_id=uuid.uuid4(),
                run_id=inputs.run_id,
                lifecycle_signal_id=inputs.lifecycle_signal_id,
                plan_id=plan.plan_id,
                candidate_id=plan.candidate_id,
                symbol=plan.symbol,
                asset_class=plan.asset_class,
                candidate_type=plan.candidate_type,
                direction=plan.direction,
                state=SimulatedPositionState.OPEN_SIMULATION,
                event_reference_at=inputs.entry_observation.as_of,
                delay=delay,
                entry=entry,
                exit_execution=None,
                partial_reduction=None,
                exit_reason=None,
                funding=None,
                costs=None,
                gross_result=None,
                net_result=None,
                gross_r=None,
                net_r=None,
                duration_seconds=None,
                data_completeness=DataCompleteness.PARTIAL,
                warnings=("SIMULATION_STILL_OPEN",),
                dedupe_key=dedupe_key,
                metadata={"note": "hypothetical simulation open - no modelled exit yet"},
            ),
            rejection=None,
        )

    partial = None
    if exit_reason is not SimulationExitReason.TARGET_1_OBSERVED:
        try:
            partial = model_partial_reduction(
                plan=plan,
                market=inputs.exit_market,
                schedule=inputs.fee_schedule,
                cost_settings=cost_settings,
                shadow_settings=shadow_settings,
            )
        except ExecutionModelError as error:
            warnings.append(f"PARTIAL_REDUCTION_UNMODELED:{error.code.value}")
            partial = None

    remaining = entry.quantity - (partial.quantity if partial is not None else 0.0)
    try:
        exit_execution = model_exit(
            plan=plan,
            reason=exit_reason,
            market=inputs.exit_market,
            schedule=inputs.fee_schedule,
            quantity=max(remaining, 0.0),
            cost_settings=cost_settings,
            shadow_settings=shadow_settings,
        )
    except ExecutionModelError as error:
        if shadow_settings.unmodeled_exit_policy == "REJECT":
            return _rejected(
                inputs,
                error.code,
                error.detail,
                model_version=model_version,
                config_hash=config_hash,
            )
        return SimulationOutcome(
            result=SimulatedPositionResult(
                simulation_position_id=uuid.uuid4(),
                run_id=inputs.run_id,
                lifecycle_signal_id=inputs.lifecycle_signal_id,
                plan_id=plan.plan_id,
                candidate_id=plan.candidate_id,
                symbol=plan.symbol,
                asset_class=plan.asset_class,
                candidate_type=plan.candidate_type,
                direction=plan.direction,
                state=SimulatedPositionState.UNMODELED_EXIT,
                event_reference_at=inputs.entry_observation.as_of,
                delay=delay,
                entry=entry,
                exit_execution=None,
                partial_reduction=partial,
                exit_reason=exit_reason,
                funding=None,
                costs=None,
                gross_result=None,
                net_result=None,
                gross_r=None,
                net_r=None,
                duration_seconds=(inputs.exit_observation.as_of - entry.as_of).total_seconds(),
                data_completeness=DataCompleteness.INSUFFICIENT,
                warnings=(*warnings, f"EXIT_UNMODELED:{error.code.value}"),
                dedupe_key=dedupe_key,
                metadata={
                    "note": (
                        "modelled exit not derivable from public book data - result "
                        "excluded from completed simulation metrics"
                    ),
                    "detail": error.detail,
                },
            ),
            rejection=None,
        )

    hold_seconds = max(0.0, (exit_execution.as_of - entry.as_of).total_seconds())
    try:
        funding = model_funding(
            plan=plan,
            funding=inputs.funding,
            notional=entry.notional,
            hold_seconds=hold_seconds,
            entry_at=entry.as_of,
            cost_settings=cost_settings,
            shadow_settings=shadow_settings,
        )
    except ExecutionModelError as error:
        return _rejected(
            inputs,
            error.code,
            error.detail,
            model_version=model_version,
            config_hash=config_hash,
        )
    warnings.extend(funding.warnings)

    gross = gross_result(plan=plan, entry=entry, exit_execution=exit_execution, partial=partial)
    costs = cost_breakdown(
        entry=entry, exit_execution=exit_execution, partial=partial, funding=funding
    )
    net = net_result(gross, costs)
    completeness = (
        DataCompleteness.COMPLETE
        if funding.data_available and not warnings
        else DataCompleteness.PARTIAL
    )
    if shadow_settings.simulation_require_complete_cost_data and not funding.data_available:
        completeness = DataCompleteness.INSUFFICIENT
    state = (
        SimulatedPositionState.CLOSED_SIMULATION
        if completeness is not DataCompleteness.INSUFFICIENT
        else SimulatedPositionState.INCOMPLETE
    )
    return SimulationOutcome(
        result=SimulatedPositionResult(
            simulation_position_id=uuid.uuid4(),
            run_id=inputs.run_id,
            lifecycle_signal_id=inputs.lifecycle_signal_id,
            plan_id=plan.plan_id,
            candidate_id=plan.candidate_id,
            symbol=plan.symbol,
            asset_class=plan.asset_class,
            candidate_type=plan.candidate_type,
            direction=plan.direction,
            state=state,
            event_reference_at=inputs.entry_observation.as_of,
            delay=delay,
            entry=entry,
            exit_execution=exit_execution,
            partial_reduction=partial,
            exit_reason=exit_reason,
            funding=funding,
            costs=costs,
            gross_result=gross,
            net_result=net,
            gross_r=risk_units(gross, plan=plan, quantity=entry.quantity),
            net_r=risk_units(net, plan=plan, quantity=entry.quantity),
            duration_seconds=hold_seconds,
            data_completeness=completeness,
            warnings=tuple(warnings),
            dedupe_key=dedupe_key,
            metadata={
                "note": (
                    "hypothetical simulation result - modelled prices on public data, "
                    "no execution, no position, no statement about real outcomes"
                ),
                "cost_to_risk_pct": cost_to_risk_pct(costs, plan=plan, quantity=entry.quantity),
                "delay_seconds": delay.delay_seconds,
                "exit_reason": exit_reason.value,
            },
        ),
        rejection=None,
    )
