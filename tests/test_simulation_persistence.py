"""Phase-11 persistence, jobs and API surface.

sqlite-backed; manifests are immutable, completed runs are never
overwritten, DB outages degrade instead of reporting success, and every
user-visible payload carries the mandatory disclaimers.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from tests.simulation_helpers import (
    COST_SETTINGS,
    FEE_SCHEDULE,
    LIFECYCLE_SETTINGS,
    NOW,
    REPORTING_SETTINGS,
    RISK_SETTINGS,
    STRATEGY_SETTINGS,
    backtest_settings,
    lifecycle_replay_scenario,
    seeded_plan,
    shadow_settings,
    simulation_inputs,
)
from tests.test_health_status import make_ctx

from app.main import create_app
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
from app.simulation.data_replay import FixtureDataProvider
from app.simulation.enums import (
    BacktestRunStatus,
    ExperimentStatus,
    RunType,
    SimulationEventType,
    SimulationRejectionCode,
)
from app.simulation.experiment import new_experiment
from app.simulation.experiment_manifest import ManifestError, build_manifest
from app.simulation.explainability import BACKTEST_DISCLAIMER, SIMULATION_DISCLAIMER
from app.simulation.metrics import compute_metrics
from app.simulation.simulation_engine import simulate
from app.simulation.simulation_rejection import build_run_rejection
from app.simulation.version import run_dedupe_key, shadow_configuration_hash
from app.simulation.walk_forward import (
    ConfigurationCandidate,
    SegmentEvidence,
    build_splits,
    select_configuration,
)

SHADOW = shadow_settings()
CONFIG_HASH = shadow_configuration_hash(SHADOW)


def make_manifest(experiment_id: uuid.UUID, *, start=NOW, end=None, seed=7):
    return build_manifest(
        experiment_id=experiment_id,
        run_type=RunType.BACKTEST,
        shadow_settings=SHADOW,
        backtest_settings=backtest_settings(),
        strategy_settings=STRATEGY_SETTINGS,
        risk_settings=RISK_SETTINGS,
        cost_settings=COST_SETTINGS,
        lifecycle_settings=LIFECYCLE_SETTINGS,
        reporting_settings=REPORTING_SETTINGS,
        fee_schedule_version=FEE_SCHEDULE.version,
        input_data={"source": "fixture", "data_version": "fixture-1"},
        universe=("BTC-PERP",),
        asset_classes=("CRYPTO",),
        timeframes=("5m",),
        start_at=start,
        end_at=end or NOW + timedelta(hours=1),
        created_at=NOW,
        random_seed=seed,
    )


def make_result(net_r: float = 1.0):
    outcome = simulate(simulation_inputs(), SHADOW, COST_SETTINGS, config_hash=CONFIG_HASH)
    assert outcome.result is not None
    return dataclasses.replace(outcome.result, net_r=net_r)


# ---------------------------------------------------------- manifests


def test_manifest_pins_every_version_and_hash() -> None:
    manifest = make_manifest(uuid.uuid4())
    payload = manifest.payload
    required = {
        "run_type",
        "simulation_model_version",
        "simulation_configuration_hash",
        "strategy_version",
        "strategy_config_hash",
        "risk_model_version",
        "risk_config_hash",
        "cost_model_version",
        "cost_config_hash",
        "lifecycle_model_version",
        "lifecycle_config_hash",
        "fee_schedule_version",
        "execution_assumption_version",
        "input_data",
        "universe",
        "asset_classes",
        "timeframes",
        "start_at",
        "end_at",
        "created_by",
        "random_seed",
        "replay_ordering",
        "replay_clock_version",
        "metrics_version",
        "disclaimer_version",
        "delay_model",
        "parameters",
    }
    assert required <= set(payload)
    assert payload["replay_ordering"]["ordering_version"] == "rov-1"
    assert manifest.content_hash and len(manifest.content_hash) == 32


def test_manifest_is_immutable_and_change_creates_a_new_one() -> None:
    manifest = make_manifest(uuid.uuid4())
    derived = manifest.with_field("parameters", {"delay": 30})
    assert derived.manifest_id != manifest.manifest_id
    assert derived.content_hash != manifest.content_hash
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.content_hash = "tampered"  # type: ignore[misc]


def test_incomplete_manifest_is_rejected() -> None:
    manifest = make_manifest(uuid.uuid4())
    broken = dataclasses.replace(
        manifest,
        payload={key: value for key, value in manifest.payload.items() if key != "universe"},
    )
    with pytest.raises(ManifestError):
        broken.require_complete()


def test_configuration_change_changes_the_hash() -> None:
    first = make_manifest(uuid.uuid4())
    import app.simulation.experiment_manifest as module

    second = module.build_manifest(
        experiment_id=first.experiment_id,
        run_type=RunType.BACKTEST,
        shadow_settings=shadow_settings(delay_fixed_seconds=45.0),
        backtest_settings=backtest_settings(),
        strategy_settings=STRATEGY_SETTINGS,
        risk_settings=RISK_SETTINGS,
        cost_settings=COST_SETTINGS,
        lifecycle_settings=LIFECYCLE_SETTINGS,
        reporting_settings=REPORTING_SETTINGS,
        fee_schedule_version=FEE_SCHEDULE.version,
        input_data={"source": "fixture", "data_version": "fixture-1"},
        universe=("BTC-PERP",),
        asset_classes=("CRYPTO",),
        timeframes=("5m",),
        start_at=NOW,
        end_at=NOW + timedelta(hours=1),
        created_at=NOW,
    )
    assert second.content_hash != first.content_hash


# --------------------------------------------------------- persistence


async def test_experiment_and_manifest_persistence(session_factory) -> None:
    repo = ExperimentRepository(session_factory)
    experiment = new_experiment(
        name="delay sensitivity", description="hypothetical only", created_at=NOW
    )
    assert await repo.create_experiment(experiment)
    assert not await repo.create_experiment(experiment)  # idempotent

    manifest = make_manifest(experiment.experiment_id)
    assert await repo.add_manifest(manifest)
    assert not await repo.add_manifest(manifest)  # identical content hash
    stored = await repo.manifest_by_hash(manifest.content_hash)
    assert stored is not None and stored.payload["universe"] == ["BTC-PERP"]
    assert stored.random_seed == 7

    assert await repo.set_status(experiment.experiment_id, ExperimentStatus.COMPLETED.value)
    row = await repo.get(experiment.experiment_id)
    assert row is not None and row.status == ExperimentStatus.COMPLETED.value
    assert await repo.counts_by_status() == {ExperimentStatus.COMPLETED.value: 1}


async def test_run_dedupe_and_terminal_immutability(session_factory) -> None:
    experiments = ExperimentRepository(session_factory)
    runs = SimulationRunRepository(session_factory)
    experiment = new_experiment(name="run", description="", created_at=NOW)
    await experiments.create_experiment(experiment)
    manifest = make_manifest(experiment.experiment_id)
    await experiments.add_manifest(manifest)
    dedupe = run_dedupe_key(
        experiment_id=str(experiment.experiment_id),
        run_type=RunType.BACKTEST.value,
        manifest_hash_value=manifest.content_hash,
    )
    run_id = uuid.uuid4()
    assert await runs.create_run(
        run_id=run_id,
        experiment_id=experiment.experiment_id,
        manifest_id=manifest.manifest_id,
        run_type=RunType.BACKTEST,
        status=BacktestRunStatus.RUNNING.value,
        dedupe_key=dedupe,
        start_at=NOW,
        end_at=NOW + timedelta(hours=1),
    )
    # a parallel run of the same manifest context is blocked
    assert not await runs.create_run(
        run_id=uuid.uuid4(),
        experiment_id=experiment.experiment_id,
        manifest_id=manifest.manifest_id,
        run_type=RunType.BACKTEST,
        status=BacktestRunStatus.RUNNING.value,
        dedupe_key=dedupe,
        start_at=NOW,
        end_at=NOW + timedelta(hours=1),
    )
    assert await runs.update_progress(
        run_id, status=BacktestRunStatus.COMPLETED.value, events_replayed=10, completed=True
    )
    # a completed run is history and never mutated again
    assert not await runs.update_progress(run_id, status=BacktestRunStatus.RUNNING.value)
    row = await runs.get(run_id)
    assert row is not None and row.run_status == BacktestRunStatus.COMPLETED.value
    assert row.events_replayed == 10
    assert await runs.counts_by_status(RunType.BACKTEST) == {BacktestRunStatus.COMPLETED.value: 1}
    assert (await runs.last_completed(RunType.BACKTEST)).id == run_id


async def test_position_execution_event_and_rejection_persistence(session_factory) -> None:
    positions = SimulatedPositionRepository(session_factory)
    executions = SimulatedExecutionRepository(session_factory)
    events = SimulationEventRepository(session_factory)
    rejections = SimulationRejectionRepository(session_factory)
    result = make_result()

    assert await positions.add_position(result, disclaimer_version="sdv-1")
    assert not await positions.add_position(result, disclaimer_version="sdv-1")  # dedupe
    stored = await positions.get(result.simulation_position_id)
    assert stored is not None
    assert stored.payload["disclaimer"] == SIMULATION_DISCLAIMER
    assert float(stored.modelled_entry_price) > 0
    assert await positions.exists_for_lifecycle(result.run_id, result.lifecycle_signal_id)

    written = await executions.add_executions(
        run_id=result.run_id,
        simulated_position_id=result.simulation_position_id,
        executions=(result.entry, result.partial_reduction, result.exit_execution),
    )
    assert written == 2  # entry + exit, partial disabled by default
    legs = await executions.executions_for(result.simulation_position_id)
    assert {row.leg for row in legs} == {"MODELLED_ENTRY", "MODELLED_EXIT"}
    assert {row.side_consumed for row in legs} == {"ask", "bid"}

    assert await events.add_event(
        run_id=result.run_id,
        event_type=SimulationEventType.SIMULATION_COMPLETED,
        as_of=NOW,
        simulated_position_id=result.simulation_position_id,
    )
    assert not await events.add_event(  # idempotent retry
        run_id=result.run_id,
        event_type=SimulationEventType.SIMULATION_COMPLETED,
        as_of=NOW,
        simulated_position_id=result.simulation_position_id,
    )
    assert len(await events.events_for(result.run_id)) == 1

    rejection = build_run_rejection(
        run_id=result.run_id,
        symbol="BTC-PERP",
        code=SimulationRejectionCode.ENTRY_BOOK_STALE,
        detail="stale",
        simulation_model_version="1.0.0",
        simulation_config_hash=CONFIG_HASH,
        as_of=NOW,
    )
    assert await rejections.record_rejection(rejection, 900)
    assert not await rejections.record_rejection(rejection, 900)  # aggregated
    rows = await rejections.recent()
    assert len(rows) == 1 and rows[0].count == 2
    assert await rejections.counts_by_code() == {"ENTRY_BOOK_STALE": 2}


async def test_metric_persistence_and_walk_forward_splits(session_factory) -> None:
    experiments = ExperimentRepository(session_factory)
    runs = SimulationRunRepository(session_factory)
    backtests = BacktestRunRepository(session_factory)
    metrics_repo = PerformanceMetricRepository(session_factory)

    experiment = new_experiment(name="wf", description="", created_at=NOW)
    await experiments.create_experiment(experiment)
    manifest = make_manifest(experiment.experiment_id)
    await experiments.add_manifest(manifest)
    run_id = uuid.uuid4()
    await runs.create_run(
        run_id=run_id,
        experiment_id=experiment.experiment_id,
        manifest_id=manifest.manifest_id,
        run_type=RunType.BACKTEST,
        status=BacktestRunStatus.RUNNING.value,
        dedupe_key="dedupe-metrics",
        start_at=NOW,
        end_at=NOW + timedelta(days=60),
    )
    assert await backtests.create(
        run_id=run_id,
        status=BacktestRunStatus.RUNNING.value,
        ordering_version="rov-1",
        clock_version="rcv-1",
    )

    metrics = compute_metrics(
        [make_result(1.0)],
        min_complete_simulations=20,
        metrics_version="smv-1",
        disclaimer_version="sdv-1",
    )
    assert await metrics_repo.add_metric_set(run_id, metrics)
    assert not await metrics_repo.add_metric_set(run_id, metrics)  # stable key
    sets = await metrics_repo.sets_for_run(run_id)
    assert len(sets) == 1 and sets[0].sample_status == "INSUFFICIENT_SAMPLE"
    values = await metrics_repo.values_for_set(sets[0].id)
    assert any(item.name == "hypothetical_win_rate" for item in values)
    assert await metrics_repo.insufficient_sample_count() == 1

    splits = build_splits(
        start_at=NOW,
        end_at=NOW + timedelta(days=60),
        settings=backtest_settings(
            walk_forward_train_days=20,
            walk_forward_validation_days=5,
            walk_forward_test_days=5,
            walk_forward_step_days=10,
        ),
    )
    selection = select_configuration(
        (
            SegmentEvidence(
                configuration_key="a",
                window=split_window,
                complete_simulations=30,
                incomplete_simulations=0,
                rejected_simulations=0,
                average_net_r=0.4,
                median_net_r=0.4,
                max_drawdown_r=-1.0,
                cost_share_pct=10.0,
                data_completeness_rate=1.0,
            )
            for split_window in (
                __import__(
                    "app.simulation.enums", fromlist=["WalkForwardWindow"]
                ).WalkForwardWindow.TRAIN,
            )
        ),
        min_simulations=20,
        candidates=(ConfigurationCandidate("a", {}),),
    )
    assert await backtests.add_split(
        run_id=run_id, split=splits[0], selection=selection, candidates=("a",)
    )
    assert not await backtests.add_split(
        run_id=run_id, split=splits[0], selection=selection, candidates=("a",)
    )
    stored_splits = await backtests.splits_for(run_id)
    assert len(stored_splits) == 1
    assert stored_splits[0].selected_configuration == "a"
    assert "never by result alone" in stored_splits[0].selection_rationale


async def test_checkpoint_and_coverage_persistence(session_factory) -> None:
    from app.simulation.backtest_data_validator import validate_coverage

    experiments = ExperimentRepository(session_factory)
    runs = SimulationRunRepository(session_factory)
    backtests = BacktestRunRepository(session_factory)
    experiment = new_experiment(name="cp", description="", created_at=NOW)
    await experiments.create_experiment(experiment)
    manifest = make_manifest(experiment.experiment_id)
    await experiments.add_manifest(manifest)
    run_id = uuid.uuid4()
    await runs.create_run(
        run_id=run_id,
        experiment_id=experiment.experiment_id,
        manifest_id=manifest.manifest_id,
        run_type=RunType.BACKTEST,
        status=BacktestRunStatus.VALIDATING.value,
        dedupe_key="dedupe-cp",
        start_at=NOW,
        end_at=NOW + timedelta(hours=1),
    )
    await backtests.create(
        run_id=run_id,
        status=BacktestRunStatus.VALIDATING.value,
        ordering_version="rov-1",
        clock_version="rcv-1",
    )
    events, start, end = lifecycle_replay_scenario(start_at=NOW, with_book=False)
    report = validate_coverage(events, backtest_settings(), start_at=start, end_at=end)
    await backtests.record_coverage(run_id, report)
    row = await backtests.get(run_id)
    assert row is not None and row.data_complete is False
    assert "orderbook" in row.missing_channels["channels"]

    await backtests.save_checkpoint(
        run_id,
        checkpoint={"events_replayed": 25},
        events_replayed=25,
        last_event_at=NOW,
        status=BacktestRunStatus.RUNNING.value,
    )
    row = await backtests.get(run_id)
    assert row is not None and row.events_replayed == 25
    assert row.checkpoint["events_replayed"] == 25


# ------------------------------------------------------------------ jobs


async def test_backtest_runner_full_run_and_gap_rejection(session_factory) -> None:
    from app.jobs.backtest_runner import BacktestRunner

    def make_runner(settings=None):
        return BacktestRunner(
            settings or backtest_settings(),
            SHADOW,
            STRATEGY_SETTINGS,
            RISK_SETTINGS,
            COST_SETTINGS,
            LIFECYCLE_SETTINGS,
            REPORTING_SETTINGS,
            ExperimentRepository(session_factory),
            SimulationRunRepository(session_factory),
            BacktestRunRepository(session_factory),
            SimulatedPositionRepository(session_factory),
            SimulatedExecutionRepository(session_factory),
            SimulationEventRepository(session_factory),
            SimulationRejectionRepository(session_factory),
            PerformanceMetricRepository(session_factory),
        )

    experiments = ExperimentRepository(session_factory)
    experiment = new_experiment(name="runner", description="", created_at=NOW)
    await experiments.create_experiment(experiment)
    events, start, end = lifecycle_replay_scenario(start_at=NOW)
    manifest = make_manifest(experiment.experiment_id, start=start, end=end)

    runner = make_runner()
    summary = await runner.run_backtest(
        manifest=manifest,
        provider=FixtureDataProvider(events),
        fee_schedule=FEE_SCHEDULE,
        seed_plans=(seeded_plan(as_of=NOW),),
    )
    assert summary["status"] in (
        BacktestRunStatus.COMPLETED.value,
        BacktestRunStatus.COMPLETED_WITH_GAPS.value,
    )
    assert summary["simulations"] == 1
    assert summary["simulations_completed"] == 1
    assert summary["sample_status"] == "INSUFFICIENT_SAMPLE"  # one simulation only
    assert summary["disclaimers"] == [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER]
    positions = await SimulatedPositionRepository(session_factory).count()
    assert positions == 1
    metric_sets = await PerformanceMetricRepository(session_factory).sets_for_run(
        uuid.UUID(summary["run_id"])
    )
    assert len(metric_sets) > 1  # overall + segments

    # missing order book data => the run never starts
    gap_events, gap_start, gap_end = lifecycle_replay_scenario(
        start_at=NOW + timedelta(days=1), with_book=False
    )
    gap_manifest = make_manifest(experiment.experiment_id, start=gap_start, end=gap_end, seed=9)
    gap_summary = await make_runner().run_backtest(
        manifest=gap_manifest,
        provider=FixtureDataProvider(gap_events),
        fee_schedule=FEE_SCHEDULE,
        seed_plans=(seeded_plan(as_of=NOW + timedelta(days=1)),),
    )
    assert gap_summary["status"] == BacktestRunStatus.REJECTED.value
    assert "orderbook" in gap_summary["detail"]


async def test_backtest_runner_refuses_without_manifest_or_when_disabled(
    session_factory,
) -> None:
    from app.jobs.backtest_runner import BacktestRunner

    runner = BacktestRunner(
        backtest_settings(enabled=False),
        SHADOW,
        STRATEGY_SETTINGS,
        RISK_SETTINGS,
        COST_SETTINGS,
        LIFECYCLE_SETTINGS,
        REPORTING_SETTINGS,
        ExperimentRepository(session_factory),
        SimulationRunRepository(session_factory),
        BacktestRunRepository(session_factory),
        SimulatedPositionRepository(session_factory),
        SimulatedExecutionRepository(session_factory),
        SimulationEventRepository(session_factory),
        SimulationRejectionRepository(session_factory),
        PerformanceMetricRepository(session_factory),
    )
    summary = await runner.run_backtest(
        manifest=make_manifest(uuid.uuid4()),
        provider=FixtureDataProvider([]),
        fee_schedule=FEE_SCHEDULE,
    )
    assert summary["status"] == BacktestRunStatus.REJECTED.value
    assert summary["code"] == SimulationRejectionCode.SIMULATION_DISABLED.value
    assert runner.state.value == "DISABLED"


# ------------------------------------------------------------------- API


def _fake_shadow():
    class FakeShadow:
        async def health_stats(self):
            return {"state": "HEALTHY", "queued": 0, "note": SIMULATION_DISCLAIMER}

        async def status_stats(self):
            return {
                "state": "HEALTHY",
                "delay_model": "FIXED_SECONDS",
                "disclaimer": SIMULATION_DISCLAIMER,
            }

        async def dashboard_details(self):
            return {"disclaimer": SIMULATION_DISCLAIMER, "simulations": []}

    return FakeShadow()


def _fake_backtest():
    class FakeBacktest:
        async def health_stats(self):
            return {"state": "HEALTHY", "active_runs": 0, "note": SIMULATION_DISCLAIMER}

        async def status_stats(self):
            return {
                "state": "HEALTHY",
                "recent_runs": [],
                "disclaimers": [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER],
            }

        async def dashboard_details(self):
            return {
                "disclaimers": [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER],
                "runs": [],
            }

    return FakeBacktest()


def test_api_exposes_simulation_sections_with_disclaimers() -> None:
    ctx = make_ctx()
    ctx.shadow = _fake_shadow()
    ctx.backtest = _fake_backtest()
    app = create_app(context=ctx)
    with TestClient(app) as client:
        health = client.get("/health").json()
        status = client.get("/status").json()
        dashboard = client.get("/dashboard").json()

    assert health["components"]["shadow_simulation"]["state"] == "HEALTHY"
    assert health["components"]["backtest"]["state"] == "HEALTHY"
    assert status["shadow_simulation"]["disclaimer"] == SIMULATION_DISCLAIMER
    assert status["backtest"]["disclaimers"] == [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER]
    # the dashboard carries both disclaimers at the very top
    assert dashboard["simulation_disclaimers"] == [
        SIMULATION_DISCLAIMER,
        BACKTEST_DISCLAIMER,
    ]
    assert next(iter(dashboard)) == "simulation_disclaimers"
    text = str(dashboard).lower()
    for banned in ("profitabel", "garantiert", "echte performance", "wallet", "token"):
        assert banned not in text


def test_api_reports_disabled_simulation_subsystems() -> None:
    ctx = make_ctx()
    app = create_app(context=ctx)
    with TestClient(app) as client:
        health = client.get("/health").json()
        status = client.get("/status").json()
        dashboard = client.get("/dashboard").json()
    assert health["components"]["shadow_simulation"] == "disabled"
    assert health["components"]["backtest"] == "disabled"
    assert status["shadow_simulation"] == "disabled"
    assert status["backtest"] == "disabled"
    assert dashboard["simulation_disclaimers"] == []
