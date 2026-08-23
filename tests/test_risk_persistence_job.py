"""Risk persistence and evaluation job: lifecycle, dedupe, degradation, API.

Persistence runs against sqlite; the context builder is stubbed with the
deterministic risk fixtures.  No network, no Telegram, no account data.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from tests.risk_helpers import COST_SETTINGS, RISK_SETTINGS, make_candidate, make_context
from tests.strategy_helpers import build_context, default_series_set
from tests.test_health_status import make_ctx

from app.config import CostSettings, StrategySettings
from app.costs.version import cost_configuration_hash
from app.domain.models import InstrumentMeta
from app.jobs.risk_plan_evaluation_refresh import RiskPlanEvaluationJob
from app.main import create_app
from app.repositories.cost_estimate_repository import CostEstimateRepository
from app.repositories.fee_schedule_repository import (
    ExecutionAssumptionRepository,
    FeeScheduleRepository,
)
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.instrument_risk_repository import InstrumentRiskRepository
from app.repositories.risk_plan_rejection_repository import RiskPlanRejectionRepository
from app.repositories.risk_plan_repository import RiskPlanRepository
from app.repositories.setup_candidate_repository import SetupCandidateRepository
from app.risk.enums import PlanRejectionCode, PlanStatus
from app.risk.models import RiskPlanRejection
from app.risk.risk_engine import evaluate_candidate
from app.strategy.enums import CandidateState
from app.strategy.strategy_engine import evaluate_context

AS_OF = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
STRATEGY_SETTINGS = StrategySettings(_env_file=None)


async def _seed_instrument(session_factory, symbol: str = "BTC-PERP") -> int:
    repo = InstrumentRepository(session_factory)
    meta = InstrumentMeta.from_api({"instrument_id": 1, "symbol": symbol, "category": "crypto"})
    await repo.upsert_discovered([meta], {symbol})
    row = await repo.get_by_symbol(symbol)
    assert row is not None
    return row.id


async def _seed_confirmed_candidate(session_factory, pk: int):
    """A real phase-8 candidate row, promoted to CONFIRMED."""
    series, as_of = default_series_set(trend_up=True, settings=STRATEGY_SETTINGS)
    outcome = evaluate_context(
        build_context(
            series,
            as_of,
            STRATEGY_SETTINGS,
            instrument_pk=pk,
            instrument_id=1,
            symbol="BTC-PERP",
        ),
        STRATEGY_SETTINGS,
    )
    candidate = dataclasses.replace(outcome.candidates[0], state=CandidateState.CONFIRMED)
    repo = SetupCandidateRepository(session_factory)
    assert await repo.insert_new(candidate)
    return candidate


def _eligible_plan(candidate_id=None, instrument_pk: int = 10):
    context = make_context(
        candidate=make_candidate(
            candidate_id=candidate_id or uuid.uuid4(), instrument_pk=instrument_pk
        )
    )
    outcome = evaluate_candidate(context, RISK_SETTINGS, COST_SETTINGS)
    assert outcome.plan is not None
    return outcome.plan, context


def _rejection(candidate_id, pk: int, code=PlanRejectionCode.NET_RR_BELOW_THRESHOLD, as_of=AS_OF):
    return RiskPlanRejection(
        rejection_id=uuid.uuid4(),
        candidate_id=candidate_id,
        instrument_pk=pk,
        instrument_id=1,
        symbol="BTC-PERP",
        as_of=as_of,
        primary_code=code,
        codes=(code,),
        detail="test detail",
        risk_model_name="conservative_eligibility_v1",
        risk_model_version="1.0.0",
        risk_config_hash="riskcfg",
        cost_model_version="1.0.0",
        cost_config_hash="costcfg",
        fee_schedule_version="polymarket-perps-assumed-1",
    )


# ---------------------------------------------------------------- repositories


async def test_plan_insert_dedupe_and_lifecycle(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = RiskPlanRepository(session_factory)
    fixed_candidate = uuid.uuid4()
    plan, _ = _eligible_plan(fixed_candidate, pk)
    plan = dataclasses.replace(plan, instrument_pk=pk)
    assert await repo.insert_new(plan)

    duplicate, _ = _eligible_plan(fixed_candidate, pk)
    duplicate = dataclasses.replace(duplicate, instrument_pk=pk)
    assert duplicate.dedupe_key == plan.dedupe_key
    assert not await repo.insert_new(duplicate)  # unique active_key blocks

    assert await repo.transition(plan.plan_id, PlanStatus.EXPIRED, {"reason": "test"})
    assert await repo.active_by_dedupe_key(plan.dedupe_key) is None
    assert await repo.insert_new(duplicate)  # slot free after terminal state

    events = await repo.events_for(plan.plan_id)
    assert [(event.from_status, event.to_status) for event in events] == [
        (None, "ELIGIBLE"),
        ("ELIGIBLE", "EXPIRED"),
    ]
    counts = await repo.counts_by_status()
    assert counts == {"EXPIRED": 1, "ELIGIBLE": 1}
    # research payload is persisted with its disclaimer
    row = (await repo.active_plans(pk))[0]
    assert "Risk Research" in row.payload["disclaimer"]


async def test_rejections_aggregate_per_candidate_and_bucket(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = RiskPlanRejectionRepository(session_factory)
    candidate_id = uuid.uuid4()
    assert await repo.record_rejection(_rejection(candidate_id, pk), 300.0)
    assert not await repo.record_rejection(
        _rejection(candidate_id, pk, as_of=AS_OF + timedelta(seconds=60)), 300.0
    )
    rows = await repo.recent()
    assert len(rows) == 1
    assert rows[0].count == 2
    # different candidate or code -> new row
    assert await repo.record_rejection(_rejection(uuid.uuid4(), pk), 300.0)
    assert await repo.record_rejection(
        _rejection(candidate_id, pk, code=PlanRejectionCode.MARGIN_MODEL_UNAVAILABLE), 300.0
    )
    counts = await repo.counts_by_code()
    assert counts["NET_RR_BELOW_THRESHOLD"] == 3
    assert counts["MARGIN_MODEL_UNAVAILABLE"] == 1
    assert await repo.count_since(AS_OF - timedelta(hours=1)) == 4


async def test_fee_schedule_versions_are_immutable(session_factory) -> None:
    repo = FeeScheduleRepository(session_factory)
    assert await repo.seed("v1", {"tiers": {"default": {}}}, "config")
    assert not await repo.seed("v1", {"tiers": {"other": {}}}, "config")  # immutable
    assert await repo.activate("v1")
    active = await repo.active_schedule()
    assert active is not None and active.version == "v1"
    assert active.schedule == {"tiers": {"default": {}}}  # original content kept

    assert await repo.seed("v2", {"tiers": {}}, "config")
    assert await repo.activate("v2")
    active = await repo.active_schedule()
    assert active is not None and active.version == "v2"
    versions = await repo.list_versions()
    assert [row.active for row in versions] == [False, True]
    assert not await repo.activate("missing")


async def test_cost_estimate_and_snapshot_persistence(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    plan, context = _eligible_plan(uuid.uuid4(), pk)
    cost_repo = CostEstimateRepository(session_factory)
    await cost_repo.add(
        plan.costs,
        estimate_id=uuid.uuid4(),
        plan_id=plan.plan_id,
        candidate_id=plan.candidate_id,
        instrument_pk=pk,
        symbol=plan.symbol,
        as_of=plan.as_of,
    )
    assert await cost_repo.count() == 1
    row = (await cost_repo.recent())[0]
    assert row.fee_schedule_version == COST_SETTINGS.fee_schedule_version
    assert "keine Garantie" in row.payload["summary"]["note"]

    snapshot_repo = InstrumentRiskRepository(session_factory)
    snapshot = dataclasses.replace(context.instrument, instrument_pk=pk)
    await snapshot_repo.add_snapshot(uuid.uuid4(), snapshot)
    latest = await snapshot_repo.latest_for(pk)
    assert latest is not None
    assert latest.payload["source"] == "test_fixture"
    assert latest.snapshot_version == "irs-1"


# ------------------------------------------------------------------------ job


class StubBuilder:
    """Deterministic RiskPlanEvaluationContextBuilder stand-in."""

    def __init__(self) -> None:
        from app.risk.version import risk_configuration_hash

        self.risk_config_hash = risk_configuration_hash(RISK_SETTINGS)
        self.cost_config_hash = cost_configuration_hash(COST_SETTINGS)
        self.fail_with = None
        self.build_calls = 0

    async def build(self, **kwargs):
        self.build_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        passed = kwargs["candidate"]
        fixture = make_candidate(
            candidate_id=passed.candidate_id,
            instrument_pk=passed.instrument_pk,
            state=passed.state,
            expiry_at=passed.expiry_at,
        )
        return make_context(
            candidate=fixture,
            fee_schedule=kwargs["fee_schedule"],
            bot_paused=kwargs["bot_paused"],
            watchlist_active=kwargs["watchlist_active"],
        )


class StubSelection:
    def __init__(self, rows: list) -> None:
        self.rows = rows
        self.last_equity_snapshot = None
        self.last_crypto_snapshot = None

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


def _make_job(
    session_factory,
    pk: int,
    *,
    paused: bool = False,
    cost_settings: CostSettings = COST_SETTINGS,
    plan_repo: RiskPlanRepository | None = None,
):
    builder = StubBuilder()
    clock = Clock(AS_OF)
    plans = plan_repo or RiskPlanRepository(session_factory)
    job = RiskPlanEvaluationJob(
        RISK_SETTINGS,
        cost_settings,
        builder,  # type: ignore[arg-type]
        plans,
        RiskPlanRejectionRepository(session_factory),
        CostEstimateRepository(session_factory),
        InstrumentRiskRepository(session_factory),
        FeeScheduleRepository(session_factory),
        ExecutionAssumptionRepository(session_factory),
        SetupCandidateRepository(session_factory),
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
    return job, builder, clock, plans


async def test_job_creates_eligible_plan_and_artifacts(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_confirmed_candidate(session_factory, pk)
    job, _, _, plans = _make_job(session_factory, pk)
    summary = await job.run_once()
    assert summary["confirmed_candidates"] == 1
    assert summary["evaluated"] == 1
    assert summary["plans_created"] == 1
    rows = await plans.active_plans(pk)
    assert len(rows) == 1
    assert rows[0].candidate_id == candidate.candidate_id
    assert rows[0].status == "ELIGIBLE"
    # cost estimate + instrument snapshot + auto-seeded fee schedule
    assert await CostEstimateRepository(session_factory).count() == 1
    assert await InstrumentRiskRepository(session_factory).count() == 1
    active_fee = await FeeScheduleRepository(session_factory).active_schedule()
    assert active_fee is not None
    assert active_fee.version == COST_SETTINGS.fee_schedule_version
    assert job.fee_schedule_available
    assert not job.degraded


async def test_job_dedupes_identical_re_evaluation(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    await _seed_confirmed_candidate(session_factory, pk)
    job, _, _, plans = _make_job(session_factory, pk)
    await job.run_once()
    job.trigger()  # force re-evaluation of the unchanged candidate
    summary = await job.run_once()
    assert summary["evaluated"] == 1
    assert summary["plans_created"] == 0  # identical plan suppressed
    assert len(await plans.active_plans(pk)) == 1


async def test_job_skips_unchanged_candidates_without_trigger(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    await _seed_confirmed_candidate(session_factory, pk)
    job, builder, _, _ = _make_job(session_factory, pk)
    await job.run_once()
    summary = await job.run_once()
    assert summary["skipped"] == 1
    assert builder.build_calls == 1


async def test_job_only_processes_confirmed_candidates(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    series, as_of = default_series_set(trend_up=True, settings=STRATEGY_SETTINGS)
    outcome = evaluate_context(
        build_context(series, as_of, STRATEGY_SETTINGS, instrument_pk=pk), STRATEGY_SETTINGS
    )
    repo = SetupCandidateRepository(session_factory)
    assert await repo.insert_new(outcome.candidates[0])  # DETECTED only
    job, _, _, _ = _make_job(session_factory, pk)
    summary = await job.run_once()
    assert summary["confirmed_candidates"] == 0
    assert summary["evaluated"] == 0


async def test_paused_mode_creates_no_plans(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    await _seed_confirmed_candidate(session_factory, pk)
    job, _, _, plans = _make_job(session_factory, pk, paused=True)
    summary = await job.run_once()
    assert summary["paused"] is True
    assert summary["plans_created"] == 0
    assert await plans.active_plans(pk) == []
    rejections = await RiskPlanRejectionRepository(session_factory).recent()
    assert any(row.primary_code == "BOT_PAUSED" for row in rejections)


async def test_candidate_expiry_mirrors_to_plan(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_confirmed_candidate(session_factory, pk)
    job, _, _, plans = _make_job(session_factory, pk)
    await job.run_once()
    assert len(await plans.active_plans(pk)) == 1

    repo = SetupCandidateRepository(session_factory)
    assert await repo.transition(candidate.candidate_id, CandidateState.EXPIRED)
    job.trigger()
    await job.run_once()
    assert await plans.active_plans(pk) == []
    row = (await plans.plans_for_candidate(candidate.candidate_id))[0]
    assert row.status == "EXPIRED"


async def test_config_change_supersedes_and_creates_new_plan(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_confirmed_candidate(session_factory, pk)
    job, _, _, plans = _make_job(session_factory, pk)
    await job.run_once()

    changed = CostSettings(_env_file=None, fee_schedule_version="polymarket-perps-assumed-2")
    job2, _, _, _ = _make_job(session_factory, pk, cost_settings=changed)
    await job2.run_once()
    rows = await plans.plans_for_candidate(candidate.candidate_id)
    statuses = sorted(row.status for row in rows)
    assert statuses == ["ELIGIBLE", "SUPERSEDED"]
    active = await plans.active_plans(pk)
    assert active[0].fee_schedule_version == "polymarket-perps-assumed-2"
    # the previous version is kept immutably, the new one is active
    versions = await FeeScheduleRepository(session_factory).list_versions()
    assert {row.version for row in versions} == {
        "polymarket-perps-assumed-1",
        "polymarket-perps-assumed-2",
    }


async def test_db_failure_degrades_without_crashing(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    await _seed_confirmed_candidate(session_factory, pk)

    class FailingPlans(RiskPlanRepository):
        async def active_by_dedupe_key(self, dedupe_key):
            raise RuntimeError("db down")

    job, _, _, _ = _make_job(session_factory, pk, plan_repo=FailingPlans(session_factory))
    summary = await job.run_once()
    assert summary["evaluated"] == 1
    assert summary["plans_created"] == 0
    assert job.degraded  # unpersisted plan never reported as success


async def test_plan_expiry_sweep(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    await _seed_confirmed_candidate(session_factory, pk)
    job, _, clock, plans = _make_job(session_factory, pk)
    await job.run_once()
    clock.now = AS_OF + timedelta(hours=2)  # beyond plan and candidate expiry
    await job.run_once()
    assert await plans.active_plans(pk) == []


# ----------------------------------------------------------- API surfaces


def _fake_risk_coordinator():
    async def health_stats():
        return {
            "state": "HEALTHY",
            "cost_state": "HEALTHY",
            "job_alive": True,
            "fee_schedule_available": True,
            "plan_counts_by_status": {"ELIGIBLE": 1},
            "rejections_last_hour": 2,
            "evaluations_total": 5,
            "evaluations_failed": 0,
            "last_success_at": "2026-08-22T12:00:00+00:00",
        }

    async def status_stats():
        stats = await health_stats()
        stats.update(
            {
                "risk_model": "conservative_eligibility_v1@1.0.0",
                "cost_model": "conservative_costs_v1@1.0.0",
                "fee_schedule_version": "polymarket-perps-assumed-1 (assumption only)",
                "note": (
                    "Research eligibility plans only - no trade signals, no real "
                    "account or position data."
                ),
            }
        )
        return stats

    async def dashboard_details():
        return {
            "disclaimer": (
                "Risk Research - kein Handelssignal. Keine reale Positions- oder "
                "Kontodatenbasis. Alle Preise sind hypothetische Modellwerte."
            ),
            "plan_counts_by_status": {"ELIGIBLE": 1},
            "recent_plans": [],
            "recent_rejections": [],
        }

    return SimpleNamespace(
        health_stats=health_stats,
        status_stats=status_stats,
        dashboard_details=dashboard_details,
    )


def test_api_exposes_risk_research_sections() -> None:
    ctx = make_ctx()
    ctx.risk = _fake_risk_coordinator()
    app = create_app(context=ctx)
    with TestClient(app) as client:
        health = client.get("/health").json()
        status = client.get("/status").json()
        dashboard = client.get("/dashboard").json()

    assert health["components"]["risk"]["state"] == "HEALTHY"
    assert health["components"]["risk"]["fee_schedule_available"] is True
    assert "assumption only" in status["risk"]["fee_schedule_version"]
    assert "no trade signals" in status["risk"]["note"]
    assert dashboard["risk"]["disclaimer"].startswith("Risk Research")
    # /status must not leak reference price levels or secrets
    risk_status_text = str(status["risk"]).lower()
    for banned in ("entry_reference", "invalidation_price", "token", "secret"):
        assert banned not in risk_status_text, banned


def test_api_reports_disabled_risk_engine() -> None:
    ctx = make_ctx()
    ctx.settings.risk.engine_enabled = False
    app = create_app(context=ctx)
    with TestClient(app) as client:
        health = client.get("/health").json()
    assert health["components"]["risk"] == "disabled"


async def test_persistence_failure_keeps_candidate_due_for_retry(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    await _seed_confirmed_candidate(session_factory, pk)

    class FlakyPlans(RiskPlanRepository):
        fail = True

        async def active_by_dedupe_key(self, dedupe_key):
            if FlakyPlans.fail:
                raise RuntimeError("db down")
            return await super().active_by_dedupe_key(dedupe_key)

    FlakyPlans.fail = True
    job, _, _, _plans = _make_job(session_factory, pk, plan_repo=FlakyPlans(session_factory))
    first = await job.run_once()
    assert first["plans_created"] == 0
    assert job.degraded

    # DB recovers: the candidate must be re-evaluated WITHOUT a forced trigger
    FlakyPlans.fail = False
    second = await job.run_once()
    assert second["evaluated"] == 1
    assert second["plans_created"] == 1
    assert not job.degraded


async def test_predecessor_survives_when_replacement_is_suppressed(session_factory) -> None:
    """A superseding plan may only retire its predecessor once IT is
    persisted; the per-instrument cap counts only OTHER candidates."""
    pk = await _seed_instrument(session_factory)
    job, _, clock, plans = _make_job(session_factory, pk)
    candidate_a = uuid.uuid4()
    plan_a, _ctx_a = _eligible_plan(candidate_a, pk)
    plan_a = dataclasses.replace(plan_a, instrument_pk=pk)
    assert await plans.insert_new(plan_a)

    # cap reached by ANOTHER candidate's plan -> new candidate suppressed,
    # nothing existing is touched
    job._risk = job._risk.model_copy(update={"max_active_plans_per_instrument": 1})
    plan_b, ctx_b = _eligible_plan(uuid.uuid4(), pk)
    plan_b = dataclasses.replace(plan_b, instrument_pk=pk)
    assert await job._admit_plan(plan_b, ctx_b, clock.now) == "suppressed"
    assert [row.id for row in await plans.active_plans(pk)] == [plan_a.plan_id]

    # a materially changed plan of the SAME candidate replaces its
    # predecessor even at the cap (insert first, supersede after)
    plan_a2, ctx_a2 = _eligible_plan(candidate_a, pk)
    plan_a2 = dataclasses.replace(plan_a2, instrument_pk=pk, dedupe_key="changed-dedupe-key-000001")
    assert await job._admit_plan(plan_a2, ctx_a2, clock.now) == "created"
    active = await plans.active_plans(pk)
    assert [row.id for row in active] == [plan_a2.plan_id]
    assert active[0].instrument_snapshot_id is not None  # audit link persisted
    old_row = (await plans.plans_for_candidate(candidate_a))[0]
    assert old_row.status == "SUPERSEDED"


async def test_execution_assumption_version_is_persisted(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    await _seed_confirmed_candidate(session_factory, pk)
    job, _, _, _ = _make_job(session_factory, pk)
    await job.run_once()
    active = await ExecutionAssumptionRepository(session_factory).active_version()
    assert active is not None
    assert active.version == COST_SETTINGS.execution_assumption_version
    assert active.assumptions["entry"] == "ENTRY_TAKER"
    assert active.assumptions["invalidation"] == "STOP_STRESS_TAKER"
