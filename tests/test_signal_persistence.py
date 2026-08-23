"""Signal lifecycle persistence: creation chain, optimistic locking,
idempotent events, aggregated updates/rejections, leases and recovery.

Runs against sqlite; no network, no Telegram, no account data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from tests.signal_helpers import NOW, SIGNAL_SETTINGS, make_plan
from tests.test_signal_admission import CONFIG_HASH

from app.domain.models import InstrumentMeta
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.signal_event_repository import SignalEventRepository
from app.repositories.signal_lifecycle_repository import (
    SignalLifecycleRepository,
    TransitionOutcome,
)
from app.repositories.signal_rejection_repository import SignalRejectionRepository
from app.repositories.signal_update_repository import SignalUpdateRepository
from app.signals.enums import (
    EntryTriggerType,
    SignalEventType,
    SignalRejectionCode,
    SignalState,
    SignalUpdateType,
)
from app.signals.event_builder import build_transition_event
from app.signals.lifecycle import new_signal_from_plan
from app.signals.models import SignalRejection, TransitionStep
from app.signals.rejection import build_admission_rejection
from app.signals.signal_update_builder import build_update


async def _seed_instrument(session_factory, symbol: str = "BTC-PERP") -> int:
    repo = InstrumentRepository(session_factory)
    meta = InstrumentMeta.from_api({"instrument_id": 1, "symbol": symbol, "category": "crypto"})
    await repo.upsert_discovered([meta], {symbol})
    row = await repo.get_by_symbol(symbol)
    assert row is not None
    return row.id


def _new_signal(pk: int, plan_id=None, candidate_id=None, **plan_overrides):
    plan = make_plan(
        plan_id=plan_id or uuid.uuid4(),
        candidate_id=candidate_id or uuid.uuid4(),
        instrument_pk=pk,
        **plan_overrides,
    )
    return new_signal_from_plan(
        plan,
        trigger=EntryTriggerType.ZONE_TOUCH_AND_5M_CLOSE_CONFIRM,
        settings=SIGNAL_SETTINGS,
        lifecycle_config_hash=CONFIG_HASH,
        approximation_flags=("CANDLE_APPROXIMATED_INTRABAR_ORDER",),
        now=NOW,
        correlation_id="corr-persist",
    )


def _step(
    from_state=SignalState.WATCHING_ENTRY,
    to_state=SignalState.INVALIDATED,
    event=SignalEventType.INVALIDATED,
    priority=90,
) -> TransitionStep:
    return TransitionStep(
        from_state=from_state,
        to_state=to_state,
        event_type=event,
        reason="test transition",
        priority=priority,
    )


def _event(signal_id, step, expected_version: int):
    return build_transition_event(
        signal_id=signal_id,
        step=step,
        expected_state_version=expected_version,
        event_schema_version="sev-1",
        as_of=NOW,
        correlation_id="corr-persist",
    )


# ------------------------------------------------------------------- creation


async def test_insert_new_writes_row_and_creation_event_chain(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = SignalLifecycleRepository(session_factory)
    events = SignalEventRepository(session_factory)
    new = _new_signal(pk)
    assert await repo.insert_new(new, "sev-1")

    row = await repo.get(new.signal_id)
    assert row is not None
    assert row.state == "WATCHING_ENTRY" and row.state_version == 2
    assert row.active_key == new.dedupe_key
    assert row.reference_snapshot["label"].startswith("Interne Modell-Referenzwerte")

    chain = await events.events_for(new.signal_id)
    assert [(event.event_type, event.state_version) for event in chain] == [
        ("CREATED", 0),
        ("ADMITTED", 1),
        ("WATCHING_ENTRY", 2),
    ]
    assert chain[0].from_state is None


async def test_insert_new_dedupes_on_active_key(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = SignalLifecycleRepository(session_factory)
    shared_plan, shared_candidate = uuid.uuid4(), uuid.uuid4()
    first = _new_signal(pk, shared_plan, shared_candidate)
    duplicate = _new_signal(pk, shared_plan, shared_candidate)
    assert first.dedupe_key == duplicate.dedupe_key
    assert await repo.insert_new(first, "sev-1")
    assert not await repo.insert_new(duplicate, "sev-1")  # unique active_key
    assert await repo.active_by_dedupe_key(first.dedupe_key) is not None


# ----------------------------------------------------------------- transitions


async def test_apply_transition_optimistic_locking(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = SignalLifecycleRepository(session_factory)
    new = _new_signal(pk)
    assert await repo.insert_new(new, "sev-1")
    step = _step()

    outcome = await repo.apply_transition(
        new.signal_id, 2, step, _event(new.signal_id, step, 2), as_of=NOW
    )
    assert outcome is TransitionOutcome.APPLIED
    row = await repo.get(new.signal_id)
    assert row is not None and row.state == "INVALIDATED" and row.state_version == 3
    assert row.terminal_at is not None and row.active_key is None

    # a second writer computed against the stale version -> CONFLICT
    outcome = await repo.apply_transition(
        new.signal_id, 2, step, _event(new.signal_id, step, 2), as_of=NOW
    )
    assert outcome is TransitionOutcome.CONFLICT

    missing = await repo.apply_transition(
        uuid.uuid4(), 2, step, _event(uuid.uuid4(), step, 2), as_of=NOW
    )
    assert missing is TransitionOutcome.NOT_FOUND


async def test_terminal_transition_frees_dedupe_slot(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = SignalLifecycleRepository(session_factory)
    shared_plan, shared_candidate = uuid.uuid4(), uuid.uuid4()
    first = _new_signal(pk, shared_plan, shared_candidate)
    assert await repo.insert_new(first, "sev-1")
    step = _step(to_state=SignalState.EXPIRED, event=SignalEventType.EXPIRED, priority=70)
    assert (
        await repo.apply_transition(
            first.signal_id, 2, step, _event(first.signal_id, step, 2), as_of=NOW
        )
        is TransitionOutcome.APPLIED
    )
    # the terminal signal no longer blocks a NEW signal for the same context
    replacement = _new_signal(pk, shared_plan, shared_candidate)
    assert await repo.insert_new(replacement, "sev-1")


async def test_entry_timestamps_are_set_by_transitions(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = SignalLifecycleRepository(session_factory)
    new = _new_signal(pk)
    assert await repo.insert_new(new, "sev-1")
    entry = _step(
        to_state=SignalState.ENTRY_CONFIRMED, event=SignalEventType.ENTRY_CONFIRMED, priority=30
    )
    active = _step(
        from_state=SignalState.ENTRY_CONFIRMED,
        to_state=SignalState.ACTIVE_RESEARCH,
        event=SignalEventType.ACTIVE_RESEARCH,
        priority=10,
    )
    assert (
        await repo.apply_transition(
            new.signal_id, 2, entry, _event(new.signal_id, entry, 2), as_of=NOW
        )
        is TransitionOutcome.APPLIED
    )
    assert (
        await repo.apply_transition(
            new.signal_id, 3, active, _event(new.signal_id, active, 3), as_of=NOW
        )
        is TransitionOutcome.APPLIED
    )
    row = await repo.get(new.signal_id)
    assert row is not None
    assert row.entry_confirmed_at is not None and row.active_research_at is not None
    assert row.terminal_at is None and row.active_key is not None


async def test_event_insert_is_idempotent(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = SignalLifecycleRepository(session_factory)
    events = SignalEventRepository(session_factory)
    new = _new_signal(pk)
    assert await repo.insert_new(new, "sev-1")
    step = _step()
    payload = _event(new.signal_id, step, 2)
    assert await events.add_event(dict(payload)) is True
    assert await events.add_event(dict(payload)) is False  # retry deduplicated
    rows = await events.events_for(new.signal_id)
    assert len([row for row in rows if row.event_type == "INVALIDATED"]) == 1


# --------------------------------------------------------------------- updates


async def test_updates_aggregate_inside_dedupe_window(session_factory) -> None:
    repo = SignalUpdateRepository(session_factory)
    signal_id = uuid.uuid4()

    def payload(at: datetime):
        return build_update(
            signal_id=signal_id,
            update_type=SignalUpdateType.EXPIRY_WARNING,
            detail="research window ends soon",
            update_schema_version="suv-1",
            as_of=at,
            dedupe_window_seconds=900,
        )

    base = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    assert await repo.add_update(payload(base)) is True
    assert await repo.add_update(payload(base + timedelta(seconds=30))) is False
    rows = await repo.updates_for(signal_id)
    assert len(rows) == 1 and rows[0].count == 2
    assert rows[0].detail["text"].startswith("Research Lifecycle:")

    assert await repo.add_update(payload(base + timedelta(seconds=900))) is True
    assert len(await repo.updates_for(signal_id)) == 2


# ------------------------------------------------------------------ rejections


async def test_rejections_aggregate_by_context_code_and_bucket(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = SignalRejectionRepository(session_factory)
    plan = make_plan(instrument_pk=pk)

    def rejection(at: datetime) -> SignalRejection:
        return build_admission_rejection(
            plan,
            SignalRejectionCode.SESSION_NOT_ALLOWED,
            "session blocked",
            lifecycle_model_version="1.0.0",
            lifecycle_config_hash=CONFIG_HASH,
            as_of=at,
        )

    assert await repo.record_rejection(rejection(NOW), 300) is True
    assert await repo.record_rejection(rejection(NOW + timedelta(seconds=30)), 300) is False
    rows = await repo.recent()
    assert len(rows) == 1 and rows[0].count == 2
    assert rows[0].primary_code == "SESSION_NOT_ALLOWED"

    counts = await repo.counts_by_code()
    assert counts["SESSION_NOT_ALLOWED"] == 2
    assert await repo.count_since(NOW - timedelta(minutes=1)) == 2


# ---------------------------------------------------------------------- leases


async def test_lease_claim_release_and_expiry_recovery(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = SignalLifecycleRepository(session_factory)
    new = _new_signal(pk)
    assert await repo.insert_new(new, "sev-1")

    assert await repo.claim(new.signal_id, "worker-a", NOW, 60)
    assert await repo.claim(new.signal_id, "worker-a", NOW, 60)  # re-entrant
    assert not await repo.claim(new.signal_id, "worker-b", NOW, 60)  # held

    # crash recovery: after expiry another worker reclaims the signal
    later = NOW + timedelta(seconds=61)
    assert await repo.expired_lease_count(later) == 1
    assert await repo.claim(new.signal_id, "worker-b", later, 60)

    await repo.release(new.signal_id, "worker-b")
    assert await repo.claim(new.signal_id, "worker-c", later, 60)


# ----------------------------------------------------------------------- reads


async def test_read_api(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = SignalLifecycleRepository(session_factory)
    first = _new_signal(pk)
    second = _new_signal(pk)
    assert await repo.insert_new(first, "sev-1")
    assert await repo.insert_new(second, "sev-1")

    assert {row.id for row in await repo.non_terminal()} == {
        first.signal_id,
        second.signal_id,
    }
    assert await repo.active_count_for_instrument(pk) == 2
    assert await repo.signal_exists_for_plan(first.plan_id)
    assert not await repo.signal_exists_for_plan(uuid.uuid4())
    assert await repo.counts_by_state() == {"WATCHING_ENTRY": 2}
    assert len(await repo.recent(limit=1)) == 1

    step = _step(to_state=SignalState.EXPIRED, event=SignalEventType.EXPIRED, priority=70)
    assert (
        await repo.apply_transition(
            first.signal_id, 2, step, _event(first.signal_id, step, 2), as_of=NOW
        )
        is TransitionOutcome.APPLIED
    )
    assert {row.id for row in await repo.non_terminal()} == {second.signal_id}
    counts = await repo.counts_by_state()
    assert counts == {"WATCHING_ENTRY": 1, "EXPIRED": 1}


async def test_monitoring_fields_update_and_degraded_marker(session_factory) -> None:
    pk = await _seed_instrument(session_factory)
    repo = SignalLifecycleRepository(session_factory)
    new = _new_signal(pk)
    assert await repo.insert_new(new, "sev-1")

    degraded_since = NOW - timedelta(seconds=30)
    await repo.update_monitoring_fields(
        new.signal_id,
        last_evaluated_at=NOW,
        last_market_data_at=NOW - timedelta(seconds=5),
        data_quality_status="DEGRADED",
        session_state="CRYPTO_24_7",
        data_degraded_since=degraded_since,
        clear_degraded=False,
    )
    row = await repo.get(new.signal_id)
    assert row is not None and row.data_degraded_since is not None
    assert row.last_data_quality_status == "DEGRADED"

    await repo.update_monitoring_fields(
        new.signal_id,
        last_evaluated_at=NOW,
        last_market_data_at=NOW,
        data_quality_status="HEALTHY",
        session_state="CRYPTO_24_7",
        data_degraded_since=None,
        clear_degraded=True,
    )
    row = await repo.get(new.signal_id)
    assert row is not None and row.data_degraded_since is None
