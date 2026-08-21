"""Strategy persistence: versioning, immutable history, dedupe, aggregation.

Runs against sqlite via the shared session_factory fixture.  All artefacts
are research outputs - never trade instructions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from tests.strategy_helpers import build_context, default_series_set

from app.config import StrategySettings
from app.domain.models import InstrumentMeta
from app.repositories.feature_repository import FeatureRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.regime_repository import RegimeRepository
from app.repositories.setup_candidate_repository import SetupCandidateRepository
from app.repositories.strategy_decision_repository import StrategyDecisionRepository
from app.strategy.enums import CandidateState, RejectionCode, StrategyRegime
from app.strategy.feature_store import FeatureStore
from app.strategy.models import RegimeResult, SetupRejection
from app.strategy.strategy_engine import evaluate_context
from app.strategy.version import configuration_hash, ruleset_hash

SETTINGS = StrategySettings(_env_file=None)
AS_OF = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


async def _seed_instrument(session_factory, symbol: str = "BTC-PERP") -> int:
    repo = InstrumentRepository(session_factory)
    meta = InstrumentMeta.from_api({"instrument_id": 1, "symbol": symbol, "category": "crypto"})
    await repo.upsert_discovered([meta], {symbol})
    row = await repo.get_by_symbol(symbol)
    assert row is not None
    return row.id


def _outcome_with_candidate(instrument_pk: int = 1):
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    context = build_context(series, as_of, SETTINGS, instrument_pk=instrument_pk)
    outcome = evaluate_context(context, SETTINGS)
    assert outcome.candidates
    return context, outcome


def _rejection(
    instrument_pk: int,
    code: RejectionCode = RejectionCode.REGIME_NO_TRADE,
    as_of: datetime = AS_OF,
    version: str = "1.0.0",
) -> SetupRejection:
    return SetupRejection(
        rejection_id=uuid.uuid4(),
        instrument_pk=instrument_pk,
        instrument_id=1,
        symbol="BTC-PERP",
        as_of=as_of,
        primary_code=code,
        codes=(code,),
        detail="test detail",
        strategy_name="market_structure_v1",
        strategy_version=version,
        config_hash="abc",
        candidate_type=None,
    )


def _regime(regime: StrategyRegime = StrategyRegime.TREND_UP) -> RegimeResult:
    return RegimeResult(
        regime=regime,
        confidence=80,
        secondary_flags=(),
        reasons=("test",),
        features_used={"efficiency_ratio": 0.5},
    )


# ------------------------------------------------------------- versioning


def test_configuration_hash_is_deterministic_and_sensitive() -> None:
    assert configuration_hash(SETTINGS) == configuration_hash(StrategySettings(_env_file=None))
    changed = StrategySettings(_env_file=None, min_setup_score=80)
    assert configuration_hash(changed) != configuration_hash(SETTINGS)
    assert ruleset_hash() == ruleset_hash()


async def test_candidate_rows_carry_versioning(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    context, outcome = _outcome_with_candidate(pk)
    repo = SetupCandidateRepository(session_factory)
    assert await repo.insert_new(outcome.candidates[0])
    row = (await repo.active_candidates(pk))[0]
    assert row.strategy_name == SETTINGS.name
    assert row.strategy_version == SETTINGS.version
    assert row.config_hash == context.config_hash
    assert row.ruleset_hash == context.ruleset_hash
    assert row.feature_schema_version == context.feature_schema_version
    assert row.details["candle_window_metadata"]  # reproducibility anchor


# --------------------------------------------------- dedupe and lifecycle


async def test_duplicate_active_candidate_is_refused(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    _, outcome = _outcome_with_candidate(pk)
    repo = SetupCandidateRepository(session_factory)
    first = outcome.candidates[0]
    assert await repo.insert_new(first)

    # a re-evaluation yields a new candidate_id but the same dedupe_key
    _, outcome2 = _outcome_with_candidate(pk)
    second = outcome2.candidates[0]
    assert second.candidate_id != first.candidate_id
    assert second.dedupe_key == first.dedupe_key
    assert not await repo.insert_new(second)  # unique active_key blocks it

    rows = await repo.active_candidates(pk)
    assert len(rows) == 1
    assert rows[0].id == first.candidate_id


async def test_terminal_state_frees_dedupe_slot(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    _, outcome = _outcome_with_candidate(pk)
    repo = SetupCandidateRepository(session_factory)
    first = outcome.candidates[0]
    assert await repo.insert_new(first)
    assert await repo.transition(first.candidate_id, CandidateState.EXPIRED)
    assert await repo.active_by_dedupe_key(first.dedupe_key) is None

    _, outcome2 = _outcome_with_candidate(pk)
    assert await repo.insert_new(outcome2.candidates[0])  # slot is free again


async def test_candidate_transitions_keep_immutable_history(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    _, outcome = _outcome_with_candidate(pk)
    repo = SetupCandidateRepository(session_factory)
    candidate = outcome.candidates[0]
    assert await repo.insert_new(candidate)
    assert await repo.transition(
        candidate.candidate_id, CandidateState.CONFIRMED, {"why": "retest"}, score=90
    )
    assert await repo.transition(candidate.candidate_id, CandidateState.EXPIRED)
    # idempotent: same state again is refused, unknown id is refused
    assert not await repo.transition(candidate.candidate_id, CandidateState.EXPIRED)
    assert not await repo.transition(uuid.uuid4(), CandidateState.EXPIRED)

    events = await repo.events_for(candidate.candidate_id)
    transitions = [(event.from_state, event.to_state) for event in events]
    assert transitions == [
        (None, candidate.state.value),
        (candidate.state.value, "CONFIRMED"),
        ("CONFIRMED", "EXPIRED"),
    ]
    counts = await repo.counts_by_state()
    assert counts == {"EXPIRED": 1}


# ------------------------------------------------- rejection aggregation


async def test_rejections_aggregate_per_bucket(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = StrategyDecisionRepository(session_factory)
    window = 300.0
    assert await repo.record_rejection(_rejection(pk, as_of=AS_OF), window)
    # same code within the same bucket aggregates instead of inserting
    assert not await repo.record_rejection(
        _rejection(pk, as_of=AS_OF + timedelta(seconds=60)), window
    )
    assert not await repo.record_rejection(
        _rejection(pk, as_of=AS_OF + timedelta(seconds=120)), window
    )
    rows = await repo.recent()
    assert len(rows) == 1
    assert rows[0].count == 3
    assert rows[0].first_as_of.replace(tzinfo=UTC) == AS_OF
    assert rows[0].last_as_of.replace(tzinfo=UTC) == AS_OF + timedelta(seconds=120)

    # a new bucket creates a new row; a different code too
    assert await repo.record_rejection(_rejection(pk, as_of=AS_OF + timedelta(seconds=400)), window)
    assert await repo.record_rejection(
        _rejection(pk, code=RejectionCode.MOMENTUM_INSUFFICIENT, as_of=AS_OF), window
    )
    counts = await repo.counts_by_code()
    assert counts["REGIME_NO_TRADE"] == 4
    assert counts["MOMENTUM_INSUFFICIENT"] == 1


# ------------------------------------------------------ regime persistence


async def test_regime_recorded_only_on_change(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = RegimeRepository(session_factory)
    assert await repo.record_if_changed(pk, "1h", _regime(), AS_OF, "1.0.0")
    assert not await repo.record_if_changed(
        pk, "1h", _regime(), AS_OF + timedelta(hours=1), "1.0.0"
    )
    assert await repo.record_if_changed(
        pk, "1h", _regime(StrategyRegime.RANGE), AS_OF + timedelta(hours=2), "1.0.0"
    )
    assert await repo.latest_regime(pk, "1h") == "RANGE"
    counts = await repo.regime_counts()
    assert counts == {"TREND_UP": 1, "RANGE": 1}


# -------------------------------------------- feature snapshots and swings


async def test_feature_artifacts_idempotent_and_retention(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    context, outcome = _outcome_with_candidate(pk)
    repo = FeatureRepository(session_factory)
    await repo.add_snapshot(
        uuid.uuid4(),
        pk,
        "BTC-PERP",
        outcome,
        strategy_name=SETTINGS.name,
        strategy_version=SETTINGS.version,
        feature_schema_version="fs-1",
        config_hash=context.config_hash,
    )
    assert await repo.snapshot_count() == 1

    swings = list(outcome.structure_by_timeframe["1h"].swings)
    assert swings
    first = await repo.add_swings(pk, swings, SETTINGS.version)
    assert first == len(swings)
    assert await repo.add_swings(pk, swings, SETTINGS.version) == 0  # idempotent

    events = list(outcome.structure_by_timeframe["1h"].events)
    assert await repo.add_structure_events(pk, events, SETTINGS.version) == len(events)
    assert await repo.add_structure_events(pk, events, SETTINGS.version) == 0

    # retention: nothing deleted within the window, everything beyond it
    assert await repo.cleanup_snapshots(retention_days=30) == 0
    assert await repo.cleanup_snapshots(retention_days=-1) == 1
    assert await repo.snapshot_count() == 0


async def test_feature_store_degrades_without_crashing(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    context, outcome = _outcome_with_candidate(pk)

    class BrokenFeatures(FeatureRepository):
        async def add_snapshot(self, *args, **kwargs):
            raise RuntimeError("db down")

    store = FeatureStore(
        BrokenFeatures(session_factory), RegimeRepository(session_factory), SETTINGS
    )
    await store.persist(pk, "BTC-PERP", outcome, uuid.uuid4(), context.config_hash)
    assert store.degraded

    healthy = FeatureStore(
        FeatureRepository(session_factory), RegimeRepository(session_factory), SETTINGS
    )
    await healthy.persist(pk, "BTC-PERP", outcome, uuid.uuid4(), context.config_hash)
    assert not healthy.degraded
