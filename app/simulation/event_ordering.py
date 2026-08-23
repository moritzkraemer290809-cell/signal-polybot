"""Versioned, deterministic replay event ordering.

Causality is the whole point of a backtest: at one replay instant the
pipeline must observe the world in a fixed order, and two events sharing
a timestamp must never resolve differently between runs.  The ordering
version is stored in every manifest - changing it changes the version and
therefore requires a NEW run.

Order at equal timestamps (ascending priority):

1. session/calendar events
2. instrument/market status
3. data-quality / book snapshot updates
4. ticker / BBO / trades
5. CLOSED candles
6. strategy evaluation
7. risk plan evaluation
8. lifecycle monitoring
9. simulation delay/execution events

Within one category the tie-breakers are (symbol, sequence) - both
deterministic and stable across runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime

from app.simulation.enums import ReplayEventCategory
from app.simulation.models import ReplayEvent
from app.simulation.version import REPLAY_ORDERING_VERSION

#: category -> priority (lower runs first at the same timestamp)
CATEGORY_PRIORITY: dict[ReplayEventCategory, int] = {
    ReplayEventCategory.SESSION_CALENDAR: 1,
    ReplayEventCategory.INSTRUMENT_STATUS: 2,
    ReplayEventCategory.DATA_QUALITY_BOOK: 3,
    ReplayEventCategory.TICKER_BBO_TRADES: 4,
    ReplayEventCategory.CLOSED_CANDLE: 5,
    ReplayEventCategory.STRATEGY_EVALUATION: 6,
    ReplayEventCategory.RISK_PLAN_EVALUATION: 7,
    ReplayEventCategory.LIFECYCLE_MONITORING: 8,
    ReplayEventCategory.SIMULATION_EXECUTION: 9,
}

#: derived pipeline stages the engine runs itself (never read from input data)
DERIVED_CATEGORIES = frozenset(
    {
        ReplayEventCategory.STRATEGY_EVALUATION,
        ReplayEventCategory.RISK_PLAN_EVALUATION,
        ReplayEventCategory.LIFECYCLE_MONITORING,
        ReplayEventCategory.SIMULATION_EXECUTION,
    }
)


class EventOrderingError(Exception):
    """An input stream violated the causal ordering contract."""

    def __init__(self, detail: str, event: ReplayEvent | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.event = event


@dataclass(frozen=True)
class EventOrderingPolicy:
    """Deterministic ordering of replay events (versioned)."""

    version: str = REPLAY_ORDERING_VERSION

    def priority_of(self, category: str) -> int:
        try:
            return CATEGORY_PRIORITY[ReplayEventCategory(category)]
        except ValueError as error:
            raise EventOrderingError(f"unknown replay category {category!r}") from error

    def sort_key(self, event: ReplayEvent) -> tuple[datetime, int, str, int]:
        return (
            event.as_of,
            self.priority_of(event.category),
            event.symbol,
            event.sequence,
        )

    def sorted_events(self, events: Iterable[ReplayEvent]) -> list[ReplayEvent]:
        """Total, reproducible order over a finite event batch."""
        return sorted(events, key=self.sort_key)

    def validate_stream(self, events: Iterable[ReplayEvent]) -> Iterator[ReplayEvent]:
        """Yield events, raising when the stream is not causally ordered."""
        previous: tuple[datetime, int, str, int] | None = None
        for event in events:
            key = self.sort_key(event)
            if previous is not None and key < previous:
                raise EventOrderingError(
                    f"replay stream out of order at {event.as_of.isoformat()} "
                    f"({event.category}/{event.symbol}) - causal replay requires "
                    "strictly ascending order",
                    event,
                )
            previous = key
            yield event

    def document(self) -> dict[str, object]:
        """Serializable ordering description for the run manifest."""
        return {
            "ordering_version": self.version,
            "categories": [
                category.value
                for category in sorted(CATEGORY_PRIORITY, key=lambda item: CATEGORY_PRIORITY[item])
            ],
            "tie_breakers": ["timestamp", "category_priority", "symbol", "sequence"],
            "note": (
                "deterministic causal ordering - identical inputs always replay in "
                "the identical sequence"
            ),
        }
