"""Signal state machine and lifecycle engine determinism.

Every allowed transition is exercised, every non-listed pair rejected;
terminal states stay immutable; the engine applies at most one final state
per cycle with strictly-distinct priorities.  Pure logic - no I/O.
"""

from __future__ import annotations

import pytest
from tests.signal_helpers import (
    NOW,
    SIGNAL_SETTINGS,
    bearish_signal,
    make_context,
    make_market,
    make_signal,
    make_signal_settings,
    post_entry_context,
    replace,
)

import app.signals.lifecycle_engine as engine_module
from app.signals.enums import SignalEventType, SignalState, SignalUpdateType
from app.signals.lifecycle_engine import evaluate_signal
from app.signals.models import MonitorFinding
from app.signals.state_machine import (
    EVENT_FOR_STATE,
    TERMINAL_STATES,
    TRANSITIONS,
    InvalidTransitionError,
    can_transition,
    is_post_entry,
    is_terminal,
    transition_map_document,
    validate_transition,
)

# ------------------------------------------------------------- state machine


def test_transition_map_covers_all_states() -> None:
    assert set(TRANSITIONS) == set(SignalState)


def test_every_allowed_transition_validates() -> None:
    for from_state, targets in TRANSITIONS.items():
        for to_state in targets:
            assert can_transition(from_state, to_state)
            validate_transition(from_state, to_state)  # must not raise


def test_every_non_listed_transition_is_rejected() -> None:
    for from_state in SignalState:
        for to_state in SignalState:
            if to_state in TRANSITIONS[from_state]:
                continue
            assert not can_transition(from_state, to_state)
            with pytest.raises(InvalidTransitionError):
                validate_transition(from_state, to_state)


def test_terminal_states_are_immutable() -> None:
    assert TERMINAL_STATES == {
        SignalState.TECHNICAL_EXIT,
        SignalState.INVALIDATED,
        SignalState.EXPIRED,
        SignalState.SUPERSEDED,
        SignalState.REJECTED,
        SignalState.DATA_INVALID,
    }
    for state in TERMINAL_STATES:
        assert is_terminal(state)
        assert TRANSITIONS[state] == frozenset()


def test_paused_is_dormant_not_terminal() -> None:
    assert not is_terminal(SignalState.PAUSED)
    assert TRANSITIONS[SignalState.PAUSED] == frozenset(
        {SignalState.EXPIRED, SignalState.SUPERSEDED, SignalState.DATA_INVALID}
    )


def test_invalidated_is_illegal_after_target_2() -> None:
    assert not can_transition(SignalState.TARGET_2_REACHED, SignalState.INVALIDATED)
    assert not can_transition(SignalState.TRAILING_RESEARCH, SignalState.INVALIDATED)
    assert can_transition(SignalState.TARGET_2_REACHED, SignalState.TECHNICAL_EXIT)


def test_post_entry_classification() -> None:
    for state in (
        SignalState.ENTRY_CONFIRMED,
        SignalState.ACTIVE_RESEARCH,
        SignalState.TARGET_1_REACHED,
        SignalState.TARGET_2_REACHED,
        SignalState.TRAILING_RESEARCH,
    ):
        assert is_post_entry(state)
    assert not is_post_entry(SignalState.WATCHING_ENTRY)


def test_event_for_every_reachable_state() -> None:
    assert set(EVENT_FOR_STATE) == set(SignalState) - {SignalState.DRAFT}


def test_transition_map_document_serializable() -> None:
    document = transition_map_document()
    assert document["WATCHING_ENTRY"] == sorted(
        target.value for target in TRANSITIONS[SignalState.WATCHING_ENTRY]
    )
    assert document["INVALIDATED"] == []


# ------------------------------------------------------------------- engine


def _entry_market():
    """Fresh market confirming the default zone-touch + 5m close trigger."""
    return make_market(
        mark_price=100.6,
        best_bid=100.5,
        best_ask=100.7,
        last_closed_5m_close=100.6,
        last_closed_5m_low=99.9,
        last_closed_5m_high=100.9,
    )


