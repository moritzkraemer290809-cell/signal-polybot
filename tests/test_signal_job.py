"""Signal lifecycle monitor job: admission, entry confirmation, terminal
monitoring, paused mode, leases, degradation and the API surface.

Persistence runs against sqlite with REAL phase-8 candidates and phase-9
plans; market data arrives through deterministic stubs.  No network, no
Telegram, no account data.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from tests.risk_helpers import make_instrument
from tests.signal_helpers import SIGNAL_SETTINGS, make_signal_settings
from tests.test_health_status import make_ctx
from tests.test_risk_persistence_job import (
    _eligible_plan,
    _seed_confirmed_candidate,
    _seed_instrument,
)

from app.domain.enums import FreshnessStatus, InstrumentQualityStatus, Timeframe
from app.domain.models import CandleData
from app.jobs.signal_lifecycle_monitor import SignalLifecycleMonitorJob
from app.main import create_app
from app.repositories.candle_repository import CandleRepository
from app.repositories.feature_repository import FeatureRepository
from app.repositories.instrument_risk_repository import InstrumentRiskRepository
from app.repositories.orm import SetupCandidateRecord
from app.repositories.risk_plan_repository import RiskPlanRepository
from app.repositories.setup_candidate_repository import SetupCandidateRepository
from app.repositories.signal_event_repository import SignalEventRepository
from app.repositories.signal_lifecycle_repository import SignalLifecycleRepository
from app.repositories.signal_rejection_repository import SignalRejectionRepository
from app.repositories.signal_update_repository import SignalUpdateRepository
from app.risk.enums import PlanStatus
from app.sessions.models import CryptoSessionSnapshot, CryptoSessionState
from app.signals.explainability import DISCLAIMER
from app.signals.lifecycle_context import SignalLifecycleContextBuilder

JOB_NOW = datetime(2026, 8, 22, 12, 1, tzinfo=UTC)


class StubMarketData:
    def __init__(self) -> None:
        self.tracker = SimpleNamespace(last_mark_price=None, last_bbo=None)

    def tracker_for(self, instrument_id: int):
        return self.tracker

    def set_prices(self, mark: float, bid: float, ask: float, at: datetime) -> None:
        self.tracker.last_mark_price = mark
        self.tracker.last_bbo = SimpleNamespace(
            bid_price=bid, ask_price=ask, ts_ms=int(at.timestamp() * 1000)
        )


class StubDataQuality:
    def __init__(self) -> None:
        self.status = InstrumentQualityStatus.HEALTHY
        self.freshness = FreshnessStatus.FRESH

    def evaluate(self, instrument_id: int):
        return SimpleNamespace(status=self.status)

    def channel_freshness(self, instrument_id: int, channel):
        return self.freshness


class StubBooks:
    def __init__(self) -> None:
        self.state = SimpleNamespace(needs_resync=False, reliable=True)

    def book(self, instrument_id: int):
        return self.state


class StubSelection:
    def __init__(self, rows: list) -> None:
        self.rows = rows
        self.last_equity_snapshot = SimpleNamespace()  # unused for CRYPTO
        self.last_crypto_snapshot = CryptoSessionSnapshot(
            state=CryptoSessionState.CRYPTO_24_7,
            evaluated_at=JOB_NOW,
            analysis_allowed=True,
        )

    async def active_watchlist_rows(self) -> list:
        return self.rows


class StubBotState:
    def __init__(self, paused: bool = False) -> None:
        self.paused = paused

    async def is_paused(self) -> bool:
        return self.paused


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _seed_candidate(session_factory, pk: int):
    """Real CONFIRMED phase-8 candidate, freshness pinned to the job clock."""
    candidate = await _seed_confirmed_candidate(session_factory, pk)
    async with session_factory() as session:
        row = await session.get(SetupCandidateRecord, candidate.candidate_id)
        assert row is not None
        row.as_of = JOB_NOW - timedelta(minutes=5)
        row.expiry_at = JOB_NOW + timedelta(hours=1)
        await session.commit()
    return candidate


async def _seed_plan(session_factory, pk: int, candidate_id, *, dedupe_key: str | None = None):
    """Real ELIGIBLE phase-9 plan (zone [99.5, 99.6], invalidation 96.45)."""
    plan, _ = _eligible_plan(candidate_id, pk)
    plan = dataclasses.replace(plan, instrument_pk=pk)
    if dedupe_key is not None:
        plan = dataclasses.replace(plan, dedupe_key=dedupe_key)
    assert await RiskPlanRepository(session_factory).insert_new(plan)
    return plan


async def _seed_risk_snapshot(session_factory, pk: int) -> None:
    await InstrumentRiskRepository(session_factory).add_snapshot(
        uuid.uuid4(),
        make_instrument(instrument_pk=pk, as_of=JOB_NOW - timedelta(seconds=60)),
    )


async def _seed_closed_candle(
    session_factory, pk: int, *, close: float, low: float, high: float
) -> None:
    """One CLOSED 5m candle (11:55-12:00) plus the forming 12:00 candle."""
    repo = CandleRepository(session_factory)
    closed = CandleData(
        timeframe=Timeframe.M5,
        open_time=JOB_NOW.replace(minute=55, hour=11),
        open=Decimal(str(low + 0.01)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("10"),
        trade_count=5,
    )
    forming = CandleData(
        timeframe=Timeframe.M5,
        open_time=JOB_NOW.replace(minute=0),
        open=Decimal(str(close)),
        high=Decimal(str(close + 5)),  # a forming spike must never confirm
        low=Decimal(str(close - 5)),
        close=Decimal(str(close + 5)),
        volume=Decimal("2"),
        trade_count=1,
    )
    await repo.upsert_candles(pk, [closed, forming])


def _make_job(
    session_factory,
    pk: int,
    *,
    paused: bool = False,
    settings=SIGNAL_SETTINGS,
    watchlisted: bool = True,
):
    market_data = StubMarketData()
    data_quality = StubDataQuality()
    books = StubBooks()
    builder = SignalLifecycleContextBuilder(
        settings,
        RiskPlanRepository(session_factory),
        SetupCandidateRepository(session_factory),
        CandleRepository(session_factory),
        FeatureRepository(session_factory),
        InstrumentRiskRepository(session_factory),
        market_data,  # type: ignore[arg-type]
        data_quality,  # type: ignore[arg-type]
        books,  # type: ignore[arg-type]
    )
    rows = (
        [
            SimpleNamespace(
                instrument_pk=pk, symbol="BTC-PERP", asset_class="CRYPTO", quality_score=90
            )
        ]
        if watchlisted
        else []
    )
    clock = Clock(JOB_NOW)
    signals = SignalLifecycleRepository(session_factory)
    job = SignalLifecycleMonitorJob(
        settings,
        builder,
        signals,
        SignalEventRepository(session_factory),
        SignalUpdateRepository(session_factory),
        SignalRejectionRepository(session_factory),
        RiskPlanRepository(session_factory),
        StubSelection(rows),  # type: ignore[arg-type]
        StubBotState(paused),  # type: ignore[arg-type]
        now_fn=clock,
    )
    stubs = SimpleNamespace(
        market_data=market_data, data_quality=data_quality, books=books, clock=clock
    )
    # neutral fresh prices: above the zone, below targets, above invalidation
    market_data.set_prices(101.0, 100.9, 101.1, JOB_NOW)
    return job, signals, stubs


async def _admitted_signal(session_factory, pk: int, job, signals):
    summary = await job.run_once()
    assert summary["admitted"] == 1, summary
    rows = await signals.non_terminal()
    assert len(rows) == 1
    return rows[0]


# ------------------------------------------------------------------- admission


async def test_job_admits_eligible_plan_once(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, _ = _make_job(session_factory, pk)

    row = await _admitted_signal(session_factory, pk, job, signals)
    assert row.state == "WATCHING_ENTRY" and row.state_version == 2
    assert row.symbol == "BTC-PERP"
    assert float(row.entry_low) == 99.5 and float(row.invalidation_price) == 96.45
    events = await SignalEventRepository(session_factory).events_for(row.id)
    assert [event.event_type for event in events] == ["CREATED", "ADMITTED", "WATCHING_ENTRY"]

    # second cycle: the plan already has its lifecycle - idempotent
    summary = await job.run_once()
    assert summary["admitted"] == 0 and summary["admission_skipped"] == 1
    assert summary["signals_evaluated"] == 1
    assert not job.degraded


async def test_job_rejects_admission_without_watchlist(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, _ = _make_job(session_factory, pk, watchlisted=False)

    summary = await job.run_once()
    assert summary["admitted"] == 0 and summary["admission_rejected"] == 1
    assert await signals.non_terminal() == []
    rejections = await SignalRejectionRepository(session_factory).recent()
    assert rejections[0].primary_code == "WATCHLIST_NOT_ACTIVE"


async def test_job_rejects_admission_on_stale_instrument_snapshot(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    job, signals, _ = _make_job(session_factory, pk)  # no risk snapshot seeded

    summary = await job.run_once()
    assert summary["admission_rejected"] == 1
    counts = await SignalRejectionRepository(session_factory).counts_by_code()
    assert counts == {"INSTRUMENT_SNAPSHOT_STALE": 1}
    assert await signals.non_terminal() == []


async def test_paused_mode_admits_nothing_and_confirms_no_entry(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)

    job, signals, _ = _make_job(session_factory, pk)
    await _admitted_signal(session_factory, pk, job, signals)

    paused_job, _, paused_stubs = _make_job(session_factory, pk, paused=True)
    # confirmable market: closed candle reclaimed the anchor inside the zone
    await _seed_closed_candle(session_factory, pk, close=99.55, low=99.3, high=99.7)
    paused_stubs.market_data.set_prices(99.55, 99.52, 99.58, JOB_NOW)
    summary = await paused_job.run_once()
    assert summary["paused"] is True
    assert summary["admitted"] == 0  # no new admissions while paused
    rows = await signals.non_terminal()
    assert rows[0].state == "WATCHING_ENTRY"  # no entry confirmation while paused


# ------------------------------------------------------------------ monitoring


async def test_entry_confirmation_reaches_active_research(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, stubs = _make_job(session_factory, pk)
    row = await _admitted_signal(session_factory, pk, job, signals)

    await _seed_closed_candle(session_factory, pk, close=99.55, low=99.3, high=99.7)
    stubs.market_data.set_prices(99.55, 99.52, 99.58, JOB_NOW)
    summary = await job.run_once()
    assert summary["transitions_applied"] == 2  # ENTRY_CONFIRMED -> ACTIVE_RESEARCH

    refreshed = await signals.get(row.id)
    assert refreshed is not None
    assert refreshed.state == "ACTIVE_RESEARCH" and refreshed.state_version == 4
    assert refreshed.entry_confirmed_at is not None
    events = await SignalEventRepository(session_factory).events_for(row.id)
    assert [event.event_type for event in events][-2:] == ["ENTRY_CONFIRMED", "ACTIVE_RESEARCH"]
    entry_event = events[-2]
    assert "not a fill" in entry_event.detail["reason"]
    assert entry_event.detail["metadata"]["approximation_flags"] == [
        "CANDLE_APPROXIMATED_INTRABAR_ORDER"
    ]


async def test_invalidation_before_entry_is_terminal(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, stubs = _make_job(session_factory, pk)
    row = await _admitted_signal(session_factory, pk, job, signals)

    stubs.market_data.set_prices(96.0, 95.9, 96.1, JOB_NOW)  # below 96.45
    await job.run_once()
    refreshed = await signals.get(row.id)
    assert refreshed is not None
    assert refreshed.state == "INVALIDATED"
    assert refreshed.terminal_at is not None and refreshed.active_key is None
    assert await signals.non_terminal() == []
    # terminal signals are never re-evaluated
    summary = await job.run_once()
    assert summary["signals_evaluated"] == 0


async def test_plan_supersede_flows_into_signal(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    plan = await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, _ = _make_job(session_factory, pk)
    row = await _admitted_signal(session_factory, pk, job, signals)

    # a NEWER eligible plan replaces the research context (old one retired)
    plans = RiskPlanRepository(session_factory)
    await _seed_plan(session_factory, pk, candidate.candidate_id, dedupe_key="newer-plan-01")
    assert await plans.transition(plan.plan_id, PlanStatus.SUPERSEDED, {"reason": "test"})
    await job.run_once()
    refreshed = await signals.get(row.id)
    assert refreshed is not None and refreshed.state == "SUPERSEDED"


async def test_data_quality_grace_then_data_invalid(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, stubs = _make_job(session_factory, pk)
    row = await _admitted_signal(session_factory, pk, job, signals)

    stubs.data_quality.status = InstrumentQualityStatus.DEGRADED
    await job.run_once()
    refreshed = await signals.get(row.id)
    assert refreshed is not None
    assert refreshed.state == "WATCHING_ENTRY"  # inside the grace period
    assert refreshed.data_degraded_since is not None

    stubs.clock.now = JOB_NOW + timedelta(seconds=240)  # beyond the 120s grace
    await job.run_once()
    refreshed = await signals.get(row.id)
    assert refreshed is not None and refreshed.state == "DATA_INVALID"

    updates = await SignalUpdateRepository(session_factory).updates_for(row.id)
    assert any(update.update_type == "DATA_QUALITY_DEGRADED" for update in updates)


async def test_data_quality_recovery_clears_marker(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, stubs = _make_job(session_factory, pk)
    row = await _admitted_signal(session_factory, pk, job, signals)

    stubs.data_quality.status = InstrumentQualityStatus.DEGRADED
    await job.run_once()
    stubs.data_quality.status = InstrumentQualityStatus.HEALTHY
    await job.run_once()
    refreshed = await signals.get(row.id)
    assert refreshed is not None
    assert refreshed.state == "WATCHING_ENTRY" and refreshed.data_degraded_since is None


async def test_expiry_terminates_watching_signal(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, stubs = _make_job(session_factory, pk)
    row = await _admitted_signal(session_factory, pk, job, signals)

    stubs.clock.now = JOB_NOW + timedelta(minutes=45)  # past plan expiry 12:30
    await job.run_once()
    refreshed = await signals.get(row.id)
    assert refreshed is not None and refreshed.state == "EXPIRED"


# ------------------------------------------------- concurrency and degradation


async def test_foreign_fresh_lease_skips_evaluation(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, _ = _make_job(session_factory, pk)
    row = await _admitted_signal(session_factory, pk, job, signals)

    assert await signals.claim(row.id, "other-worker", JOB_NOW, 60)
    summary = await job.run_once()
    assert summary["signals_evaluated"] == 0  # lease respected


async def test_expired_foreign_lease_is_reclaimed(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, _ = _make_job(session_factory, pk)
    row = await _admitted_signal(session_factory, pk, job, signals)

    assert await signals.claim(row.id, "crashed-worker", JOB_NOW - timedelta(minutes=5), 60)
    summary = await job.run_once()  # restart recovery: expired lease reclaimed
    assert summary["signals_evaluated"] == 1


async def test_version_conflict_records_rejection(session_factory, monkeypatch) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, stubs = _make_job(session_factory, pk)
    await _admitted_signal(session_factory, pk, job, signals)

    # a concurrent writer wins the race just before our UPDATE
    from app.repositories.signal_lifecycle_repository import TransitionOutcome

    async def conflict(*args: Any, **kwargs: Any) -> TransitionOutcome:
        return TransitionOutcome.CONFLICT

    monkeypatch.setattr(job._signals, "apply_transition", conflict)
    stubs.market_data.set_prices(96.0, 95.9, 96.1, JOB_NOW)
    summary = await job.run_once()
    assert summary["version_conflicts"] == 1 and summary["transitions_applied"] == 0
    counts = await SignalRejectionRepository(session_factory).counts_by_code()
    assert counts.get("STATE_VERSION_CONFLICT") == 1


async def test_db_outage_degrades_and_never_reports_success(session_factory, monkeypatch) -> None:
    pk = await _seed_instrument(session_factory)
    job, _, _ = _make_job(session_factory, pk)

    async def broken() -> list:
        raise RuntimeError("db down")

    monkeypatch.setattr(job._signals, "non_terminal", broken)
    await job.run_once()
    assert job.degraded
    assert job.last_success_at is None
    await job.start()
    try:
        assert job.state.value == "DEGRADED"  # alive task + degraded flag
    finally:
        await job.stop()
    assert job.state.value == "UNAVAILABLE"  # no running task


async def test_unpersisted_transition_is_not_reported_applied(session_factory, monkeypatch) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, stubs = _make_job(session_factory, pk)
    row = await _admitted_signal(session_factory, pk, job, signals)

    from app.repositories.signal_lifecycle_repository import TransitionOutcome

    async def failed(*args: Any, **kwargs: Any) -> TransitionOutcome:
        return TransitionOutcome.FAILED

    monkeypatch.setattr(job._signals, "apply_transition", failed)
    stubs.market_data.set_prices(96.0, 95.9, 96.1, JOB_NOW)
    summary = await job.run_once()
    assert summary["transitions_applied"] == 0
    assert job.degraded  # DB write failure degrades the subsystem
    refreshed = await signals.get(row.id)
    assert refreshed is not None and refreshed.state == "WATCHING_ENTRY"


async def test_overlap_lock_and_trigger_coalescing(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, _, _ = _make_job(session_factory, pk)
    async with job._lock:  # a cycle is "running"
        summary = await job.run_once()
    assert summary == {}  # overlap skipped, previous summary returned
    job.trigger()
    job.trigger()  # coalesces into one wakeup event
    assert job._wakeup.is_set()


# --------------------------------------------------------------------- reports


async def test_reports_carry_disclaimer_and_no_prices(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    await _seed_plan(session_factory, pk, candidate.candidate_id)
    await _seed_risk_snapshot(session_factory, pk)
    job, signals, _ = _make_job(session_factory, pk)
    await _admitted_signal(session_factory, pk, job, signals)

    health = await job.health_stats()
    assert health["state"] == "UNAVAILABLE"  # background task not started in tests
    assert health["signal_counts_by_state"] == {"WATCHING_ENTRY": 1}

    status = await job.status_stats()
    assert status["note"] == DISCLAIMER
    assert status["lifecycle_model"].startswith("internal_research_lifecycle_v1@")
    status_text = str(status).lower()
    for banned in ("entry_low", "invalidation_price", "target_prices", "99.5", "96.45"):
        assert banned not in status_text, banned
    assert status["recent_signals"][0]["note"] == "Research Lifecycle - kein Handelssignal."

    dashboard = await job.dashboard_details()
    assert dashboard is not None
    assert dashboard["disclaimer"] == DISCLAIMER
    detail = dashboard["signals"][0]
    assert detail["model_reference_levels"]["label"].startswith("Interne Modell-Referenzwerte")
    assert detail["transitions"][0]["event"] == "CREATED"
    assert dashboard["state_machine"]["transitions"]["INVALIDATED"] == []


def test_api_exposes_signal_lifecycle_sections() -> None:
    class FakeSignals:
        async def health_stats(self):
            return {"state": "HEALTHY", "signal_counts_by_state": {"WATCHING_ENTRY": 1}}

        async def status_stats(self):
            return {**(await self.health_stats()), "note": DISCLAIMER}

        async def dashboard_details(self):
            return {"disclaimer": DISCLAIMER, "signals": []}

    ctx = make_ctx()
    ctx.signals = FakeSignals()
    app = create_app(context=ctx)
    with TestClient(app) as client:
        health = client.get("/health").json()
        status = client.get("/status").json()
        dashboard = client.get("/dashboard").json()

    assert health["components"]["signals"]["state"] == "HEALTHY"
    assert status["signals"]["note"] == DISCLAIMER
    assert dashboard["signals"]["disclaimer"] == DISCLAIMER


def test_api_reports_disabled_signal_lifecycle() -> None:
    ctx = make_ctx()
    ctx.settings.signals.lifecycle_enabled = False
    app = create_app(context=ctx)
    with TestClient(app) as client:
        health = client.get("/health").json()
    assert health["components"]["signals"] == "disabled"


async def test_notify_disabled_no_telegram_paths(session_factory) -> None:
    """Hard boundary: phase 10 sends nothing to Telegram."""
    settings = make_signal_settings()
    assert settings.lifecycle_notify_enabled is False
    assert settings.lifecycle_telegram_output_enabled is False
    pk = await _seed_instrument(session_factory)
    job, _, _ = _make_job(session_factory, pk, settings=settings)
    assert not hasattr(job, "_telegram")
    # no telegram module is importable from the job (imports are AST-checked
    # in test_isolation.py; here we assert the wired instance has no channel)
    for attribute in vars(job).values():
        assert "telegram" not in type(attribute).__module__.lower()
