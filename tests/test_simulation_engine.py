"""Pure simulation core: delay models, book walks, costs, funding, results.

Nothing here touches persistence or the network; every value is a
modelled, hypothetical figure.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.simulation_helpers import (
    COST_SETTINGS,
    FEE_SCHEDULE,
    NOW,
    market_reference,
    observation,
    plan_reference,
    shadow_settings,
    simulation_inputs,
)

from app.simulation.entry_simulator import model_entry
from app.simulation.enums import (
    DataCompleteness,
    DelayModel,
    SimulatedPositionState,
    SimulationExitReason,
    SimulationRejectionCode,
)
from app.simulation.exit_simulator import model_exit
from app.simulation.follower_delay import realize_delay
from app.simulation.simulated_execution import ExecutionModelError
from app.simulation.simulation_engine import check_admission, simulate
from app.simulation.version import shadow_configuration_hash

SHADOW = shadow_settings()
CONFIG_HASH = shadow_configuration_hash(SHADOW)


def run(**overrides):
    inputs = simulation_inputs(**overrides)
    return simulate(
        inputs, overrides.pop("settings", SHADOW), COST_SETTINGS, config_hash=CONFIG_HASH
    )


# ------------------------------------------------------------------- delay


def test_fixed_delay_is_deterministic() -> None:
    entry = observation(
        plan_reference().plan_id,
        plan_reference(),
        state="ENTRY_CONFIRMED",
        event_type="ENTRY_CONFIRMED",
        at=NOW,
    )
    delay = realize_delay(entry, shadow_settings(delay_fixed_seconds=25.0))
    assert delay.model is DelayModel.FIXED_SECONDS
    assert delay.delay_seconds == 25.0
    assert delay.scheduled_entry_at == NOW + timedelta(seconds=25)
    assert delay == realize_delay(entry, shadow_settings(delay_fixed_seconds=25.0))


def test_uniform_and_lognormal_delays_are_seeded_and_bounded() -> None:
    plan = plan_reference()
    entry = observation(
        plan.plan_id, plan, state="ENTRY_CONFIRMED", event_type="ENTRY_CONFIRMED", at=NOW
    )
    uniform = shadow_settings(
        delay_model="UNIFORM_RANGE_SECONDS", delay_min_seconds=10.0, delay_max_seconds=30.0
    )
    first = realize_delay(entry, uniform, seed_base=7)
    second = realize_delay(entry, uniform, seed_base=7)
    assert first == second  # deterministic seed
    assert 10.0 <= first.delay_seconds <= 30.0
    assert first.seed is not None
    other = realize_delay(entry, uniform, seed_base=8)
    assert other.seed != first.seed

    lognormal = shadow_settings(delay_model="LOGNORMAL_DELAY")
    drawn = realize_delay(entry, lognormal, seed_base=3)
    assert drawn.delay_seconds >= 0
    assert drawn == realize_delay(entry, lognormal, seed_base=3)


def test_event_timestamp_only_models_no_delay() -> None:
    plan = plan_reference()
    entry = observation(
        plan.plan_id, plan, state="ENTRY_CONFIRMED", event_type="ENTRY_CONFIRMED", at=NOW
    )
    delay = realize_delay(entry, shadow_settings(delay_model="EVENT_TIMESTAMP_ONLY"))
    assert delay.delay_seconds == 0.0
    assert delay.scheduled_entry_at == NOW


# ------------------------------------------------------------- admission


def test_admission_gates() -> None:
    assert check_admission(simulation_inputs(), SHADOW)[0] is None
    disabled = shadow_settings(mode_enabled=False)
    assert check_admission(simulation_inputs(), disabled)[0] is (
        SimulationRejectionCode.SIMULATION_DISABLED
    )
    cases = [
        ({"bot_paused": True}, SimulationRejectionCode.BOT_PAUSED),
        ({"duplicate_exists": True}, SimulationRejectionCode.DUPLICATE_SIMULATION),
        ({"session_allowed": False}, SimulationRejectionCode.SESSION_NOT_ALLOWED),
        ({"data_quality_ok": False}, SimulationRejectionCode.ENTRY_BOOK_STALE),
        ({"plan_audit_available": False}, SimulationRejectionCode.PLAN_AUDIT_MISSING),
        ({"candidate_audit_available": False}, SimulationRejectionCode.CANDIDATE_AUDIT_MISSING),
        ({"fee_schedule": None}, SimulationRejectionCode.FEE_SCHEDULE_UNAVAILABLE),
    ]
    for overrides, expected in cases:
        code, _ = check_admission(simulation_inputs(**overrides), SHADOW)
        assert code is expected, f"expected {expected}, got {code}"


def test_lifecycle_state_must_have_confirmed_entry() -> None:
    plan = plan_reference()
    inputs = simulation_inputs(
        plan=plan,
        entry_observation=observation(
            plan.plan_id, plan, state="WATCHING_ENTRY", event_type="WATCHING_ENTRY", at=NOW
        ),
    )
    code, _ = check_admission(inputs, SHADOW)
    assert code is SimulationRejectionCode.LIFECYCLE_NOT_ELIGIBLE


# ------------------------------------------------------------ book walks


def test_bullish_and_bearish_walks_use_the_adverse_side() -> None:
    outcome = run()
    result = outcome.result
    assert result is not None and result.entry is not None
    assert result.entry.side_consumed == "ask"  # bullish entry consumes the ask
    assert result.exit_execution is not None
    assert result.exit_execution.side_consumed == "bid"
    assert result.entry.modelled_price > 100.0  # worse than the mid

    bearish_plan = plan_reference(
        direction="BEARISH",
        candidate_type="BEARISH_SWEEP_REVERSAL",
        invalidation_price=103.0,
        target_prices=(96.0, 93.0),
    )
    bear = run(plan=bearish_plan, exit_market=market_reference(96.1, NOW + timedelta(hours=2)))
    assert bear.result is not None and bear.result.entry is not None
    assert bear.result.entry.side_consumed == "bid"
    assert bear.result.exit_execution.side_consumed == "ask"
    assert bear.result.net_r is not None and bear.result.net_r > 0


def test_entry_rejections_for_missing_stale_and_thin_data() -> None:
    assert run(entry_market=None).rejection.primary_code is (
        SimulationRejectionCode.ENTRY_DELAY_DATA_UNAVAILABLE
    )
    stale = market_reference(100.05, NOW + timedelta(seconds=20), staleness_seconds=600)
    assert run(entry_market=stale).rejection.primary_code is (
        SimulationRejectionCode.ENTRY_BOOK_STALE
    )
    thin = market_reference(100.05, NOW + timedelta(seconds=20), depth=0.01)
    assert run(entry_market=thin).rejection.primary_code is (
        SimulationRejectionCode.ENTRY_BOOK_INSUFFICIENT_DEPTH
    )


def test_excessive_entry_chase_is_rejected() -> None:
    far = market_reference(103.0, NOW + timedelta(seconds=20))
    outcome = run(entry_market=far)
    assert outcome.rejection is not None
    assert outcome.rejection.primary_code is SimulationRejectionCode.ENTRY_SLIPPAGE_EXCESSIVE


def test_unmodeled_exit_policies() -> None:
    thin_exit = market_reference(104.1, NOW + timedelta(hours=2), depth=0.01)
    marked = run(exit_market=thin_exit)
    assert marked.result is not None
    assert marked.result.state is SimulatedPositionState.UNMODELED_EXIT
    assert marked.result.data_completeness is DataCompleteness.INSUFFICIENT
    assert any("EXIT_UNMODELED" in warning for warning in marked.result.warnings)
    assert marked.result.net_result is None  # never counted as a completed result

    strict = simulate(
        simulation_inputs(exit_market=thin_exit),
        shadow_settings(unmodeled_exit_policy="REJECT"),
        COST_SETTINGS,
        config_hash=CONFIG_HASH,
    )
    assert strict.result is None
    assert strict.rejection.primary_code is (SimulationRejectionCode.EXIT_BOOK_INSUFFICIENT_DEPTH)


def test_stale_exit_book_is_flagged() -> None:
    stale_exit = market_reference(104.1, NOW + timedelta(hours=2), staleness_seconds=600)
    outcome = run(exit_market=stale_exit)
    assert outcome.result is not None
    assert outcome.result.state is SimulatedPositionState.UNMODELED_EXIT


# ------------------------------------------------------- costs and funding


def test_costs_reuse_the_phase9_engine_and_are_counted_once() -> None:
    result = run().result
    assert result is not None and result.costs is not None
    costs = result.costs
    assert costs.entry_fee > 0 and costs.exit_fee > 0
    assert costs.entry_slippage > 0 and costs.exit_slippage > 0
    assert costs.funding_cost >= 0
    assert costs.total == pytest.approx(
        costs.fees_total + costs.slippage_total + costs.funding_cost
    )
    # net = gross - every modelled friction, exactly once
    assert result.net_result == pytest.approx(result.gross_result - costs.total)
    assert result.net_r < result.gross_r


def test_funding_policy_blocks_or_marks_missing_data() -> None:
    required = run(funding=None)
    assert required.rejection is not None
    assert required.rejection.primary_code is SimulationRejectionCode.FUNDING_UNAVAILABLE

    tolerant = simulate(
        simulation_inputs(funding=None),
        shadow_settings(
            simulation_require_funding_data=False,
            simulation_require_complete_cost_data=False,
        ),
        COST_SETTINGS,
        config_hash=CONFIG_HASH,
    )
    assert tolerant.result is not None
    assert tolerant.result.funding is not None
    assert tolerant.result.funding.data_available is False
    assert "FUNDING_DATA_UNAVAILABLE" in tolerant.result.warnings
    assert tolerant.result.data_completeness is DataCompleteness.PARTIAL


def test_funding_is_modelled_over_the_actual_holding_window() -> None:
    short = run(exit_at=NOW + timedelta(minutes=10)).result
    long = run(exit_at=NOW + timedelta(hours=20)).result
    assert short is not None and long is not None
    assert long.funding.hours_modelled > short.funding.hours_modelled
    assert long.funding.funding_cost >= short.funding.funding_cost


# ------------------------------------------------------------ results


def test_partial_target_reduction_is_opt_in() -> None:
    default_result = run().result
    assert default_result is not None and default_result.partial_reduction is None

    enabled = simulate(
        simulation_inputs(
            exit_reason=SimulationExitReason.TARGET_2_OBSERVED,
            exit_market=market_reference(107.2, NOW + timedelta(hours=2)),
        ),
        shadow_settings(
            simulation_allow_partial_target_simulation=True,
            simulation_partial_target_pct=40.0,
        ),
        COST_SETTINGS,
        config_hash=CONFIG_HASH,
    )
    assert enabled.result is not None
    assert enabled.result.partial_reduction is not None
    assert enabled.result.partial_reduction.quantity == pytest.approx(4.0)


def test_open_lifecycle_yields_an_open_simulation() -> None:
    outcome = run(exit_observation=None, exit_reason=None)
    assert outcome.result is not None
    assert outcome.result.state is SimulatedPositionState.OPEN_SIMULATION
    assert outcome.result.net_result is None
    assert "SIMULATION_STILL_OPEN" in outcome.result.warnings


def test_exit_reasons_map_to_conservative_execution_modes() -> None:
    for reason in (
        SimulationExitReason.TARGET_1_OBSERVED,
        SimulationExitReason.INVALIDATION_CONDITION,
        SimulationExitReason.TECHNICAL_EXIT_CONDITION,
        SimulationExitReason.EXPIRED,
        SimulationExitReason.SUPERSEDED,
        SimulationExitReason.SESSION_POLICY_END,
        SimulationExitReason.LIFECYCLE_TERMINAL,
        SimulationExitReason.DATA_INVALID,
    ):
        outcome = run(exit_reason=reason)
        assert outcome.result is not None, reason
        assert outcome.result.exit_reason is reason


def test_invalidation_models_about_one_r_loss() -> None:
    outcome = run(
        exit_reason=SimulationExitReason.INVALIDATION_CONDITION,
        exit_market=market_reference(97.0, NOW + timedelta(hours=1)),
    )
    result = outcome.result
    assert result is not None and result.net_r is not None
    assert -1.6 < result.net_r < -0.9


def test_result_metadata_never_claims_execution() -> None:
    result = run().result
    assert result is not None
    note = result.metadata["note"].lower()
    for term in ("executed", "filled", "realised", "real position"):
        assert term not in note
    assert "hypothetical" in note


def test_simulation_is_deterministic() -> None:
    inputs = simulation_inputs()
    first = simulate(inputs, SHADOW, COST_SETTINGS, config_hash=CONFIG_HASH)
    second = simulate(inputs, SHADOW, COST_SETTINGS, config_hash=CONFIG_HASH)
    assert first.result.net_result == second.result.net_result
    assert first.result.delay == second.result.delay


def test_direct_entry_and_exit_helpers_raise_structured_errors() -> None:
    plan = plan_reference()
    delay = realize_delay(
        observation(
            plan.plan_id, plan, state="ENTRY_CONFIRMED", event_type="ENTRY_CONFIRMED", at=NOW
        ),
        SHADOW,
    )
    with pytest.raises(ExecutionModelError) as error:
        model_entry(
            plan=plan,
            delay=delay,
            market=None,
            schedule=FEE_SCHEDULE,
            cost_settings=COST_SETTINGS,
            shadow_settings=SHADOW,
        )
    assert error.value.code is SimulationRejectionCode.ENTRY_DELAY_DATA_UNAVAILABLE

    with pytest.raises(ExecutionModelError) as error:
        model_exit(
            plan=plan,
            reason=SimulationExitReason.TARGET_1_OBSERVED,
            market=None,
            schedule=FEE_SCHEDULE,
            quantity=1.0,
            cost_settings=COST_SETTINGS,
            shadow_settings=SHADOW,
        )
    assert error.value.code is SimulationRejectionCode.EXIT_BOOK_STALE
