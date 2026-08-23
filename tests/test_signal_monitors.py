"""Per-monitor behavior: entry triggers, invalidation, targets, structure
exits, sessions, data quality, plan/candidate context, expiry, supersede.

Bullish and bearish variants; conservative references only (mark/BBO/closed
candles); stale data never confirms; no monitor ever claims a fill.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

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

from app.signals.data_quality_monitor import check_data_quality
from app.signals.entry_monitor import check_entry
from app.signals.entry_trigger import CANDLE_APPROXIMATION_FLAG, default_trigger_for
from app.signals.enums import EntryTriggerType, SignalState, SignalUpdateType
from app.signals.expiry_monitor import check_expiry
from app.signals.invalidation_monitor import check_invalidation
from app.signals.plan_monitor import check_plan
from app.signals.session_monitor import check_session
from app.signals.structure_exit_monitor import check_structure_exit
from app.signals.supersede_monitor import check_supersede
from app.signals.target_monitor import check_targets

# --------------------------------------------------------------- entry monitor


def test_zone_touch_trigger_requires_fresh_bbo() -> None:
    signal = make_signal(entry_trigger=EntryTriggerType.ZONE_TOUCH)
    market = make_market(best_bid=100.4, best_ask=100.6)  # ask inside zone
    finding = check_entry(make_context(signal=signal, market=market), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.ENTRY_CONFIRMED
    assert finding.metadata["source"] == "BBO"
    assert finding.approximation_flags == ()

    stale = replace(market, bbo_fresh=False, mark_price=102.5)
    finding = check_entry(make_context(signal=signal, market=stale), SIGNAL_SETTINGS)
    assert finding is None or finding.to_state is None  # stale BBO never confirms


def test_zone_touch_and_close_confirm_needs_closed_candle() -> None:
    signal = make_signal()  # ZONE_TOUCH_AND_5M_CLOSE_CONFIRM
    market = make_market(
        best_bid=100.4,
        best_ask=100.6,
        last_closed_5m_close=None,
        last_closed_5m_low=None,
        last_closed_5m_high=None,
        last_closed_5m_close_time=None,
    )
    finding = check_entry(make_context(signal=signal, market=market), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is None  # observed, not confirmed
    assert finding.updates[0][0] is SignalUpdateType.ENTRY_CONDITION_OBSERVED

    confirmed = replace(
        market,
        last_closed_5m_close=100.6,
        last_closed_5m_low=99.9,
        last_closed_5m_high=100.9,
        last_closed_5m_close_time=NOW - timedelta(minutes=1),
    )
    finding = check_entry(make_context(signal=signal, market=confirmed), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.ENTRY_CONFIRMED
    assert CANDLE_APPROXIMATION_FLAG in finding.approximation_flags
    assert "not a fill" in finding.reason


def test_reclaim_trigger_bullish_and_bearish() -> None:
    signal = make_signal(entry_trigger=EntryTriggerType.RECLAIM_LEVEL_CLOSE_CONFIRM)
    market = make_market(
        mark_price=100.6,
        best_bid=100.5,
        best_ask=100.7,
        last_closed_5m_close=100.4,  # >= anchor (entry_low 100), <= high
        last_closed_5m_low=99.7,
        last_closed_5m_high=100.8,
    )
    finding = check_entry(make_context(signal=signal, market=market), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.ENTRY_CONFIRMED

    bearish = bearish_signal(entry_trigger=EntryTriggerType.RECLAIM_LEVEL_CLOSE_CONFIRM)
    market = make_market(
        mark_price=104.6,
        best_bid=104.5,
        best_ask=104.7,
        last_closed_5m_close=104.6,  # <= anchor (entry_high 105), >= low
        last_closed_5m_low=104.0,
        last_closed_5m_high=105.3,
    )
    finding = check_entry(make_context(signal=bearish, market=market), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.ENTRY_CONFIRMED


def test_retest_and_breakout_triggers() -> None:
    retest = make_signal(entry_trigger=EntryTriggerType.RETEST_CONFIRM)
    market = make_market(
        mark_price=100.7,
        best_bid=100.6,
        best_ask=100.8,
        last_closed_5m_close=100.5,  # retested zone, closed back above anchor
        last_closed_5m_low=100.2,
        last_closed_5m_high=100.9,
    )
    finding = check_entry(make_context(signal=retest, market=market), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.ENTRY_CONFIRMED

    breakout = make_signal(
        entry_trigger=EntryTriggerType.BREAKOUT_CLOSE_CONFIRM,
        candidate_type="RANGE_BREAKOUT_UP",
    )
    finding = check_entry(make_context(signal=breakout, market=market), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.ENTRY_CONFIRMED


def test_entry_blocked_by_pause_session_data_expiry_and_level() -> None:
    signal = make_signal()
    market = make_market(
        best_bid=100.4,
        best_ask=100.6,
        last_closed_5m_close=100.6,
        last_closed_5m_low=99.9,
        last_closed_5m_high=100.9,
    )
    confirmable = make_context(signal=signal, market=market)
    assert check_entry(confirmable, SIGNAL_SETTINGS) is not None

    assert check_entry(replace(confirmable, bot_paused=True), SIGNAL_SETTINGS) is None
    assert check_entry(replace(confirmable, session_allowed=False), SIGNAL_SETTINGS) is None
    degraded = replace(market, data_quality_ok=False, data_quality_status="DEGRADED")
    assert check_entry(replace(confirmable, market=degraded), SIGNAL_SETTINGS) is None
    resync = replace(market, book_resyncing=True)
    assert check_entry(replace(confirmable, market=resync), SIGNAL_SETTINGS) is None
    expired = replace(confirmable, signal=make_signal(expires_at=NOW - timedelta(seconds=1)))
    assert check_entry(expired, SIGNAL_SETTINGS) is None
    # conservative side price at/below the invalidation level blocks entry
    broken = replace(market, best_bid=97.9, mark_price=97.9, best_ask=98.1)
    assert check_entry(replace(confirmable, market=broken), SIGNAL_SETTINGS) is None


def test_entry_only_monitors_watching_entry() -> None:
    context = post_entry_context()
    assert check_entry(context, SIGNAL_SETTINGS) is None


def test_default_trigger_mapping() -> None:
    assert (
        default_trigger_for("BULLISH_SWEEP_REVERSAL", SIGNAL_SETTINGS)
        is EntryTriggerType.RECLAIM_LEVEL_CLOSE_CONFIRM
    )
    assert (
        default_trigger_for("RANGE_BREAKOUT_DOWN", SIGNAL_SETTINGS)
        is EntryTriggerType.BREAKOUT_CLOSE_CONFIRM
    )
    assert (
        default_trigger_for("UNKNOWN_TYPE", SIGNAL_SETTINGS)
        is EntryTriggerType.ZONE_TOUCH_AND_5M_CLOSE_CONFIRM
    )
    relaxed = make_signal_settings(entry_close_confirmation_required=False)
    assert default_trigger_for("BULLISH_SWEEP_REVERSAL", relaxed) is EntryTriggerType.ZONE_TOUCH


# -------------------------------------------------------- invalidation monitor


def test_invalidation_via_mark_bbo_and_close() -> None:
    context = make_context(market=make_market(mark_price=97.9))
    finding = check_invalidation(context, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.INVALIDATED
    assert finding.metadata["source"] == "MARK_PRICE"
    assert "never a stop fill" in finding.reason

    context = make_context(market=make_market(best_bid=97.9))
    finding = check_invalidation(context, SIGNAL_SETTINGS)
    assert finding is not None and finding.metadata["source"] == "BBO"

    context = make_context(market=make_market(last_closed_5m_close=97.9))
    finding = check_invalidation(context, SIGNAL_SETTINGS)
    assert finding is not None and finding.metadata["source"] == "5M_CLOSE"


def test_invalidation_bearish_uses_ask_side() -> None:
    signal = bearish_signal(state=SignalState.ACTIVE_RESEARCH, state_version=4)
    market = make_market(best_ask=107.2, best_bid=106.9, mark_price=106.5)
    finding = check_invalidation(make_context(signal=signal, market=market), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.INVALIDATED


def test_invalidation_policies_filter_sources() -> None:
    bbo_only = make_market(best_bid=97.9)  # mark + close stay above the level
    mark_policy = make_signal_settings(invalidation_trigger_default="MARK_PRICE_TOUCH")
    assert check_invalidation(make_context(market=bbo_only), mark_policy) is None
    close_required = make_signal_settings(invalidation_close_confirmation_required=True)
    assert check_invalidation(make_context(market=bbo_only), close_required) is None
    assert check_invalidation(make_context(market=bbo_only), SIGNAL_SETTINGS) is not None


def test_invalidation_needs_fresh_data() -> None:
    market = make_market(
        mark_price=97.0, data_quality_ok=False, data_quality_status="STALE", bbo_fresh=False
    )
    assert check_invalidation(make_context(market=market), SIGNAL_SETTINGS) is None


def test_invalidation_after_target_2_maps_to_technical_exit() -> None:
    signal = make_signal(state=SignalState.TARGET_2_REACHED, state_version=6)
    finding = check_invalidation(
        make_context(signal=signal, market=make_market(mark_price=97.9)), SIGNAL_SETTINGS
    )
    assert finding is not None and finding.to_state is SignalState.TECHNICAL_EXIT
    assert finding.priority_key == "TECHNICAL_EXIT"


# -------------------------------------------------------------- target monitor


def test_targets_fire_via_close_and_are_idempotent() -> None:
    market = make_market(last_closed_5m_close=104.2, last_closed_5m_high=104.5, mark_price=104.1)
    finding = check_targets(post_entry_context(market=market), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.TARGET_1_REACHED
    assert "no fill or realised gain implied" in finding.reason

    already = post_entry_context(
        signal=make_signal(
            state=SignalState.TARGET_1_REACHED, state_version=5, entry_confirmed_at=NOW
        ),
        market=market,
    )
    assert check_targets(already, SIGNAL_SETTINGS) is None  # idempotent


def test_target_2_checked_before_target_1() -> None:
    market = make_market(last_closed_5m_close=107.5, last_closed_5m_high=107.9)
    finding = check_targets(post_entry_context(market=market), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.TARGET_2_REACHED


def test_target_policy_bbo_uses_conservative_side() -> None:
    settings = make_signal_settings(target_trigger_default="BBO_TOUCH")
    market = make_market(best_bid=104.1, best_ask=104.3, last_closed_5m_close=103.0)
    finding = check_targets(post_entry_context(market=market), settings)
    assert finding is not None and finding.metadata["source"] == "BBO"
    # bid below the target must not fire even with the ask beyond it
    market = make_market(best_bid=103.9, best_ask=104.2, last_closed_5m_close=103.0)
    assert check_targets(post_entry_context(market=market), settings) is None


def test_targets_only_from_plan_and_only_when_enabled() -> None:
    no_targets = post_entry_context(
        signal=make_signal(
            state=SignalState.ACTIVE_RESEARCH,
            state_version=4,
            entry_confirmed_at=NOW,
            target_prices=(),
        )
    )
    assert check_targets(no_targets, SIGNAL_SETTINGS) is None
    disabled = make_signal_settings(monitor_targets_enabled=False)
    market = make_market(last_closed_5m_close=104.2)
    assert check_targets(post_entry_context(market=market), disabled) is None
    assert check_targets(make_context(market=market), SIGNAL_SETTINGS) is None  # pre-entry


# ------------------------------------------------------- structure exit monitor


def test_opposing_structure_event_triggers_technical_exit() -> None:
    context = post_entry_context(
        opposing_structure_events=("15m bearish ChoCh @ 103 (confirmed ...)",)
    )
    finding = check_structure_exit(context, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.TECHNICAL_EXIT
    assert "no actual position exit implied" in finding.reason
    assert check_structure_exit(make_context(), SIGNAL_SETTINGS) is None  # pre-entry


def test_breakout_close_back_inside_range_exits() -> None:
    signal = make_signal(
        state=SignalState.ACTIVE_RESEARCH,
        state_version=4,
        entry_confirmed_at=NOW - timedelta(minutes=10),
        candidate_type="RANGE_BREAKOUT_UP",
        expires_at=NOW + timedelta(hours=2),
    )
    market = make_market(last_closed_5m_close=99.5)  # back below anchor, above 98
    finding = check_structure_exit(make_context(signal=signal, market=market), SIGNAL_SETTINGS)
    assert finding is not None and finding.metadata["basis"] == "CLOSE_BACK_INSIDE_RANGE"
    # below the invalidation the invalidation monitor owns the decision
    market = make_market(last_closed_5m_close=97.5)
    assert check_structure_exit(make_context(signal=signal, market=market), SIGNAL_SETTINGS) is None


# ------------------------------------------------------------- session monitor


def test_session_policies() -> None:
    blocked = post_entry_context(session_allowed=False, session_state="EQUITY_CLOSED")
    finding = check_session(blocked, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.EXPIRED  # default policy

    exit_policy = make_signal_settings(monitor_session_end_policy="TECHNICAL_EXIT")
    finding = check_session(blocked, exit_policy)
    assert finding is not None and finding.to_state is SignalState.TECHNICAL_EXIT
    # pre-entry the TECHNICAL_EXIT policy conservatively degrades to EXPIRE
    pre_entry = make_context(session_allowed=False, session_state="EQUITY_CLOSED")
    finding = check_session(pre_entry, exit_policy)
    assert finding is not None and finding.to_state is SignalState.EXPIRED

    pause_policy = make_signal_settings(monitor_session_end_policy="PAUSE")
    finding = check_session(blocked, pause_policy)
    assert finding is not None and finding.to_state is SignalState.PAUSED

    assert check_session(make_context(), SIGNAL_SETTINGS) is None  # allowed session
    paused_signal = make_context(
        signal=make_signal(state=SignalState.PAUSED, state_version=5), session_allowed=False
    )
    assert check_session(paused_signal, pause_policy) is None  # already dormant


# -------------------------------------------------------- data quality monitor


def test_data_quality_grace_then_invalid() -> None:
    healthy = make_context()
    assert check_data_quality(healthy, SIGNAL_SETTINGS) is None

    degraded_market = make_market(data_quality_status="DEGRADED", data_quality_ok=False)
    inside_grace = make_context(market=degraded_market)
    finding = check_data_quality(inside_grace, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is None
    assert "set_degraded_since" in finding.metadata

    long_degraded = make_context(
        signal=make_signal(data_degraded_since=NOW - timedelta(minutes=10)),
        market=degraded_market,
    )
    finding = check_data_quality(long_degraded, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.DATA_INVALID
    assert "never an exit fill" in finding.reason


def test_data_quality_recovery_clears_marker() -> None:
    recovered = make_context(signal=make_signal(data_degraded_since=NOW - timedelta(seconds=30)))
    finding = check_data_quality(recovered, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is None
    assert finding.metadata.get("clear_degraded_since") is True


def test_book_resync_counts_as_degraded() -> None:
    resync = make_context(market=make_market(book_resyncing=True))
    finding = check_data_quality(resync, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is None  # grace period first


# ---------------------------------------------------------------- plan monitor


def test_plan_monitor_mappings() -> None:
    lost = make_context(plan=None)
    finding = check_plan(lost, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.DATA_INVALID

    for status, expected in (
        ("SUPERSEDED", SignalState.SUPERSEDED),
        ("EXPIRED", SignalState.EXPIRED),
        ("DATA_INVALID", SignalState.DATA_INVALID),
    ):
        context = make_context(plan=replace(make_context().plan, status=status))
        finding = check_plan(context, SIGNAL_SETTINGS)
        assert finding is not None and finding.to_state is expected

    weird = replace(make_context().plan, status="WITHDRAWN")
    finding = check_plan(make_context(plan=weird), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.REJECTED  # pre-entry
    finding = check_plan(post_entry_context(plan=weird), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.DATA_INVALID  # post-entry


def test_candidate_monitor_mappings() -> None:
    base = make_context()
    lost = make_context(candidate=None)
    finding = check_plan(lost, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.DATA_INVALID

    superseded = make_context(candidate=replace(base.candidate, state="SUPERSEDED"))
    finding = check_plan(superseded, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.SUPERSEDED

    expired = make_context(candidate=replace(base.candidate, state="EXPIRED"))
    finding = check_plan(expired, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.EXPIRED

    # documented: post-entry candidate expiry alone takes no action
    post = post_entry_context(candidate=replace(base.candidate, state="EXPIRED"))
    assert check_plan(post, SIGNAL_SETTINGS) is None


# -------------------------------------------------------------- expiry monitor


def test_expiry_pre_and_post_entry() -> None:
    expired = make_context(signal=make_signal(expires_at=NOW - timedelta(seconds=1)))
    finding = check_expiry(expired, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.EXPIRED

    post = post_entry_context(
        signal=make_signal(
            state=SignalState.ACTIVE_RESEARCH,
            state_version=4,
            entry_confirmed_at=NOW - timedelta(hours=1),
            expires_at=NOW - timedelta(seconds=1),
        )
    )
    finding = check_expiry(post, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.EXPIRED  # default policy

    exit_policy = make_signal_settings(monitor_post_entry_expiry_policy="TECHNICAL_EXIT")
    finding = check_expiry(post, exit_policy)
    assert finding is not None and finding.to_state is SignalState.TECHNICAL_EXIT


def test_entry_wait_and_active_duration_windows() -> None:
    waited = make_context(signal=make_signal(watching_entry_at=NOW - timedelta(seconds=1801)))
    finding = check_expiry(waited, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.EXPIRED
    assert "entry wait" in finding.reason

    overlong = post_entry_context(
        signal=make_signal(
            state=SignalState.ACTIVE_RESEARCH,
            state_version=4,
            entry_confirmed_at=NOW - timedelta(hours=7),
            expires_at=NOW + timedelta(hours=1),
        )
    )
    finding = check_expiry(overlong, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.EXPIRED


def test_expiry_warning_update() -> None:
    soon = make_context(
        signal=make_signal(
            expires_at=NOW + timedelta(seconds=200),
            watching_entry_at=NOW - timedelta(minutes=1),
        )
    )
    finding = check_expiry(soon, SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is None
    assert finding.updates[0][0] is SignalUpdateType.EXPIRY_WARNING


# ----------------------------------------------------------- supersede monitor


def test_supersede_strict() -> None:
    assert check_supersede(make_context(), SIGNAL_SETTINGS) is None
    newer = uuid.uuid4()
    finding = check_supersede(make_context(superseding_plan_id=newer), SIGNAL_SETTINGS)
    assert finding is not None and finding.to_state is SignalState.SUPERSEDED
    assert "own admission" in finding.reason
    own = make_context(superseding_plan_id=make_signal().plan_id)
    assert check_supersede(own, SIGNAL_SETTINGS) is None
