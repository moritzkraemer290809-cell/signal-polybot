"""Context builder: plan payload parsing, closed-candle discipline and
opposing structure-event filtering against real persistence rows."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from tests.signal_helpers import SIGNAL_SETTINGS
from tests.test_signal_job import (
    JOB_NOW,
    StubBooks,
    StubDataQuality,
    StubMarketData,
    _seed_candidate,
    _seed_instrument,
    _seed_plan,
)
from tests.test_signal_persistence import _new_signal

from app.repositories.candle_repository import CandleRepository
from app.repositories.feature_repository import FeatureRepository
from app.repositories.instrument_risk_repository import InstrumentRiskRepository
from app.repositories.risk_plan_repository import RiskPlanRepository
from app.repositories.setup_candidate_repository import SetupCandidateRepository
from app.repositories.signal_lifecycle_repository import SignalLifecycleRepository
from app.signals.lifecycle_context import (
    SignalLifecycleContextBuilder,
    plan_snapshot_from_record,
    signal_snapshot_from_record,
)
from app.strategy.models import StructureEvent

from app.domain.enums import Timeframe  # isort: skip
from app.domain.models import CandleData  # isort: skip
from app.strategy.enums import StructureEventType  # isort: skip


def _builder(session_factory) -> SignalLifecycleContextBuilder:
    return SignalLifecycleContextBuilder(
        SIGNAL_SETTINGS,
        RiskPlanRepository(session_factory),
        SetupCandidateRepository(session_factory),
        CandleRepository(session_factory),
        FeatureRepository(session_factory),
        InstrumentRiskRepository(session_factory),
        StubMarketData(),  # type: ignore[arg-type]
        StubDataQuality(),  # type: ignore[arg-type]
        StubBooks(),  # type: ignore[arg-type]
    )


async def test_plan_snapshot_from_persisted_row(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    plan = await _seed_plan(session_factory, pk, candidate.candidate_id)
    row = (await RiskPlanRepository(session_factory).plans_for_candidate(candidate.candidate_id))[0]
    snapshot = plan_snapshot_from_record(row)
    assert snapshot is not None
    assert snapshot.plan_id == plan.plan_id and snapshot.status == "ELIGIBLE"
    assert snapshot.entry_low == 99.5 and snapshot.entry_high == 99.6
    assert snapshot.invalidation_price == 96.45
    assert snapshot.target_prices == (107.0, 109.0)
    assert snapshot.expiry_at.tzinfo is not None


def test_plan_snapshot_rejects_incomplete_payload() -> None:
    incomplete = SimpleNamespace(payload={"model_reference_levels": {"entry_zone": [1.0]}})
    assert plan_snapshot_from_record(incomplete) is None  # type: ignore[arg-type]
    empty = SimpleNamespace(payload=None)
    assert plan_snapshot_from_record(empty) is None  # type: ignore[arg-type]


async def test_signal_snapshot_roundtrip(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = SignalLifecycleRepository(session_factory)
    new = _new_signal(pk)
    assert await repo.insert_new(new, "sev-1")
    row = await repo.get(new.signal_id)
    assert row is not None
    snapshot = signal_snapshot_from_record(row)
    assert snapshot.signal_id == new.signal_id
    assert snapshot.state.value == "WATCHING_ENTRY" and snapshot.state_version == 2
    assert snapshot.target_prices == new.target_prices
    assert snapshot.expires_at.tzinfo is not None


async def test_only_closed_candles_are_used(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    builder = _builder(session_factory)
    repo = CandleRepository(session_factory)
    forming = CandleData(
        timeframe=Timeframe.M5,
        open_time=JOB_NOW.replace(minute=0),  # closes 12:05 - still open at 12:01
        open=Decimal("100"),
        high=Decimal("120"),
        low=Decimal("90"),
        close=Decimal("119"),
        volume=Decimal("5"),
        trade_count=3,
    )
    await repo.upsert_candles(pk, [forming])
    market = await builder.market_state(pk, 1, JOB_NOW)
    assert market.last_closed_5m_close is None  # the forming candle is invisible

    closed = CandleData(
        timeframe=Timeframe.M5,
        open_time=JOB_NOW.replace(hour=11, minute=55),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("5"),
        trade_count=3,
    )
    await repo.upsert_candles(pk, [closed])
    market = await builder.market_state(pk, 1, JOB_NOW)
    assert market.last_closed_5m_close == 100.5
    assert market.last_closed_5m_close_time == JOB_NOW.replace(minute=0)


async def test_market_state_without_services_is_conservative(session_factory) -> None:
    builder = SignalLifecycleContextBuilder(
        SIGNAL_SETTINGS,
        RiskPlanRepository(session_factory),
        SetupCandidateRepository(session_factory),
        CandleRepository(session_factory),
        FeatureRepository(session_factory),
        InstrumentRiskRepository(session_factory),
        None,
        None,
        None,
    )
    market = await builder.market_state(1, 1, JOB_NOW)
    assert market.mark_price is None and not market.bbo_fresh and not market.book_fresh
    assert market.data_quality_status == "UNKNOWN" and not market.data_quality_ok


async def test_opposing_structure_events_filtered_by_direction_and_time(
    session_factory,
) -> None:
    pk = await _seed_instrument(session_factory)
    builder = _builder(session_factory)
    features = FeatureRepository(session_factory)
    entry_at = JOB_NOW - timedelta(minutes=30)

    def event(kind: StructureEventType, at, detail: str) -> StructureEvent:
        return StructureEvent(
            event_type=kind,
            timeframe=Timeframe.M15,
            price=103.0,
            reference_swing_id=None,
            confirm_close_time=at,
            detail=detail,
        )

    await features.add_structure_events(
        pk,
        [
            event(
                StructureEventType.CHANGE_OF_CHARACTER,
                entry_at - timedelta(minutes=10),  # BEFORE entry - ignored
                "bearish ChoCh: close below last confirmed higher low",
            ),
            event(
                StructureEventType.CHANGE_OF_CHARACTER,
                entry_at + timedelta(minutes=5),
                "bullish ChoCh: close above last confirmed lower high",  # same side
            ),
            event(
                StructureEventType.BREAK_OF_STRUCTURE,
                entry_at + timedelta(minutes=6),
                "continuation",  # not a ChoCh
            ),
            event(
                StructureEventType.CHANGE_OF_CHARACTER,
                entry_at + timedelta(minutes=10),
                "bearish ChoCh: close below last confirmed higher low",  # OPPOSING
            ),
        ],
        "1.0.0",
    )
    signal = signal_snapshot_from_record(await _persisted_signal(session_factory, pk))
    signal = signal.__class__(**{**signal.__dict__, "entry_confirmed_at": entry_at})
    events = await builder.opposing_structure_events(signal)
    assert len(events) == 1
    assert "bearish ChoCh" in events[0]

    pre_entry = signal.__class__(**{**signal.__dict__, "entry_confirmed_at": None})
    assert await builder.opposing_structure_events(pre_entry) == ()


async def _persisted_signal(session_factory, pk: int):
    repo = SignalLifecycleRepository(session_factory)
    new = _new_signal(pk, uuid.uuid4(), uuid.uuid4())
    assert await repo.insert_new(new, "sev-1")
    row = await repo.get(new.signal_id)
    assert row is not None
    return row


async def test_plan_relations_detects_newer_eligible_plan(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    candidate = await _seed_candidate(session_factory, pk)
    own = await _seed_plan(session_factory, pk, candidate.candidate_id)
    builder = _builder(session_factory)
    row = await _persisted_signal(session_factory, pk)
    signal = signal_snapshot_from_record(row)
    signal = signal.__class__(
        **{**signal.__dict__, "plan_id": own.plan_id, "candidate_id": candidate.candidate_id}
    )

    plan, superseding = await builder.plan_relations(signal)
    assert plan is not None and plan.plan_id == own.plan_id
    assert superseding is None

    newer = await _seed_plan(
        session_factory, pk, candidate.candidate_id, dedupe_key="newer-plan-ctx"
    )
    plan, superseding = await builder.plan_relations(signal)
    assert superseding == newer.plan_id
