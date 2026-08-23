"""Monotonic replay clock with an explicit look-ahead guard.

The clock is the single source of "now" during a backtest.  It only ever
moves forward, and every data access is checked against it: reading a
value stamped later than the current replay instant is a look-ahead bug,
not a rounding detail, so it raises instead of silently succeeding.
"""

from __future__ import annotations

from datetime import datetime

from app.simulation.enums import SimulationRejectionCode
from app.simulation.version import REPLAY_CLOCK_VERSION


class LookaheadError(Exception):
    """Future data was requested at the current replay instant."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = SimulationRejectionCode.LOOKAHEAD_GUARD_TRIGGERED


class ReplayClock:
    """Monotonic clock over a closed [start_at, end_at] replay window."""

    version = REPLAY_CLOCK_VERSION

    def __init__(self, start_at: datetime, end_at: datetime) -> None:
        if end_at < start_at:
            raise ValueError("replay end_at must not precede start_at")
        self._start_at = start_at
        self._end_at = end_at
        self._now = start_at
        self.lookahead_violations = 0

    @property
    def start_at(self) -> datetime:
        return self._start_at

    @property
    def end_at(self) -> datetime:
        return self._end_at

    def now(self) -> datetime:
        return self._now

    def advance_to(self, moment: datetime) -> None:
        """Move the replay instant forward (never backwards)."""
        if moment < self._now:
            self.lookahead_violations += 1
            raise LookaheadError(
                f"replay clock cannot move backwards: {moment.isoformat()} < "
                f"{self._now.isoformat()}"
            )
        self._now = min(moment, self._end_at) if moment > self._end_at else moment

    def within_window(self, moment: datetime) -> bool:
        return self._start_at <= moment <= self._end_at

    def guard(self, moment: datetime | None, what: str) -> None:
        """Reject any value stamped after the current replay instant."""
        if moment is None:
            return
        if moment > self._now:
            self.lookahead_violations += 1
            raise LookaheadError(
                f"look-ahead guard: {what} stamped {moment.isoformat()} is not "
                f"observable at replay instant {self._now.isoformat()}"
            )

    def visible(self, moment: datetime | None) -> bool:
        """True when a timestamp is already observable (no exception)."""
        return moment is not None and moment <= self._now

    def document(self) -> dict[str, str]:
        return {
            "clock_version": self.version,
            "start_at": self._start_at.isoformat(),
            "end_at": self._end_at.isoformat(),
            "note": "monotonic replay clock - no future data is observable",
        }
