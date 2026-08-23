"""Data quality monitoring with a configurable degradation grace period.

Insufficient data terminates the signal as DATA_INVALID - never as a
filled exit.  A short degraded window is tolerated inside the grace
period; stale data never confirms entry, targets or invalidation (the
other monitors enforce that independently).  All degradation reasons are
persisted via metadata/updates.
"""

from __future__ import annotations

from datetime import timedelta

from app.config import SignalLifecycleSettings
from app.signals.enums import SignalEventType, SignalState, SignalUpdateType
from app.signals.models import MonitorContext, MonitorFinding


def check_data_quality(
    context: MonitorContext, settings: SignalLifecycleSettings
) -> MonitorFinding | None:
    signal = context.signal
    market = context.market
    healthy = market.data_quality_ok and not market.book_resyncing
    if healthy:
        if signal.data_degraded_since is not None:
            # recovery observation; the job clears the marker
            return MonitorFinding(
                monitor="data_quality",
                priority_key="INFO",
                to_state=None,
                event_type=None,
                reason="data quality recovered inside the grace period",
                metadata={"clear_degraded_since": True},
                updates=((SignalUpdateType.DATA_QUALITY_DEGRADED, "data quality recovered"),),
            )
        return None

    degraded_since = signal.data_degraded_since or context.as_of
    elapsed = (context.as_of - degraded_since).total_seconds()
    grace = settings.lifecycle_data_degraded_grace_seconds
    reasons = {
        "data_quality_status": market.data_quality_status,
        "book_resyncing": market.book_resyncing,
        "bbo_fresh": market.bbo_fresh,
        "book_fresh": market.book_fresh,
        "degraded_since": degraded_since.isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "grace_seconds": grace,
    }
    if elapsed > grace:
        return MonitorFinding(
            monitor="data_quality",
            priority_key="DATA_INVALID",
            to_state=SignalState.DATA_INVALID,
            event_type=SignalEventType.DATA_INVALID,
            reason=(
                f"data quality {market.data_quality_status} beyond the "
                f"{grace:.0f}s grace period - monitoring basis invalid "
                "(research lifecycle, never an exit fill)"
            ),
            metadata=reasons,
            updates=(
                (
                    SignalUpdateType.DATA_QUALITY_DEGRADED,
                    f"data quality {market.data_quality_status} beyond grace",
                ),
            ),
        )
    # inside the grace period: observation only, remember the start
    return MonitorFinding(
        monitor="data_quality",
        priority_key="INFO",
        to_state=None,
        event_type=None,
        reason=(
            f"data quality {market.data_quality_status} degraded - grace period "
            f"until {(degraded_since + timedelta(seconds=grace)).isoformat()}"
        ),
        metadata={"set_degraded_since": degraded_since.isoformat(), **reasons},
        updates=(
            (
                SignalUpdateType.DATA_QUALITY_DEGRADED,
                f"data quality {market.data_quality_status} (grace running)",
            ),
        ),
    )
