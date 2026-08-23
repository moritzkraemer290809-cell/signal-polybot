"""Shadow simulation monitor job: queue, dedupe, paused mode, outages.

The job consumes ONLY persisted internal lifecycles; it never reaches
Telegram, never places anything and never reports an unpersisted
simulation as completed.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta
from types import SimpleNamespace

from tests.simulation_helpers import (
    COST_SETTINGS,
    FEE_SCHEDULE,
    NOW,
    REPORTING_SETTINGS,
    market_reference,
    shadow_settings,
    simulation_inputs,
)

from app.jobs.shadow_simulation_monitor import ShadowSimulationMonitorJob
from app.repositories.performance_metric_repository import PerformanceMetricRepository
from app.repositories.simulated_execution_repository import SimulatedExecutionRepository
from app.repositories.simulated_position_repository import SimulatedPositionRepository
from app.repositories.simulation_event_repository import (
    SimulationEventRepository,
    SimulationRejectionRepository,
)
from app.repositories.simulation_run_repository import SimulationRunRepository
from app.simulation.enums import SimulationSubsystemState
from app.simulation.explainability import SIMULATION_DISCLAIMER
from app.simulation.shadow_event_consumer import ShadowEventQueue, ShadowWorkItem
from app.simulation.shadow_mode import is_simulatable


class StubBuilder:
    """Returns prepared simulation inputs (no repositories, no network)."""

    def __init__(self, inputs=None) -> None:
        self.inputs = inputs
        self.calls = 0

    async def build_inputs(self, row, **kwargs):
        self.calls += 1
        if self.inputs is None:
            return None
        # the real builder stamps the job's run id onto the inputs
        return dataclasses.replace(self.inputs, run_id=kwargs.get("run_id", self.inputs.run_id))

    def market_reference(self, instrument_id, as_of):
        return market_reference(100.0, as_of)


class StubLifecycles:
    def __init__(self, rows: list) -> None:
        self.rows = rows

    async def recent(self, limit: int = 20) -> list:
        return self.rows

    async def get(self, signal_id):
        return next((row for row in self.rows if row.id == signal_id), None)


class StubBotState:
    def __init__(self, paused: bool = False) -> None:
        self.paused = paused

    async def is_paused(self) -> bool:
        return self.paused


class StubFees:
    """No administered schedule row - the job keeps its last snapshot."""

    async def active_schedule(self):
        return None


def lifecycle_row(state: str = "TARGET_1_REACHED", *, entry_at=NOW):
    return SimpleNamespace(
        id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        state=state,
        state_version=5,
        entry_confirmed_at=entry_at,
        terminal_at=NOW + timedelta(hours=2),
        last_session_state="CRYPTO_24_7",
    )


def make_job(session_factory, *, rows, inputs=None, paused=False, settings=None):
    job = ShadowSimulationMonitorJob(
        settings or shadow_settings(),
        COST_SETTINGS,
        REPORTING_SETTINGS,
        StubBuilder(inputs),  # type: ignore[arg-type]
        StubLifecycles(rows),  # type: ignore[arg-type]
        SimulationRunRepository(session_factory),
        SimulatedPositionRepository(session_factory),
        SimulatedExecutionRepository(session_factory),
        SimulationEventRepository(session_factory),
        SimulationRejectionRepository(session_factory),
        PerformanceMetricRepository(session_factory),
        StubFees(),  # type: ignore[arg-type]
        StubBotState(paused),  # type: ignore[arg-type]
        now_fn=lambda: NOW,
    )
    # the fee snapshot comes from the stub schedule
    job._fee_snapshot = FEE_SCHEDULE
    return job


# ------------------------------------------------------------------ queue


def test_queue_is_bounded_and_coalesces_per_signal() -> None:
    queue = ShadowEventQueue(2)
    first, second, third = (uuid.uuid4() for _ in range(3))
    assert queue.offer(ShadowWorkItem(first, "ACTIVE_RESEARCH", 3, NOW))
    assert queue.offer(ShadowWorkItem(second, "ACTIVE_RESEARCH", 3, NOW))
    # same signal again -> coalesced, not duplicated
    assert queue.offer(ShadowWorkItem(first, "TARGET_1_REACHED", 4, NOW))
    assert len(queue) == 2 and queue.coalesced == 1
    # capacity reached -> oldest is dropped with a counter (backpressure)
    assert not queue.offer(ShadowWorkItem(third, "ACTIVE_RESEARCH", 3, NOW))
    assert queue.dropped == 1 and len(queue) == 2
    drained = queue.drain(5)
    assert len(drained) == 2 and len(queue) == 0


def test_only_lifecycles_with_confirmed_entry_are_simulatable() -> None:
    assert is_simulatable("ACTIVE_RESEARCH", NOW) is True
    assert is_simulatable("TARGET_1_REACHED", NOW) is True
    assert is_simulatable("INVALIDATED", NOW) is True
    assert is_simulatable("WATCHING_ENTRY", None) is False
    assert is_simulatable("ACTIVE_RESEARCH", None) is False


# ------------------------------------------------------------------- job


async def test_job_models_and_persists_one_simulation(session_factory) -> None:
    row = lifecycle_row()
    job = make_job(
        session_factory,
        rows=[row],
        inputs=simulation_inputs(lifecycle_signal_id=row.id),
    )
    summary = await job.run_once()
    assert summary["simulations_completed"] == 1
    assert summary["note"] == SIMULATION_DISCLAIMER
    assert await SimulatedPositionRepository(session_factory).count() == 1
    assert await SimulatedExecutionRepository(session_factory).count() == 2
    assert await SimulationEventRepository(session_factory).count() == 1
    assert not job.degraded


async def test_paused_mode_creates_no_simulation(session_factory) -> None:
    row = lifecycle_row()
    job = make_job(
        session_factory,
        rows=[row],
        inputs=simulation_inputs(lifecycle_signal_id=row.id),
        paused=True,
    )
    summary = await job.run_once()
    assert summary["paused"] is True
    assert summary["simulations_completed"] == 0
    assert await SimulatedPositionRepository(session_factory).count() == 0


async def test_disabled_mode_does_nothing(session_factory) -> None:
    row = lifecycle_row()
    job = make_job(
        session_factory,
        rows=[row],
        inputs=simulation_inputs(lifecycle_signal_id=row.id),
        settings=shadow_settings(mode_enabled=False),
    )
    summary = await job.run_once()
    assert summary["enabled"] is False
    assert job.state is SimulationSubsystemState.DISABLED
    assert await SimulatedPositionRepository(session_factory).count() == 0


async def test_duplicate_simulation_is_suppressed(session_factory) -> None:
    row = lifecycle_row()
    inputs = simulation_inputs(lifecycle_signal_id=row.id)
    job = make_job(session_factory, rows=[row], inputs=inputs)
    await job.run_once()
    # a second cycle inside the dedupe window must not create another row
    job._seen.clear()  # ignore the in-memory guard: the DB must hold the line
    await job.run_once()
    assert await SimulatedPositionRepository(session_factory).count() == 1


async def test_dedupe_window_skips_recently_seen_lifecycles(session_factory) -> None:
    row = lifecycle_row()
    job = make_job(
        session_factory, rows=[row], inputs=simulation_inputs(lifecycle_signal_id=row.id)
    )
    await job.run_once()
    builder_calls = job._builder.calls  # type: ignore[attr-defined]
    await job.run_once()
    assert job._builder.calls == builder_calls  # type: ignore[attr-defined]


async def test_rejection_is_persisted_and_counted(session_factory) -> None:
    row = lifecycle_row()
    job = make_job(
        session_factory,
        rows=[row],
        inputs=simulation_inputs(lifecycle_signal_id=row.id, funding=None),
    )
    summary = await job.run_once()
    assert summary["simulations_rejected"] == 1
    counts = await SimulationRejectionRepository(session_factory).counts_by_code()
    assert counts == {"FUNDING_UNAVAILABLE": 1}
    assert await SimulatedPositionRepository(session_factory).count() == 0


async def test_db_outage_degrades_without_reporting_success(session_factory, monkeypatch) -> None:
    row = lifecycle_row()
    job = make_job(
        session_factory, rows=[row], inputs=simulation_inputs(lifecycle_signal_id=row.id)
    )

    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(job._positions, "add_position", broken)
    summary = await job.run_once()
    assert summary["simulations_completed"] == 0
    assert job.degraded
    assert job.last_success_at is None
    assert await SimulatedPositionRepository(session_factory).count() == 0


async def test_lifecycle_load_failure_degrades(session_factory, monkeypatch) -> None:
    job = make_job(session_factory, rows=[], inputs=None)

    async def broken(limit: int = 20):
        raise RuntimeError("db down")

    monkeypatch.setattr(job._lifecycles, "recent", broken)
    await job.run_once()
    assert job.degraded


async def test_open_lifecycles_are_not_persisted(session_factory) -> None:
    row = lifecycle_row(state="ACTIVE_RESEARCH")
    job = make_job(
        session_factory,
        rows=[row],
        inputs=simulation_inputs(
            lifecycle_signal_id=row.id, exit_observation=None, exit_reason=None
        ),
    )
    summary = await job.run_once()
    assert summary["simulations_completed"] == 0
    assert await SimulatedPositionRepository(session_factory).count() == 0


async def test_reports_carry_the_disclaimer_and_no_forbidden_wording(
    session_factory,
) -> None:
    row = lifecycle_row()
    job = make_job(
        session_factory, rows=[row], inputs=simulation_inputs(lifecycle_signal_id=row.id)
    )
    await job.run_once()
    health = await job.health_stats()
    status = await job.status_stats()
    dashboard = await job.dashboard_details()
    assert health["note"] == SIMULATION_DISCLAIMER
    assert status["disclaimer"] == SIMULATION_DISCLAIMER
    assert dashboard is not None and dashboard["disclaimer"] == SIMULATION_DISCLAIMER
    assert dashboard["simulations"][0]["modelled_entry_price"] > 0

    from app.simulation.explainability import contains_forbidden_language

    assert contains_forbidden_language(str(status)) == ()
    assert contains_forbidden_language(str(dashboard)) == ()


async def test_job_start_stop_and_state(session_factory) -> None:
    job = make_job(session_factory, rows=[], inputs=None)
    assert job.state is SimulationSubsystemState.UNAVAILABLE
    await job.start()
    try:
        assert job.alive
        assert job.state is SimulationSubsystemState.HEALTHY
    finally:
        await job.stop()
    assert not job.alive
