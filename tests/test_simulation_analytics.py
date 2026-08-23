"""Walk-forward, metrics, drawdown, bootstrap and reporting.

All figures describe hypothetical simulations; the tests assert the
sample-size guard, the multi-criteria selection and the mandatory
disclaimer wording.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from tests.simulation_helpers import (
    COST_SETTINGS,
    NOW,
    backtest_settings,
    market_reference,
    shadow_settings,
    simulation_inputs,
)

from app.simulation.analytics import SEGMENT_DIMENSIONS, segment_metrics
from app.simulation.bootstrap_statistics import UNCERTAINTY_NOTE, bootstrap_mean
from app.simulation.drawdown import (
    compute_drawdown,
    equity_curve,
    max_consecutive_losses,
)
from app.simulation.enums import (
    MetricSampleStatus,
    WalkForwardMode,
    WalkForwardWindow,
)
from app.simulation.explainability import (
    BACKTEST_DISCLAIMER,
    SIMULATION_DISCLAIMER,
    contains_forbidden_language,
    metric_set_view,
    simulation_row,
)
from app.simulation.metrics import RATIO_METRICS, compute_metrics
from app.simulation.reporting import build_report, report_to_json, simulations_to_csv
from app.simulation.simulation_engine import simulate
from app.simulation.version import shadow_configuration_hash
from app.simulation.walk_forward import (
    ConfigurationCandidate,
    SegmentEvidence,
    build_splits,
    select_configuration,
)

SHADOW = shadow_settings()
CONFIG_HASH = shadow_configuration_hash(SHADOW)


def make_result(net_r: float, *, symbol: str = "BTC-PERP", exit_price: float = 104.1):
    """One completed hypothetical simulation with a chosen net R."""
    outcome = simulate(
        simulation_inputs(
            plan=dataclasses.replace(simulation_inputs().plan, symbol=symbol),
            exit_market=market_reference(exit_price, NOW + timedelta(hours=2)),
        ),
        SHADOW,
        COST_SETTINGS,
        config_hash=CONFIG_HASH,
    )
    assert outcome.result is not None
    risk = outcome.result.entry.quantity * 3.15
    return dataclasses.replace(
        outcome.result,
        net_r=net_r,
        gross_r=net_r + 0.05,
        net_result=net_r * risk,
        gross_result=(net_r + 0.05) * risk,
        symbol=symbol,
        metadata={**outcome.result.metadata, "risk_per_unit": 3.15},
    )


# ------------------------------------------------------------ walk-forward


def test_rolling_and_expanding_splits_are_ascending_without_leakage() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 4, 1, tzinfo=UTC)
    settings = backtest_settings(
        walk_forward_train_days=20,
        walk_forward_validation_days=5,
        walk_forward_test_days=5,
        walk_forward_step_days=10,
    )
    rolling = build_splits(start_at=start, end_at=end, settings=settings)
    assert len(rolling) >= 3
    for split in rolling:
        assert split.mode is WalkForwardMode.ROLLING
        assert split.train.end_at == split.validation.start_at
        assert split.validation.end_at == split.test.start_at
        assert not split.leaks_test_data()
    assert [split.test.start_at for split in rolling] == sorted(
        split.test.start_at for split in rolling
    )

    expanding = build_splits(
        start_at=start,
        end_at=end,
        settings=backtest_settings(
            walk_forward_mode="EXPANDING",
            walk_forward_train_days=20,
            walk_forward_validation_days=5,
            walk_forward_test_days=5,
            walk_forward_step_days=10,
        ),
    )
    assert all(split.train.start_at == start for split in expanding)
    assert expanding[-1].train.end_at > expanding[0].train.end_at


def _evidence(key: str, *, net: float, drawdown: float, complete: int, window):
    return SegmentEvidence(
        configuration_key=key,
        window=window,
        complete_simulations=complete,
        incomplete_simulations=0,
        rejected_simulations=1,
        average_net_r=net,
        median_net_r=net,
        max_drawdown_r=drawdown,
        cost_share_pct=10.0,
        data_completeness_rate=0.95,
    )


def test_selection_never_picks_highest_return_alone() -> None:
    candidates = (
        ConfigurationCandidate("conservative", {"delay": 20}),
        ConfigurationCandidate("aggressive", {"delay": 5}),
    )
    evidence = tuple(
        _evidence(key, net=net, drawdown=drawdown, complete=40, window=window)
        for key, net, drawdown in (
            ("conservative", 0.35, -1.0),
            ("aggressive", 0.95, -6.0),  # better return, far deeper drawdown
        )
        for window in (WalkForwardWindow.TRAIN, WalkForwardWindow.VALIDATION)
    )
    result = select_configuration(evidence, min_simulations=20, candidates=candidates)
    assert result.selected_key == "conservative"
    assert result.sample_status is MetricSampleStatus.SUFFICIENT
    assert "never by result alone" in result.rationale
    assert set(result.criteria["weights"]) == {
        "average_net_r",
        "max_drawdown_r",
        "data_completeness",
        "cost_share",
        "sample_size",
    }


def test_selection_ignores_test_window_evidence() -> None:
    candidates = (ConfigurationCandidate("a", {}), ConfigurationCandidate("b", {}))
    evidence = (
        _evidence("a", net=0.2, drawdown=-1.0, complete=30, window=WalkForwardWindow.TRAIN),
        _evidence("a", net=0.2, drawdown=-1.0, complete=30, window=WalkForwardWindow.VALIDATION),
        # a spectacular TEST result must not influence the choice
        _evidence("b", net=5.0, drawdown=-0.1, complete=99, window=WalkForwardWindow.TEST),
    )
    result = select_configuration(evidence, min_simulations=20, candidates=candidates)
    assert result.selected_key == "a"
    assert any("b:" in warning for warning in result.warnings)


def test_selection_reports_insufficient_sample() -> None:
    candidates = (ConfigurationCandidate("a", {}),)
    evidence = (_evidence("a", net=1.0, drawdown=-0.2, complete=3, window=WalkForwardWindow.TRAIN),)
    result = select_configuration(evidence, min_simulations=20, candidates=candidates)
    assert result.selected_key is None
    assert result.sample_status is MetricSampleStatus.INSUFFICIENT_SAMPLE
    assert "minimum number of complete hypothetical simulations" in result.rationale


def test_selection_is_deterministic_on_ties() -> None:
    candidates = (ConfigurationCandidate("b", {}), ConfigurationCandidate("a", {}))
    evidence = tuple(
        _evidence(key, net=0.5, drawdown=-1.0, complete=30, window=window)
        for key in ("a", "b")
        for window in (WalkForwardWindow.TRAIN, WalkForwardWindow.VALIDATION)
    )
    first = select_configuration(evidence, min_simulations=20, candidates=candidates)
    second = select_configuration(evidence, min_simulations=20, candidates=candidates)
    assert first.selected_key == second.selected_key == "a"  # key ascending


# ---------------------------------------------------------------- metrics


def test_metrics_describe_hypothetical_outcomes() -> None:
    results = [make_result(value) for value in (1.5, -1.0, 2.0, -1.0, 0.0)]
    metrics = compute_metrics(
        results,
        rejected=2,
        min_complete_simulations=3,
        virtual_account_pusd=10_000.0,
        metrics_version="smv-1",
        disclaimer_version="sdv-1",
    )
    assert metrics.sample_status == MetricSampleStatus.SUFFICIENT.value
    assert metrics.value_of("simulations_completed") == 5
    assert metrics.value_of("simulated_win_count") == 2
    assert metrics.value_of("simulated_loss_count") == 2
    assert metrics.value_of("simulated_break_even_count") == 1
    assert metrics.value_of("hypothetical_win_rate") == pytest.approx(40.0)
    assert metrics.value_of("average_net_r") == pytest.approx(0.3)
    assert metrics.value_of("median_net_r") == pytest.approx(0.0)
    assert metrics.value_of("maximum_consecutive_losses") == 1
    assert metrics.value_of("profit_factor_hypothetical") is not None
    assert metrics.value_of("max_drawdown_r") is not None
    assert metrics.value_of("rejection_rate") == pytest.approx(2 / 7 * 100)
    assert metrics.value_of("fees_total") > 0
    assert metrics.value_of("funding_total") >= 0
    names = {item.name for item in metrics.values}
    assert {"cost_total", "slippage_total", "costs_as_pct_of_risk"} <= names


def test_insufficient_sample_withholds_ratio_metrics() -> None:
    metrics = compute_metrics(
        [make_result(1.0)],
        min_complete_simulations=20,
        metrics_version="smv-1",
        disclaimer_version="sdv-1",
    )
    assert metrics.sample_status == MetricSampleStatus.INSUFFICIENT_SAMPLE.value
    for name in RATIO_METRICS:
        assert metrics.value_of(name) is None
    assert any("INSUFFICIENT_SAMPLE" in warning for warning in metrics.warnings)
    assert any("no conclusion" in warning for warning in metrics.warnings)


def test_metric_names_are_explicitly_hypothetical() -> None:
    metrics = compute_metrics(
        [make_result(1.0)],
        min_complete_simulations=1,
        metrics_version="smv-1",
        disclaimer_version="sdv-1",
    )
    names = {item.name for item in metrics.values}
    assert "hypothetical_win_rate" in names
    assert "profit_factor_hypothetical" in names
    assert {"simulations_total", "simulations_completed"} <= names
    for item in metrics.values:
        assert contains_forbidden_language(item.detail) == ()


def test_segmentation_covers_the_required_dimensions() -> None:
    required = {
        "SYMBOL",
        "ASSET_CLASS",
        "SESSION_STATE",
        "MARKET_REGIME",
        "CANDIDATE_TYPE",
        "RESEARCH_DIRECTION",
        "SETUP_SCORE_BUCKET",
        "ELIGIBILITY_SCORE_BUCKET",
        "STRATEGY_VERSION",
        "RISK_MODEL_VERSION",
        "COST_MODEL_VERSION",
        "FEE_SCHEDULE_VERSION",
        "DELAY_MODEL",
        "EXIT_REASON",
        "DATA_COMPLETENESS",
        "PERIOD_DAY",
    }
    assert required <= set(SEGMENT_DIMENSIONS)

    results = [make_result(1.0, symbol="BTC-PERP"), make_result(-1.0, symbol="ETH-PERP")]
    sets = segment_metrics(
        results,
        dimensions=("SYMBOL", "DELAY_MODEL"),
        min_complete_simulations=1,
        metrics_version="smv-1",
        disclaimer_version="sdv-1",
    )
    keys = {(item.segment_kind, item.segment_key) for item in sets}
    assert ("SYMBOL", "BTC-PERP") in keys and ("SYMBOL", "ETH-PERP") in keys
    assert ("DELAY_MODEL", "FIXED_SECONDS") in keys


# --------------------------------------------------------------- drawdown


def test_drawdown_in_r_and_on_the_virtual_account() -> None:
    curve = equity_curve([1.0, -0.5, -1.5, 0.4, 2.0])
    result = compute_drawdown(curve)
    assert result is not None
    assert result.basis == "R"
    assert result.max_drawdown == pytest.approx(-2.0)
    assert result.max_drawdown_pct is None  # no reference => no percentage claim
    assert result.recovered is True
    assert result.drawdown_duration_steps == 2

    virtual = compute_drawdown(
        equity_curve([100.0, -50.0, -150.0], starting_value=10_000.0),
        basis="VIRTUAL_ACCOUNT_PUSD",
        percentage_reference=10_000.0,
    )
    assert virtual is not None and virtual.max_drawdown_pct == pytest.approx(2.0)


def test_consecutive_losses_and_empty_curves() -> None:
    assert max_consecutive_losses([1, -1, -1, 2, -1, -1, -1]) == 3
    assert compute_drawdown([1.0]) is None


# -------------------------------------------------------------- bootstrap


def test_bootstrap_is_seeded_and_gated_by_sample_size() -> None:
    values = [0.5, -1.0, 2.0, 1.1, -1.0, 0.7, 1.4, -0.9, 0.3, 2.2]
    first = bootstrap_mean(values, resamples=250, seed=42, min_sample=5)
    second = bootstrap_mean(values, resamples=250, seed=42, min_sample=5)
    assert first is not None and first == second
    assert first.lower_bound <= first.observed <= first.upper_bound
    assert first.seed == 42 and first.sample_size == 10
    assert first.note == UNCERTAINTY_NOTE
    assert "keine Prognose" in first.note

    other_seed = bootstrap_mean(values, resamples=250, seed=7, min_sample=5)
    assert other_seed is not None and other_seed.seed == 7
    assert bootstrap_mean(values, resamples=250, seed=42, min_sample=50) is None


def test_metrics_include_bootstrap_only_above_the_minimum() -> None:
    results = [make_result(value) for value in (1.0, -1.0, 2.0, 0.5, -0.4, 1.2)]
    with_bootstrap = compute_metrics(
        results,
        min_complete_simulations=5,
        metrics_version="smv-1",
        disclaimer_version="sdv-1",
        bootstrap_resamples=100,
        bootstrap_seed=11,
    )
    assert with_bootstrap.value_of("bootstrap_net_r_lower") is not None
    assert with_bootstrap.value_of("bootstrap_net_r_upper") is not None

    too_small = compute_metrics(
        results[:2],
        min_complete_simulations=20,
        metrics_version="smv-1",
        disclaimer_version="sdv-1",
        bootstrap_resamples=100,
        bootstrap_seed=11,
    )
    assert too_small.value_of("bootstrap_net_r_lower") is None


# -------------------------------------------------------------- reporting


def test_reports_carry_both_disclaimers_and_no_success_wording() -> None:
    results = [make_result(1.0), make_result(-1.0)]
    metrics = compute_metrics(
        results,
        min_complete_simulations=1,
        metrics_version="smv-1",
        disclaimer_version="sdv-1",
    )
    report = build_report(
        run_payload={"run": "test", "status": "COMPLETED"},
        manifest={"run_type": "BACKTEST"},
        simulations=results,
        metric_sets=[metrics],
        data_quality_warnings=["COMPLETED_WITH_GAPS"],
    )
    assert report["disclaimers"] == [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER]
    text = report_to_json(report)
    assert contains_forbidden_language(text) == ()
    assert "Hypothetische Simulation" in text
    for row in report["simulations"]:
        assert row["disclaimer"] == SIMULATION_DISCLAIMER
        assert "modelled_entry_price" in row and "fill" not in str(row).lower()

    csv_text = simulations_to_csv(results)
    assert csv_text.startswith(f"# {SIMULATION_DISCLAIMER}")
    assert BACKTEST_DISCLAIMER in csv_text
    assert contains_forbidden_language(csv_text) == ()


def test_simulation_row_and_metric_view_wording() -> None:
    result = make_result(1.0)
    row = simulation_row(result)
    assert row["research_direction"].endswith("(Research Context)")
    assert set(row) >= {
        "modelled_entry_price",
        "modelled_exit_price",
        "modelled_net_r",
        "modelled_fees",
        "modelled_slippage",
        "modelled_funding",
        "data_completeness",
        "model_warnings",
    }
    metrics = compute_metrics(
        [result],
        min_complete_simulations=1,
        metrics_version="smv-1",
        disclaimer_version="sdv-1",
    )
    view = metric_set_view(metrics)
    assert view["disclaimer"] == SIMULATION_DISCLAIMER
    assert view["sample_status"] in {"SUFFICIENT", "INSUFFICIENT_SAMPLE"}
    assert contains_forbidden_language(str(view)) == ()


def test_no_future_projection_language_anywhere() -> None:
    """Reports describe the sample, never a prediction."""
    result = make_result(1.0)
    metrics = compute_metrics(
        [result],
        min_complete_simulations=1,
        metrics_version="smv-1",
        disclaimer_version="sdv-1",
    )
    text = report_to_json(
        build_report(run_payload={}, manifest={}, simulations=[result], metric_sets=[metrics])
    ).lower()
    for phrase in ("prognose", "forecast", "expected profit", "wird erzielen"):
        assert phrase not in text
