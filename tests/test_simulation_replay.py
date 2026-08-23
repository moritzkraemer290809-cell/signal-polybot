"""Causal replay: ordering, look-ahead protection, gaps, checkpoints.

Historical replay must be strictly causal and reproducible: no future
data, deterministic tie-breakers, explicit gap handling and resumable
checkpoints.  Fixtures only - no network access.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from tests.simulation_helpers import (
    COST_SETTINGS,
    FEE_SCHEDULE,
    LIFECYCLE_SETTINGS,
    NOW,
    RISK_SETTINGS,
    STRATEGY_SETTINGS,
    backtest_settings,
    lifecycle_replay_scenario,
    seeded_plan,
    shadow_settings,
)

from app.simulation.backtest_data_validator import validate_coverage
from app.simulation.backtest_engine import BacktestCheckpoint, BacktestEngine
from app.simulation.data_replay import (
    FixtureDataProvider,
    LocalFileDataProvider,
    ReplayEventSource,
    ReplayInputError,
)
from app.simulation.enums import (
    BacktestRunStatus,
    ReplayEventCategory,
    SimulationRejectionCode,
)
from app.simulation.event_ordering import (
    CATEGORY_PRIORITY,
    EventOrderingError,
    EventOrderingPolicy,
)
from app.simulation.models import ReplayEvent
from app.simulation.replay_clock import LookaheadError, ReplayClock
from app.simulation.version import shadow_configuration_hash

SHADOW = shadow_settings()
CONFIG_HASH = shadow_configuration_hash(SHADOW)


def make_engine(**overrides):
    defaults = dict(
        backtest_settings=backtest_settings(),
        strategy_settings=STRATEGY_SETTINGS,
        risk_settings=RISK_SETTINGS,
        cost_settings=COST_SETTINGS,
        lifecycle_settings=LIFECYCLE_SETTINGS,
        shadow_settings=SHADOW,
        fee_schedule=FEE_SCHEDULE,
        simulation_config_hash=CONFIG_HASH,
    )
    defaults.update(overrides)
    return BacktestEngine(**defaults)


def run_scenario(**kwargs):
    seed = kwargs.pop("seed_plans", (seeded_plan(as_of=NOW),))
    engine_kwargs = kwargs.pop("engine", {})
    events, start, end = lifecycle_replay_scenario(start_at=NOW, **kwargs)
    clock = ReplayClock(start, end)
    source = ReplayEventSource(FixtureDataProvider(events), clock)
    engine = make_engine(seed_plans=seed, **engine_kwargs)
    return engine, engine.run(source, clock, run_id=uuid.uuid4())


# ------------------------------------------------------------- ordering


def test_ordering_priorities_follow_the_documented_sequence() -> None:
    policy = EventOrderingPolicy()
    order = [
        ReplayEventCategory.SESSION_CALENDAR,
        ReplayEventCategory.INSTRUMENT_STATUS,
        ReplayEventCategory.DATA_QUALITY_BOOK,
        ReplayEventCategory.TICKER_BBO_TRADES,
        ReplayEventCategory.CLOSED_CANDLE,
        ReplayEventCategory.STRATEGY_EVALUATION,
        ReplayEventCategory.RISK_PLAN_EVALUATION,
        ReplayEventCategory.LIFECYCLE_MONITORING,
        ReplayEventCategory.SIMULATION_EXECUTION,
    ]
    priorities = [CATEGORY_PRIORITY[item] for item in order]
    assert priorities == sorted(priorities)
    assert policy.document()["ordering_version"] == "rov-1"


def test_same_timestamp_tie_breakers_are_deterministic() -> None:
    policy = EventOrderingPolicy()
    moment = NOW
    events = [
        ReplayEvent(moment, ReplayEventCategory.CLOSED_CANDLE.value, "B", 2),
        ReplayEvent(moment, ReplayEventCategory.SESSION_CALENDAR.value, "B", 1),
        ReplayEvent(moment, ReplayEventCategory.CLOSED_CANDLE.value, "A", 9),
        ReplayEvent(moment, ReplayEventCategory.TICKER_BBO_TRADES.value, "A", 5),
    ]
    ordered = policy.sorted_events(events)
    assert [(item.category, item.symbol, item.sequence) for item in ordered] == [
        (ReplayEventCategory.SESSION_CALENDAR.value, "B", 1),
        (ReplayEventCategory.TICKER_BBO_TRADES.value, "A", 5),
        (ReplayEventCategory.CLOSED_CANDLE.value, "A", 9),
        (ReplayEventCategory.CLOSED_CANDLE.value, "B", 2),
    ]
    assert policy.sorted_events(list(reversed(events))) == ordered


def test_out_of_order_stream_is_rejected() -> None:
    policy = EventOrderingPolicy()
    events = [
        ReplayEvent(NOW + timedelta(minutes=5), ReplayEventCategory.CLOSED_CANDLE.value, "A", 1),
        ReplayEvent(NOW, ReplayEventCategory.CLOSED_CANDLE.value, "A", 2),
    ]
    with pytest.raises(EventOrderingError):
        list(policy.validate_stream(events))


def test_unknown_category_is_rejected() -> None:
    with pytest.raises(EventOrderingError):
        EventOrderingPolicy().priority_of("NOT_A_CATEGORY")


# ------------------------------------------------------- look-ahead guard


def test_clock_never_moves_backwards() -> None:
    clock = ReplayClock(NOW, NOW + timedelta(hours=1))
    clock.advance_to(NOW + timedelta(minutes=10))
    with pytest.raises(LookaheadError):
        clock.advance_to(NOW + timedelta(minutes=5))
    assert clock.lookahead_violations == 1


def test_future_data_is_never_observable() -> None:
    clock = ReplayClock(NOW, NOW + timedelta(hours=1))
    clock.advance_to(NOW + timedelta(minutes=10))
    clock.guard(NOW + timedelta(minutes=9), "past value")  # fine
    with pytest.raises(LookaheadError) as error:
        clock.guard(NOW + timedelta(minutes=11), "future candle")
    assert "look-ahead guard" in error.value.detail
    assert clock.visible(NOW + timedelta(minutes=9)) is True
    assert clock.visible(NOW + timedelta(minutes=11)) is False


def test_replay_stage_rejects_future_candles() -> None:
    """A candle stamped ahead of the clock triggers the guard, not a result."""
    events, start, end = lifecycle_replay_scenario(start_at=NOW)
    poisoned = list(events)
    poisoned.append(
        ReplayEvent(
            as_of=start + timedelta(minutes=5),
            category=ReplayEventCategory.CLOSED_CANDLE.value,
            symbol="BTC-PERP",
            sequence=9999,
            payload={
                "timeframe": "5m",
                # a candle that only CLOSES far in the future
                "open_time": (end + timedelta(hours=5)).isoformat(),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
            },
        )
    )
    clock = ReplayClock(start, end)
    source = ReplayEventSource(FixtureDataProvider(poisoned), clock)
    engine = make_engine(seed_plans=(seeded_plan(as_of=NOW),))
    result = engine.run(source, clock, run_id=uuid.uuid4())
    assert result.status is BacktestRunStatus.REJECTED
    assert any(
        rejection.primary_code is SimulationRejectionCode.LOOKAHEAD_GUARD_TRIGGERED
        for rejection in result.rejections
    )


def test_open_candles_are_never_used() -> None:
    """A candle is only observable AFTER its close time."""
    clock = ReplayClock(NOW, NOW + timedelta(hours=1))
    from app.simulation.backtest_context import ReplayState

    state = ReplayState(clock)
    clock.advance_to(NOW + timedelta(minutes=2))
    with pytest.raises(LookaheadError):
        state.apply(
            ReplayEventCategory.CLOSED_CANDLE.value,
            "BTC-PERP",
            NOW + timedelta(minutes=2),
            {
                "timeframe": "5m",
                "open_time": NOW.isoformat(),  # closes at NOW+5m, still open
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
            },
        )


# ------------------------------------------------------- data validation


def test_complete_coverage_passes() -> None:
    events, start, end = lifecycle_replay_scenario(start_at=NOW)
    report = validate_coverage(events, backtest_settings(), start_at=start, end_at=end)
    assert report.complete is True
    assert report.gaps == ()


def test_missing_channel_blocks_the_run() -> None:
    events, start, end = lifecycle_replay_scenario(start_at=NOW, with_book=False)
    report = validate_coverage(events, backtest_settings(), start_at=start, end_at=end)
    assert report.complete is False
    assert "orderbook" in report.missing_channels
    assert "BACKTEST_DATA_INCOMPLETE" in report.warnings


def test_missing_funding_blocks_when_required() -> None:
    events, start, end = lifecycle_replay_scenario(start_at=NOW, with_funding=False)
    report = validate_coverage(events, backtest_settings(), start_at=start, end_at=end)
    assert report.complete is False
    assert "funding" in report.missing_channels


def test_data_gap_rejects_or_segments_by_policy() -> None:
    events, start, end = lifecycle_replay_scenario(start_at=NOW)
    # drop the middle of the window to create a gap
    kept = [
        event
        for event in events
        if not (start + timedelta(minutes=10) < event.as_of < start + timedelta(minutes=30))
    ]
    strict = validate_coverage(
        kept,
        backtest_settings(max_data_gap_seconds=300, allow_segmented_data=False),
        start_at=start,
        end_at=end,
    )
    assert strict.complete is False and strict.excluded_intervals == ()
    assert "DATA_GAP" in strict.warnings

    segmented = validate_coverage(
        kept,
        backtest_settings(max_data_gap_seconds=300, allow_segmented_data=True),
        start_at=start,
        end_at=end,
    )
    assert segmented.complete is False
    assert segmented.excluded_intervals
    assert "COMPLETED_WITH_GAPS" in segmented.warnings
    assert "not a complete backtest" in segmented.detail


# ---------------------------------------------------------------- replay


def test_full_replay_models_a_hypothetical_simulation() -> None:
    _, result = run_scenario()
    assert result.status is BacktestRunStatus.COMPLETED
    assert result.events_replayed > 0
    assert result.lifecycles_started == 1
    assert len(result.simulations) == 1
    simulation = result.simulations[0]
    assert simulation.completed
    assert simulation.exit_reason is not None
    assert simulation.net_r is not None and simulation.net_r > 0
    assert simulation.entry.modelled_price > 0


def test_replay_reproduces_identical_results() -> None:
    plan = seeded_plan(as_of=NOW)
    _, first = run_scenario(seed_plans=(plan,))
    _, second = run_scenario(seed_plans=(plan,))
    assert first.events_replayed == second.events_replayed
    assert [item.net_r for item in first.simulations] == [item.net_r for item in second.simulations]


def test_segmented_replay_skips_excluded_intervals() -> None:
    from app.simulation.models import DataGapInterval

    excluded = (
        DataGapInterval(
            start_at=NOW + timedelta(minutes=10),
            end_at=NOW + timedelta(minutes=45),
            channel="orderbook",
            symbol="BTC-PERP",
            detail="test gap",
        ),
    )
    _, result = run_scenario(engine={"excluded_intervals": excluded})
    assert result.status is BacktestRunStatus.COMPLETED_WITH_GAPS
    assert "not a complete backtest" in result.detail
    assert result.excluded_intervals == excluded


def test_max_event_limit_truncates_with_a_warning() -> None:
    events, start, end = lifecycle_replay_scenario(start_at=NOW)
    clock = ReplayClock(start, end)
    source = ReplayEventSource(FixtureDataProvider(events), clock, max_events=5)
    engine = make_engine(seed_plans=(seeded_plan(as_of=NOW),))
    result = engine.run(source, clock, run_id=uuid.uuid4())
    assert source.truncated is True
    assert result.events_replayed == 5
    assert "MAX_EVENTS_PER_RUN_REACHED" in result.warnings


def test_cancellation_stops_the_replay() -> None:
    events, start, end = lifecycle_replay_scenario(start_at=NOW)
    clock = ReplayClock(start, end)
    source = ReplayEventSource(FixtureDataProvider(events), clock)
    engine = make_engine(seed_plans=(seeded_plan(as_of=NOW),))
    engine.cancel()
    result = engine.run(source, clock, run_id=uuid.uuid4())
    assert result.status is BacktestRunStatus.CANCELLED
    assert result.events_replayed == 0


def test_checkpoint_resume_never_applies_an_event_twice() -> None:
    events, start, end = lifecycle_replay_scenario(start_at=NOW)
    clock = ReplayClock(start, end)
    source = ReplayEventSource(FixtureDataProvider(events), clock)
    engine = make_engine(seed_plans=(seeded_plan(as_of=NOW),))
    run_id = uuid.uuid4()
    full = engine.run(source, clock, run_id=run_id)

    # resume from the midpoint: only the remaining events are replayed
    midpoint = events[len(events) // 2]
    checkpoint = BacktestCheckpoint(
        run_id=run_id,
        events_replayed=len(events) // 2,
        last_event_at=midpoint.as_of,
        last_event_sequence=midpoint.sequence,
        simulations_completed=0,
        rejections=0,
    )
    resume_clock = ReplayClock(start, end)
    resume_source = ReplayEventSource(FixtureDataProvider(events), resume_clock)
    resumed = make_engine(seed_plans=(seeded_plan(as_of=NOW),)).run(
        resume_source, resume_clock, run_id=run_id, resume_from=checkpoint
    )
    assert resumed.events_replayed > checkpoint.events_replayed
    assert resumed.events_replayed <= full.events_replayed


def test_checkpoints_are_emitted_during_the_run() -> None:
    events, start, end = lifecycle_replay_scenario(start_at=NOW)
    clock = ReplayClock(start, end)
    source = ReplayEventSource(FixtureDataProvider(events), clock)
    engine = make_engine(seed_plans=(seeded_plan(as_of=NOW),), checkpoint_interval=3)
    seen: list[BacktestCheckpoint] = []
    engine.run(source, clock, run_id=uuid.uuid4(), on_checkpoint=seen.append)
    assert len(seen) >= 2
    assert seen[0].events_replayed < seen[-1].events_replayed
    assert seen[-1].as_dict()["run_id"]


# ------------------------------------------------------- local providers


def test_local_file_provider_rejects_paths_outside_the_repository(tmp_path) -> None:
    from app.config import REPO_ROOT

    with pytest.raises(ReplayInputError):
        LocalFileDataProvider(REPO_ROOT, "data/backtest_input", "../../etc/passwd")
    with pytest.raises(ReplayInputError):
        LocalFileDataProvider(REPO_ROOT, "data/backtest_input", "missing.csv")


def test_local_csv_provider_reads_validated_rows(tmp_path) -> None:
    from app.config import REPO_ROOT

    directory = REPO_ROOT / "data" / "backtest_input"
    sample = directory / "_pytest_sample.csv"
    sample.write_text(
        "as_of,category,symbol,sequence,kind,best_bid,best_ask\n"
        f"{NOW.isoformat()},TICKER_BBO_TRADES,BTC-PERP,1,bbo,99.9,100.1\n"
        f"{NOW.isoformat()},TICKER_BBO_TRADES,BTC-PERP,1,bbo,99.9,100.1\n"
    )
    try:
        provider = LocalFileDataProvider(REPO_ROOT, "data/backtest_input", "_pytest_sample.csv")
        events = list(provider.events(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1)))
        assert len(events) == 1  # duplicate row dropped deterministically
        assert events[0].symbol == "BTC-PERP"
        assert provider.source_metadata["source"] == "local_file"
    finally:
        sample.unlink(missing_ok=True)


def test_local_csv_provider_validates_schema() -> None:
    from app.config import REPO_ROOT

    directory = REPO_ROOT / "data" / "backtest_input"
    sample = directory / "_pytest_bad.csv"
    sample.write_text("timestamp,value\n1,2\n")
    try:
        provider = LocalFileDataProvider(REPO_ROOT, "data/backtest_input", "_pytest_bad.csv")
        with pytest.raises(ReplayInputError):
            list(provider.events(NOW, NOW + timedelta(minutes=1)))
    finally:
        sample.unlink(missing_ok=True)


def test_replay_makes_no_network_calls(monkeypatch) -> None:
    """A replay must run without any socket access whatsoever."""
    import socket

    def blocked(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("replay attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    _, result = run_scenario()
    assert result.status is BacktestRunStatus.COMPLETED
