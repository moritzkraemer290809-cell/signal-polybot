"""Admission gates: an ELIGIBLE plan is never adopted unchecked.

Every gate is conservative and produces a structured rejection code.  A
rejected admission never creates a lifecycle instance and never modifies
the underlying plan or candidate; the aggregated rejection history is the
audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import SignalLifecycleSettings
from app.signals.entry_trigger import default_trigger_for
from app.signals.enums import SignalRejectionCode
from app.signals.models import (
    AdmissionDecision,
    CandidateStateSnapshot,
    MarketStateSnapshot,
    PlanSnapshot,
)


@dataclass(frozen=True)
class AdmissionContext:
    """Immutable inputs for one admission check."""

    plan: PlanSnapshot
    candidate: CandidateStateSnapshot | None
    market: MarketStateSnapshot
    session_state: str
    session_allowed: bool
    watchlist_active: bool
    market_quality_score: int | None
    bot_paused: bool
    active_signal_count: int
    duplicate_exists: bool
    instrument_snapshot_age_seconds: float | None
    as_of: datetime


def admit(context: AdmissionContext, settings: SignalLifecycleSettings) -> AdmissionDecision:
    def rejected(code: SignalRejectionCode, detail: str) -> AdmissionDecision:
        return AdmissionDecision(admitted=False, code=code, detail=detail)

    plan = context.plan
    now = context.as_of
    if context.bot_paused:
        return rejected(SignalRejectionCode.BOT_PAUSED, "global paused mode - no admission")
    if settings.lifecycle_require_eligible_plan and plan.status != "ELIGIBLE":
        return rejected(SignalRejectionCode.PLAN_NOT_ELIGIBLE, f"plan status {plan.status}")
    if plan.expiry_at <= now:
        return rejected(SignalRejectionCode.PLAN_EXPIRED, "plan already expired")
    if (now - plan.as_of).total_seconds() > settings.lifecycle_plan_max_age_seconds:
        return rejected(
            SignalRejectionCode.PLAN_SNAPSHOT_STALE,
            f"plan snapshot older than {settings.lifecycle_plan_max_age_seconds:.0f}s",
        )
    candidate = context.candidate
    if candidate is None:
        return rejected(SignalRejectionCode.CANDIDATE_NOT_CONFIRMED, "candidate not loadable")
    if candidate.state == "SUPERSEDED":
        return rejected(SignalRejectionCode.CANDIDATE_SUPERSEDED, "candidate superseded")
    if candidate.state == "EXPIRED" or (
        candidate.expiry_at is not None and candidate.expiry_at <= now
    ):
        return rejected(SignalRejectionCode.CANDIDATE_EXPIRED, "candidate expired")
    if settings.lifecycle_require_confirmed_candidate and candidate.state != "CONFIRMED":
        return rejected(
            SignalRejectionCode.CANDIDATE_NOT_CONFIRMED,
            f"candidate state {candidate.state}",
        )
    if (
        candidate.as_of is not None
        and (now - candidate.as_of).total_seconds() > settings.lifecycle_candidate_max_age_seconds
    ):
        return rejected(
            SignalRejectionCode.CANDIDATE_SNAPSHOT_STALE,
            f"candidate snapshot older than {settings.lifecycle_candidate_max_age_seconds:.0f}s",
        )
    if settings.lifecycle_require_active_watchlist and not context.watchlist_active:
        return rejected(
            SignalRejectionCode.WATCHLIST_NOT_ACTIVE, "instrument not on active watchlist"
        )
    if settings.lifecycle_require_allowed_session and not context.session_allowed:
        return rejected(
            SignalRejectionCode.SESSION_NOT_ALLOWED,
            f"session {context.session_state} not allowed",
        )
    if (
        context.market_quality_score is None
        or context.market_quality_score < settings.lifecycle_min_market_quality_score
    ):
        return rejected(
            SignalRejectionCode.MARKET_QUALITY_INSUFFICIENT,
            f"market quality {context.market_quality_score} below "
            f"{settings.lifecycle_min_market_quality_score}",
        )
    market = context.market
    quality_ok = market.data_quality_ok or (
        settings.lifecycle_allow_degraded_data and market.data_quality_status == "DEGRADED"
    )
    if settings.lifecycle_require_healthy_data and not quality_ok:
        return rejected(
            SignalRejectionCode.DATA_QUALITY_INSUFFICIENT,
            f"data quality {market.data_quality_status}",
        )
    if settings.lifecycle_require_fresh_orderbook and (
        not market.book_fresh or market.book_resyncing or not market.bbo_fresh
    ):
        return rejected(
            SignalRejectionCode.ORDERBOOK_NOT_FRESH, "orderbook/BBO not fresh or resyncing"
        )
    if (
        context.instrument_snapshot_age_seconds is None
        or context.instrument_snapshot_age_seconds > settings.lifecycle_snapshot_max_age_seconds
    ):
        return rejected(
            SignalRejectionCode.INSTRUMENT_SNAPSHOT_STALE,
            "instrument snapshot missing or older than "
            f"{settings.lifecycle_snapshot_max_age_seconds:.0f}s",
        )
    if context.duplicate_exists:
        return rejected(
            SignalRejectionCode.SIGNAL_DUPLICATE,
            "an active lifecycle signal for this plan/config context already exists",
        )
    if context.active_signal_count >= settings.lifecycle_max_active_per_instrument:
        return rejected(
            SignalRejectionCode.ACTIVE_SIGNAL_LIMIT_REACHED,
            f"active research signal limit "
            f"({settings.lifecycle_max_active_per_instrument}) reached",
        )
    if not plan.target_prices or plan.entry_reference_price <= 0:
        return rejected(
            SignalRejectionCode.ADMISSION_CONFIGURATION_INVALID,
            "plan reference levels incomplete",
        )
    trigger = default_trigger_for(plan.candidate_type, settings)
    return AdmissionDecision(
        admitted=True,
        code=None,
        detail=f"admitted with entry trigger {trigger.value}",
        trigger=trigger,
        approximation_flags=(
            ("CANDLE_APPROXIMATED_INTRABAR_ORDER",) if trigger.value != "ZONE_TOUCH" else ()
        ),
    )