def test_neutral_market_produces_no_transitions() -> None:
    result = evaluate_signal(make_context(), SIGNAL_SETTINGS)
    assert result.transitions == ()
    assert result.final_state is None


def test_entry_chain_two_steps_one_final_state() -> None:
    result = evaluate_signal(make_context(market=_entry_market()), SIGNAL_SETTINGS)
    assert [step.to_state for step in result.transitions] == [
        SignalState.ENTRY_CONFIRMED,
        SignalState.ACTIVE_RESEARCH,
    ]
    assert result.final_state is SignalState.ACTIVE_RESEARCH
    assert result.transitions[0].event_type is SignalEventType.ENTRY_CONFIRMED
    assert "not a fill" in result.transitions[0].reason


def test_terminal_signal_is_never_evaluated() -> None:
    context = make_context(signal=make_signal(state=SignalState.INVALIDATED))
    result = evaluate_signal(context, SIGNAL_SETTINGS)
    assert result.transitions == () and result.updates == ()


def test_invalidation_outranks_entry_in_same_cycle() -> None:
    # entry conditions met via candle, but mark broke the invalidation level
    market = replace(_entry_market(), mark_price=97.5, best_bid=97.4, best_ask=97.6)
    result = evaluate_signal(make_context(market=market), SIGNAL_SETTINGS)
    assert result.final_state is SignalState.INVALIDATED
    assert len(result.transitions) == 1


def test_invalidation_outranks_target_in_same_cycle() -> None:
    # active research: 5m close beyond target 1 AND BBO beyond invalidation
    market = make_market(
        mark_price=104.6,
        best_bid=97.4,  # conservative exit side broke the level
        best_ask=104.8,
        last_closed_5m_close=104.5,
        last_closed_5m_low=97.0,
        last_closed_5m_high=104.9,
    )
    result = evaluate_signal(post_entry_context(market=market), SIGNAL_SETTINGS)
    assert result.final_state is SignalState.INVALIDATED
    outranked = [entry for entry in result.observed if "outranked" in entry]
    assert any("target" in entry for entry in outranked)


def test_data_invalid_has_highest_priority() -> None:
    signal = make_signal(
        state=SignalState.ACTIVE_RESEARCH,
        state_version=4,
        entry_confirmed_at=NOW,
        data_degraded_since=NOW.replace(hour=11),  # degraded for an hour
    )
    market = make_market(
        data_quality_status="STALE",
        data_quality_ok=False,
        mark_price=97.0,  # invalidation would fire too
        bbo_fresh=True,
        best_bid=97.0,
        best_ask=97.2,
    )
    result = evaluate_signal(make_context(signal=signal, market=market), SIGNAL_SETTINGS)
    assert result.final_state is SignalState.DATA_INVALID


def test_paused_signal_only_housekeeping_monitors_act() -> None:
    signal = make_signal(state=SignalState.PAUSED, state_version=5)
    market = make_market(mark_price=97.0, best_bid=96.9, best_ask=97.1)
    result = evaluate_signal(make_context(signal=signal, market=market), SIGNAL_SETTINGS)
    assert result.transitions == ()  # invalidation may not touch a paused signal

    expired = replace(signal, expires_at=NOW.replace(hour=11))
    result = evaluate_signal(make_context(signal=expired, market=market), SIGNAL_SETTINGS)
    assert result.final_state is SignalState.EXPIRED  # expiry still applies


def test_target_2_auto_progresses_to_trailing() -> None:
    context = post_entry_context(
        signal=make_signal(
            state=SignalState.TARGET_2_REACHED,
            state_version=6,
            entry_confirmed_at=NOW,
            expires_at=NOW.replace(hour=14),
        )
    )
    result = evaluate_signal(context, SIGNAL_SETTINGS)
    assert result.final_state is SignalState.TRAILING_RESEARCH
    assert result.transitions[0].priority == SIGNAL_SETTINGS.monitor_event_priority_json["INFO"]


