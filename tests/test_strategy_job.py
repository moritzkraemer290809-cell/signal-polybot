"""Strategy evaluation job: scheduling discipline, dedupe/lifecycle wiring,
paused mode, data-loss handling, degradation - plus the API surfaces.

The context builder is stubbed with deterministic synthetic series; all
persistence runs against sqlite.  No network, no REST, no Telegram.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from tests.strategy_helpers import build_context, default_series_set
from tests.test_health_status import make_ctx

from app.config import StrategySettings
from app.domain.models import InstrumentMeta
from app.jobs.strategy_evaluation_refresh import StrategyEvaluationJob
from app.main import create_app
from app.repositories.feature_repository import FeatureRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.regime_repository import RegimeRepository
from app.repositories.setup_candidate_repository import SetupCandidateRepository
from app.repositories.strategy_decision_repository import StrategyDecisionRepository
from app.strategy.enums import CandidateState, RejectionCode
from app.strategy.evaluation_context import ContextBuildError
from app.strategy.feature_store import FeatureStore
from app.strategy.strategy_engine import evaluate_context
from app.strategy.version import configuration_hash

SETTINGS = StrategySettings(_env_file=None)
SERIES, SERIES_AS_OF = default_series_set(trend_up=True, settings=SETTINGS)


class StubBuilder:
    """Deterministic EvaluationContextBuilder stand-in."""

    def __init__(self) -> None:
        self.config_hash = configuration_hash(SETTINGS)
        self.fail_with: ContextBuildError | None = None
        self.build_calls = 0

    async def build(self, **kwargs):
        self.build_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return build_context(
            SERIES,
            SERIES_AS_OF,
            SETTINGS,
            instrument_pk=kwargs["instrument_pk"],
            instrument_id=kwargs["instrument_id"],
            symbol=kwargs["symbol"],
            bot_paused=kwargs["bot_paused"],
        )


class StubSelection:
    def __init__(self, rows: list) -> None:
        self.rows = rows
        self.last_equity_snapshot = None
        self.last_crypto_snapshot = None

    async def active_watchlist_rows(self) -> list:
        return self.rows


class StubCandles:
    def __init__(self, latest: datetime | None) -> None:
        self.latest = latest

    async def latest_open_time(self, instrument_pk, timeframe):
        return self.latest


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


async def _seed_instrument(session_factory, symbol: str = "BTC-PERP") -> int:
    repo = InstrumentRepository(session_factory)
    meta = InstrumentMeta.from_api({"instrument_id": 1, "symbol": symbol, "category": "crypto"})
    await repo.upsert_discovered([meta], {symbol})
    row = await repo.get_by_symbol(symbol)
    assert row is not None
    return row.id


def _make_job(
    session_factory,
    pk: int,
    *,
    settings: StrategySettings = SETTINGS,
    paused: bool = False,
    candidate_repo: SetupCandidateRepository | None = None,
):
    builder = StubBuilder()
    candles = StubCandles(SERIES_AS_OF - timedelta(minutes=5))
    clock = Clock(SERIES_AS_OF)
    candidates = candidate_repo or SetupCandidateRepository(session_factory)
    job = StrategyEvaluationJob(
        settings,
        builder,  # type: ignore[arg-type]
        FeatureStore(
            FeatureRepository(session_factory),
            RegimeRepository(session_factory),
            settings,
        ),
        candidates,
        StrategyDecisionRepository(session_factory),
        candles,  # type: ignore[arg-type]
        InstrumentRepository(session_factory),
        StubSelection(
            [
                SimpleNamespace(
                    instrument_pk=pk,
                    symbol="BTC-PERP",
                    asset_class="CRYPTO",
                    quality_score=90,
                )
            ]
        ),  # type: ignore[arg-type]
        StubBotState(paused),  # type: ignore[arg-type]
        now_fn=clock,
    )
    return job, builder, candles, clock, candidates


# ------------------------------------------------------------------ cycles


async def test_run_once_evaluates_and_persists_candidates(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, _, _, _, candidates = _make_job(session_factory, pk)
    summary = await job.run_once()
    assert summary["watchlist_instruments"] == 1
    assert summary["evaluated"] == 1
    assert summary["candidates_found"] >= 1
    rows = await candidates.active_candidates(pk)
    assert rows
    assert rows[0].symbol == "BTC-PERP"
    assert job.per_instrument["BTC-PERP"]["regime"] == "TREND_UP"
    assert not job.degraded
    # feature snapshot + regime history persisted alongside
    assert await FeatureRepository(session_factory).snapshot_count() == 1
    assert await RegimeRepository(session_factory).latest_regime(pk, "1h") == "TREND_UP"


async def test_evaluation_only_on_new_closed_5m_candle(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, builder, candles, clock, _ = _make_job(session_factory, pk)
    first = await job.run_once()
    assert first["evaluated"] == 1

    # no new 5m close -> instrument skipped entirely
    second = await job.run_once()
    assert second["evaluated"] == 0
    assert second["skipped_no_new_candle"] == 1
    assert builder.build_calls == 1

    # a new CLOSED 5m candle -> evaluated again
    candles.latest = SERIES_AS_OF
    clock.now = SERIES_AS_OF + timedelta(minutes=5)
    third = await job.run_once()
    assert third["evaluated"] == 1

    # a newer but still OPEN candle does not trigger a run
    candles.latest = SERIES_AS_OF + timedelta(minutes=5)
    clock.now = SERIES_AS_OF + timedelta(minutes=6)
    fourth = await job.run_once()
    assert fourth["evaluated"] == 0
    assert fourth["skipped_no_new_candle"] == 1


async def test_forced_trigger_bypasses_candle_discipline(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, builder, _, _, _ = _make_job(session_factory, pk)
    await job.run_once()
    job.trigger()
    summary = await job.run_once()
    assert summary["evaluated"] == 1
    assert builder.build_calls == 2


async def test_paused_mode_creates_no_candidates(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, _, _, _, candidates = _make_job(session_factory, pk, paused=True)
    summary = await job.run_once()
    assert summary["paused"] is True
    assert await candidates.active_candidates(pk) == []
    rejections = await StrategyDecisionRepository(session_factory).recent()
    assert any(row.primary_code == RejectionCode.BOT_PAUSED.value for row in rejections)


async def test_empty_watchlist_means_no_evaluations(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, builder, _, _, _ = _make_job(session_factory, pk)
    job._selection = StubSelection([])  # type: ignore[assignment]
    summary = await job.run_once()
    assert summary["watchlist_instruments"] == 0
    assert summary["evaluated"] == 0
    assert builder.build_calls == 0


async def test_data_gap_expires_actives_and_records_rejection(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, builder, _, _, candidates = _make_job(session_factory, pk)
    await job.run_once()
    assert await candidates.active_candidates(pk)

    builder.fail_with = ContextBuildError(RejectionCode.CANDLE_GAP, "gap in 5m candles")
    job.trigger()
    await job.run_once()
    assert await candidates.active_candidates(pk) == []
    rows = await StrategyDecisionRepository(session_factory).recent()
    assert any(row.primary_code == RejectionCode.CANDLE_GAP.value for row in rows)
    assert job.per_instrument["BTC-PERP"]["data_fresh"] is False


async def test_candidates_expire_after_evaluation_window(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, _, _, clock, candidates = _make_job(session_factory, pk)
    await job.run_once()
    assert await candidates.active_candidates(pk)

    # past expiry (expire_after_5m_candles * 5m), no new candle needed
    clock.now = SERIES_AS_OF + timedelta(minutes=5 * SETTINGS.expire_after_5m_candles + 1)
    await job.run_once()
    assert await candidates.active_candidates(pk) == []
    counts = await candidates.counts_by_state()
    assert counts.get("EXPIRED", 0) >= 1


async def test_db_failure_degrades_without_crashing(session_factory) -> None:
    pk = await _seed_instrument(session_factory)

    class FailingCandidates(SetupCandidateRepository):
        async def active_by_dedupe_key(self, dedupe_key):
            raise RuntimeError("db down")

    job, _, _, _, _ = _make_job(
        session_factory, pk, candidate_repo=FailingCandidates(session_factory)
    )
    summary = await job.run_once()
    assert summary["evaluated"] == 1
    assert job.degraded  # unpersisted candidate is never reported as success


async def test_admit_upgrades_detected_to_confirmed(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, _, _, clock, candidates = _make_job(session_factory, pk)
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    outcome = evaluate_context(build_context(series, as_of, SETTINGS, instrument_pk=pk), SETTINGS)
    detected = outcome.candidates[0]
    assert detected.state is CandidateState.DETECTED
    await job._admit_candidate(detected, clock.now)
    confirmed = dataclasses.replace(
        detected, candidate_id=uuid.uuid4(), state=CandidateState.CONFIRMED
    )
    await job._admit_candidate(confirmed, clock.now)
    rows = await candidates.active_candidates(pk)
    assert len(rows) == 1
    assert rows[0].id == detected.candidate_id  # original row upgraded in place
    assert rows[0].state == "CONFIRMED"
    events = await candidates.events_for(detected.candidate_id)
    assert [event.to_state for event in events] == ["DETECTED", "CONFIRMED"]


async def test_confirmed_stronger_supersedes_opposite_weaker(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, _, _, clock, candidates = _make_job(session_factory, pk)
    up_series, up_as_of = default_series_set(trend_up=True, settings=SETTINGS)
    bullish = evaluate_context(
        build_context(up_series, up_as_of, SETTINGS, instrument_pk=pk), SETTINGS
    ).candidates[0]
    down_series, down_as_of = default_series_set(trend_up=False, settings=SETTINGS)
    bearish = evaluate_context(
        build_context(down_series, down_as_of, SETTINGS, instrument_pk=pk), SETTINGS
    ).candidates[0]

    await job._admit_candidate(bearish, clock.now)
    stronger = dataclasses.replace(
        bullish,
        candidate_id=uuid.uuid4(),
        state=CandidateState.CONFIRMED,
        setup_score=bearish.setup_score + 5,
    )
    await job._admit_candidate(stronger, clock.now)

    rows = await candidates.active_candidates(pk)
    assert [row.id for row in rows] == [stronger.candidate_id]
    events = await candidates.events_for(bearish.candidate_id)
    assert events[-1].to_state == "SUPERSEDED"


async def test_max_active_candidates_cap(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, _, _, clock, candidates = _make_job(session_factory, pk)
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    base = evaluate_context(
        build_context(series, as_of, SETTINGS, instrument_pk=pk), SETTINGS
    ).candidates[0]
    for suffix in ("a", "b"):
        await job._admit_candidate(
            dataclasses.replace(base, candidate_id=uuid.uuid4(), dedupe_key=f"cap-test-{suffix}"),
            clock.now,
        )
    assert len(await candidates.active_candidates(pk)) == 2
    await job._admit_candidate(
        dataclasses.replace(base, candidate_id=uuid.uuid4(), dedupe_key="cap-test-c"),
        clock.now,
    )
    assert len(await candidates.active_candidates(pk)) == 2  # cap holds


async def test_terminal_dedupe_window_suppresses_reappearance(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    job, _, _, clock, candidates = _make_job(session_factory, pk)
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    base = evaluate_context(
        build_context(series, as_of, SETTINGS, instrument_pk=pk), SETTINGS
    ).candidates[0]
    await job._admit_candidate(base, clock.now)
    await candidates.transition(base.candidate_id, CandidateState.EXPIRED)
    job._recent_terminal_dedupe[base.dedupe_key] = clock.now

    reappear = dataclasses.replace(base, candidate_id=uuid.uuid4())
    await job._admit_candidate(reappear, clock.now)
    assert await candidates.active_candidates(pk) == []  # suppressed

    # outside the dedupe window the setup may be recorded again
    later = clock.now + timedelta(seconds=SETTINGS.candidate_dedupe_seconds + 1)
    await job._admit_candidate(dataclasses.replace(base, candidate_id=uuid.uuid4()), later)
    assert len(await candidates.active_candidates(pk)) == 1


# ----------------------------------------------------------- API surfaces


def _fake_strategy_coordinator():
    async def health_stats():
        return {
            "state": "HEALTHY",
            "job_alive": True,
            "watchlist_instruments": 1,
            "active_candidates": 1,
            "evaluations_total": 3,
            "evaluations_failed": 0,
            "last_success_at": "2026-08-21T12:00:00+00:00",
        }

    async def status_stats():
        stats = await health_stats()
        stats.update(
            {
                "strategy_name": "market_structure_v1",
                "strategy_version": "1.0.0",
                "config_hash": "abc123",
                "note": "Research candidates only - not trade signals.",
            }
        )
        return stats

    async def dashboard_details():
        return {
            "disclaimer": "Research-Ausgabe. Kein Trade-Signal. Keine Renditeprognose.",
            "candidate_counts_by_state": {"DETECTED": 1},
            "recent_candidates": [
                {
                    "symbol": "BTC-PERP",
                    "type": "BULLISH_SWEEP_REVERSAL",
                    "direction": "BULLISH (research classification)",
                    "state": "DETECTED",
                    "score": 85,
                }
            ],
            "recent_rejections": [],
        }

    return SimpleNamespace(
        health_stats=health_stats,
        status_stats=status_stats,
        dashboard_details=dashboard_details,
    )


def test_api_exposes_strategy_research_sections() -> None:
    ctx = make_ctx()
    ctx.strategy = _fake_strategy_coordinator()
    app = create_app(context=ctx)
    with TestClient(app) as client:
        health = client.get("/health").json()
        status = client.get("/status").json()
        dashboard = client.get("/dashboard").json()

    assert health["components"]["strategy"]["state"] == "HEALTHY"
    assert status["strategy"]["strategy_version"] == "1.0.0"
    assert "not trade signals" in status["strategy"]["note"]
    assert (
        dashboard["strategy"]["disclaimer"]
        == "Research-Ausgabe. Kein Trade-Signal. Keine Renditeprognose."
    )
    # no secrets and no trade parameters in the strategy research sections
    # (instrument metadata like max_leverage is public exchange data and is
    # outside the strategy payload)
    strategy_sections = (
        health["components"]["strategy"],
        status["strategy"],
        dashboard["strategy"],
    )
    for payload in strategy_sections:
        text = str(payload).lower()
        for banned in (
            "token",
            "secret",
            "entry_price",
            "stop_loss",
            "take_profit",
            "leverage",
            "position_size",
        ):
            assert banned not in text, banned


def test_api_reports_disabled_strategy() -> None:
    ctx = make_ctx()
    ctx.settings.strategy.enabled = False
    app = create_app(context=ctx)
    with TestClient(app) as client:
        health = client.get("/health").json()
    assert health["components"]["strategy"] == "disabled"
