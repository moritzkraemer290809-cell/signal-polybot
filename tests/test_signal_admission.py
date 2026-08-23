"""Admission gates: an ELIGIBLE plan is never adopted unchecked.

Every gate produces its structured rejection code in spec order; the happy
path yields the candidate-type trigger and candle-approximation flags.
Also covers dedupe/idempotency key determinism and NewSignal construction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from tests.signal_helpers import (
    NOW,
    SIGNAL_SETTINGS,
    make_candidate_state,
    make_market,
    make_plan,
    make_signal_settings,
    replace,
)

from app.signals.admission import AdmissionContext, admit
from app.signals.enums import EntryTriggerType, SignalRejectionCode, SignalState
from app.signals.idempotency import event_idempotency_key, update_idempotency_key
from app.signals.lifecycle import new_signal_from_plan, snapshot_from_new_signal
from app.signals.version import lifecycle_configuration_hash, signal_dedupe_key

CONFIG_HASH = lifecycle_configuration_hash(SIGNAL_SETTINGS)


def make_admission(**overrides: Any) -> AdmissionContext:
    defaults: dict[str, Any] = {
        "plan": make_plan(),
        "candidate": make_candidate_state(),
        "market": make_market(),
        "session_state": "CRYPTO_24_7",
        "session_allowed": True,
        "watchlist_active": True,
        "market_quality_score": 85,
        "bot_paused": False,
        "active_signal_count": 0,
        "duplicate_exists": False,
        "instrument_snapshot_age_seconds": 30.0,
        "as_of": NOW,
    }
    defaults.update(overrides)
    return AdmissionContext(**defaults)


def test_happy_path_admits_with_candidate_trigger_and_flags() -> None:
    decision = admit(make_admission(), SIGNAL_SETTINGS)
    assert decision.admitted
    assert decision.trigger is EntryTriggerType.RECLAIM_LEVEL_CLOSE_CONFIRM
    assert decision.approximation_flags == ("CANDLE_APPROXIMATED_INTRABAR_ORDER",)


def test_zone_touch_trigger_carries_no_approximation_flag() -> None:
    relaxed = make_signal_settings(entry_close_confirmation_required=False)
    decision = admit(make_admission(), relaxed)
    assert decision.admitted and decision.trigger is EntryTriggerType.ZONE_TOUCH
    assert decision.approximation_flags == ()


def test_every_admission_gate_in_order() -> None:
    cases = [
        (make_admission(bot_paused=True), SignalRejectionCode.BOT_PAUSED),
        (
            make_admission(plan=make_plan(status="EXPIRED")),
            SignalRejectionCode.PLAN_NOT_ELIGIBLE,
        ),
        (
            make_admission(plan=make_plan(expiry_at=NOW - timedelta(seconds=1))),
            SignalRejectionCode.PLAN_EXPIRED,
        ),
        (
            make_admission(plan=make_plan(as_of=NOW - timedelta(hours=1))),
            SignalRejectionCode.PLAN_SNAPSHOT_STALE,
        ),
        (make_admission(candidate=None), SignalRejectionCode.CANDIDATE_NOT_CONFIRMED),
        (
            make_admission(candidate=make_candidate_state(state="SUPERSEDED")),
            SignalRejectionCode.CANDIDATE_SUPERSEDED,
        ),
        (
            make_admission(candidate=make_candidate_state(state="EXPIRED")),
            SignalRejectionCode.CANDIDATE_EXPIRED,
        ),
        (
            make_admission(candidate=make_candidate_state(expiry_at=NOW - timedelta(seconds=1))),
            SignalRejectionCode.CANDIDATE_EXPIRED,
        ),
        (
            make_admission(candidate=make_candidate_state(state="DETECTED")),
            SignalRejectionCode.CANDIDATE_NOT_CONFIRMED,
        ),
        (
            make_admission(candidate=make_candidate_state(as_of=NOW - timedelta(hours=2))),
            SignalRejectionCode.CANDIDATE_SNAPSHOT_STALE,
        ),
        (make_admission(watchlist_active=False), SignalRejectionCode.WATCHLIST_NOT_ACTIVE),
        (make_admission(session_allowed=False), SignalRejectionCode.SESSION_NOT_ALLOWED),
        (
            make_admission(market_quality_score=None),
            SignalRejectionCode.MARKET_QUALITY_INSUFFICIENT,
        ),
        (
            make_admission(market_quality_score=69),
            SignalRejectionCode.MARKET_QUALITY_INSUFFICIENT,
        ),
        (
            make_admission(
                market=make_market(data_quality_status="DEGRADED", data_quality_ok=False)
            ),
            SignalRejectionCode.DATA_QUALITY_INSUFFICIENT,
        ),
        (
            make_admission(market=make_market(book_fresh=False)),
            SignalRejectionCode.ORDERBOOK_NOT_FRESH,
        ),
        (
            make_admission(market=make_market(book_resyncing=True)),
            SignalRejectionCode.ORDERBOOK_NOT_FRESH,
        ),
        (
            make_admission(instrument_snapshot_age_seconds=None),
            SignalRejectionCode.INSTRUMENT_SNAPSHOT_STALE,
        ),
        (
            make_admission(instrument_snapshot_age_seconds=901.0),
            SignalRejectionCode.INSTRUMENT_SNAPSHOT_STALE,
        ),
        (make_admission(duplicate_exists=True), SignalRejectionCode.SIGNAL_DUPLICATE),
        (
            make_admission(active_signal_count=2),
            SignalRejectionCode.ACTIVE_SIGNAL_LIMIT_REACHED,
        ),
        (
            make_admission(plan=make_plan(target_prices=())),
            SignalRejectionCode.ADMISSION_CONFIGURATION_INVALID,
        ),
    ]
    for context, expected in cases:
        decision = admit(context, SIGNAL_SETTINGS)
        assert not decision.admitted, expected
        assert decision.code is expected, f"expected {expected}, got {decision.code}"


def test_degraded_data_admitted_only_with_explicit_opt_in() -> None:
    degraded = make_admission(
        market=make_market(data_quality_status="DEGRADED", data_quality_ok=False)
    )
    assert admit(degraded, SIGNAL_SETTINGS).code is SignalRejectionCode.DATA_QUALITY_INSUFFICIENT
    opt_in = make_signal_settings(lifecycle_allow_degraded_data=True)
    assert admit(degraded, opt_in).admitted


# ----------------------------------------------------- construction/versioning


def test_new_signal_from_plan_copies_immutable_reference_values() -> None:
    plan = make_plan()
    new = new_signal_from_plan(
        plan,
        trigger=EntryTriggerType.RECLAIM_LEVEL_CLOSE_CONFIRM,
        settings=SIGNAL_SETTINGS,
        lifecycle_config_hash=CONFIG_HASH,
        approximation_flags=("CANDLE_APPROXIMATED_INTRABAR_ORDER",),
        now=NOW,
        correlation_id="corr",
    )
    assert new.state is SignalState.WATCHING_ENTRY
    assert new.expires_at == plan.expiry_at  # plan window bounds the research window
    assert new.entry_low == plan.entry_low and new.invalidation_price == plan.invalidation_price
    assert new.reference_snapshot["label"].startswith("Interne Modell-Referenzwerte")
    assert "keine Handelsanweisung" in new.reference_snapshot["label"]

    snapshot = snapshot_from_new_signal(new)
    assert snapshot.state_version == 2  # after the CREATED/ADMITTED/WATCHING chain
    assert snapshot.state is SignalState.WATCHING_ENTRY


def test_dedupe_key_is_deterministic_and_config_sensitive() -> None:
    key = signal_dedupe_key(
        plan_id="p1",
        candidate_id="c1",
        lifecycle_model_version="1.0.0",
        lifecycle_config_hash=CONFIG_HASH,
    )
    assert key == signal_dedupe_key(
        plan_id="p1",
        candidate_id="c1",
        lifecycle_model_version="1.0.0",
        lifecycle_config_hash=CONFIG_HASH,
    )
    assert key != signal_dedupe_key(
        plan_id="p2",
        candidate_id="c1",
        lifecycle_model_version="1.0.0",
        lifecycle_config_hash=CONFIG_HASH,
    )
    other_config = lifecycle_configuration_hash(make_signal_settings(entry_zone_tolerance_bps=7.0))
    assert other_config != CONFIG_HASH
    assert key != signal_dedupe_key(
        plan_id="p1",
        candidate_id="c1",
        lifecycle_model_version="1.0.0",
        lifecycle_config_hash=other_config,
    )


def test_event_idempotency_key_binds_expected_version() -> None:
    signal_id = str(uuid.uuid4())
    key = event_idempotency_key(
        signal_id=signal_id,
        event_type="INVALIDATED",
        to_state="INVALIDATED",
        expected_state_version=4,
    )
    assert key == event_idempotency_key(
        signal_id=signal_id,
        event_type="INVALIDATED",
        to_state="INVALIDATED",
        expected_state_version=4,
    )
    assert key != event_idempotency_key(
        signal_id=signal_id,
        event_type="INVALIDATED",
        to_state="INVALIDATED",
        expected_state_version=5,
    )


def test_update_idempotency_key_buckets_by_window() -> None:
    signal_id = str(uuid.uuid4())
    base = datetime(2026, 8, 23, 12, 0, 5, tzinfo=UTC)
    inside = update_idempotency_key(
        signal_id=signal_id, update_type="EXPIRY_WARNING", as_of=base, window_seconds=900
    )
    same_bucket = update_idempotency_key(
        signal_id=signal_id,
        update_type="EXPIRY_WARNING",
        as_of=base + timedelta(seconds=60),
        window_seconds=900,
    )
    next_bucket = update_idempotency_key(
        signal_id=signal_id,
        update_type="EXPIRY_WARNING",
        as_of=base + timedelta(seconds=900),
        window_seconds=900,
    )
    assert inside == same_bucket
    assert inside != next_bucket


def test_admission_rejection_never_mutates_inputs() -> None:
    plan = make_plan()
    context = make_admission(plan=plan, duplicate_exists=True)
    before = replace(plan)
    decision = admit(context, SIGNAL_SETTINGS)
    assert not decision.admitted
    assert plan == before  # immutable snapshots, no side effects