def test_bearish_entry_chain() -> None:
    market = make_market(
        mark_price=104.4,
        best_bid=104.3,
        best_ask=104.5,
        last_closed_5m_close=104.4,
        last_closed_5m_low=104.1,
        last_closed_5m_high=105.1,
    )
    result = evaluate_signal(make_context(signal=bearish_signal(), market=market), SIGNAL_SETTINGS)
    assert result.final_state is SignalState.ACTIVE_RESEARCH


def test_engine_is_deterministic() -> None:
    context = make_context(market=_entry_market())
    first = evaluate_signal(context, SIGNAL_SETTINGS)
    second = evaluate_signal(context, SIGNAL_SETTINGS)
    assert first == second


def test_invalid_transition_proposal_is_suppressed(monkeypatch) -> None:
    def bogus_monitor(context, settings):
        return MonitorFinding(
            monitor="bogus",
            priority_key="TARGET_1_REACHED",
            to_state=SignalState.TARGET_1_REACHED,  # illegal from WATCHING_ENTRY
            event_type=SignalEventType.TARGET_1_REACHED,
            reason="bogus",
        )

    monkeypatch.setattr(engine_module, "_MONITORS", (bogus_monitor,))
    result = evaluate_signal(make_context(), SIGNAL_SETTINGS)
    assert result.transitions == ()
    assert any("invalid transition" in entry for entry in result.suppressed_findings)


def test_unknown_priority_key_is_skipped_with_warning(monkeypatch) -> None:
    def bogus_monitor(context, settings):
        return MonitorFinding(
            monitor="bogus",
            priority_key="NOT_A_KEY",
            to_state=SignalState.EXPIRED,
            event_type=SignalEventType.EXPIRED,
            reason="bogus",
        )

    monkeypatch.setattr(engine_module, "_MONITORS", (bogus_monitor,))
    result = evaluate_signal(make_context(), SIGNAL_SETTINGS)
    assert result.transitions == ()
    assert result.warnings


def test_priority_map_validator_rejects_bad_configs() -> None:
    with pytest.raises(ValueError, match="strictly distinct"):
        make_signal_settings(
            monitor_event_priority_json={
                "DATA_INVALID": 100,
                "INVALIDATED": 90,
                "SUPERSEDED": 90,  # duplicate priority
                "EXPIRED": 70,
                "PAUSED": 65,
                "TECHNICAL_EXIT": 60,
                "TARGET_2_REACHED": 50,
                "TARGET_1_REACHED": 40,
                "ENTRY_CONFIRMED": 30,
                "INFO": 10,
            }
        )
    with pytest.raises(ValueError, match="DATA_INVALID"):
        make_signal_settings(
            monitor_event_priority_json={
                "DATA_INVALID": 10,  # must rank highest
                "INVALIDATED": 90,
                "SUPERSEDED": 80,
                "EXPIRED": 70,
                "PAUSED": 65,
                "TECHNICAL_EXIT": 60,
                "TARGET_2_REACHED": 50,
                "TARGET_1_REACHED": 40,
                "ENTRY_CONFIRMED": 30,
                "INFO": 5,
            }
        )


def test_telegram_output_flag_is_hard_rejected() -> None:
    with pytest.raises(ValueError, match=r"[Tt]elegram"):
        make_signal_settings(lifecycle_telegram_output_enabled=True)


def test_updates_are_labelled_research_lifecycle() -> None:
    market = make_market(best_bid=100.4, best_ask=100.6)  # touch without close confirm
    result = evaluate_signal(
        make_context(
            signal=make_signal(),
            market=replace(
                market,
                last_closed_5m_close=None,
                last_closed_5m_low=None,
                last_closed_5m_high=None,
                last_closed_5m_close_time=None,
            ),
        ),
        SIGNAL_SETTINGS,
    )
    assert result.transitions == ()
    assert any(
        update_type is SignalUpdateType.ENTRY_CONDITION_OBSERVED
        for update_type, _ in result.updates
    )
