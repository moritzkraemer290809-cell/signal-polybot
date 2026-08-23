"""Historical data replay interfaces and local providers.

The replay core never touches the network: it consumes immutable,
time-ordered snapshots handed to it by a provider.  V1 ships an in-memory
fixture provider (tests, deterministic scenarios) and a CSV/JSON provider
restricted to a repository-relative input directory.  A repository-backed
provider lives in the adapter layer.

Every consumed event passes the look-ahead guard: an event stamped after
the current replay instant is a bug, never a silent pass.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.simulation.enums import ReplayEventCategory, SimulationRejectionCode
from app.simulation.event_ordering import EventOrderingPolicy
from app.simulation.models import ReplayEvent
from app.simulation.replay_clock import LookaheadError, ReplayClock

#: columns every CSV replay input must provide
CSV_REQUIRED_COLUMNS = ("as_of", "category", "symbol")


class ReplayInputError(Exception):
    """A local replay input file was unusable."""

    def __init__(self, code: SimulationRejectionCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@runtime_checkable
class HistoricalDataProvider(Protocol):
    """Source of immutable, time-ordered public replay events."""

    @property
    def source_metadata(self) -> dict[str, Any]:
        """Provenance/version metadata recorded in the run manifest."""
        ...

    def events(self, start_at: datetime, end_at: datetime) -> Iterable[ReplayEvent]:
        """All input events inside the closed window, any order."""
        ...


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _parse_timestamp(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return _utc(raw)
    text = str(raw).strip()
    if text.isdigit():  # epoch milliseconds
        return datetime.fromtimestamp(int(text) / 1000, tz=UTC)
    try:
        return _utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError as error:
        raise ReplayInputError(
            SimulationRejectionCode.BACKTEST_DATA_INCOMPLETE,
            f"unparsable timestamp {raw!r}",
        ) from error


@dataclass
class FixtureDataProvider:
    """In-memory provider for deterministic local scenarios and tests."""

    events_list: Sequence[ReplayEvent]
    source_name: str = "fixture"
    data_version: str = "fixture-1"

    @property
    def source_metadata(self) -> dict[str, Any]:
        return {
            "source": self.source_name,
            "data_version": self.data_version,
            "event_count": len(self.events_list),
            "kind": "in-memory fixture (local, deterministic)",
        }

    def events(self, start_at: datetime, end_at: datetime) -> Iterable[ReplayEvent]:
        return [event for event in self.events_list if start_at <= event.as_of <= end_at]


class LocalFileDataProvider:
    """CSV/JSON provider restricted to a repository-relative directory.

    Paths are resolved against the configured input directory and must stay
    inside it: no traversal, no absolute paths, no foreign project files.
    """

    def __init__(
        self,
        repo_root: Path,
        allowed_directory: str,
        filename: str,
        *,
        data_version: str = "local-file-1",
    ) -> None:
        base = (repo_root / allowed_directory).resolve()
        if not base.is_relative_to(repo_root.resolve()):
            raise ReplayInputError(
                SimulationRejectionCode.CONFIGURATION_INVALID,
                "configured backtest input directory escapes the repository",
            )
        candidate = (base / filename).resolve()
        if not candidate.is_relative_to(base):
            raise ReplayInputError(
                SimulationRejectionCode.CONFIGURATION_INVALID,
                f"input file {filename!r} escapes the allowed input directory",
            )
        if not candidate.exists():
            raise ReplayInputError(
                SimulationRejectionCode.BACKTEST_DATA_INCOMPLETE,
                f"input file {filename!r} not found in the allowed input directory",
            )
        self._path = candidate
        self._base = base
        self._data_version = data_version

    @property
    def source_metadata(self) -> dict[str, Any]:
        stat = self._path.stat()
        return {
            "source": "local_file",
            "data_version": self._data_version,
            "file": self._path.relative_to(self._base).as_posix(),
            "size_bytes": stat.st_size,
            "kind": "repository-local input file",
        }

    def _rows(self) -> Iterator[dict[str, Any]]:
        if self._path.suffix.lower() == ".json":
            payload = json.loads(self._path.read_text())
            if not isinstance(payload, list):
                raise ReplayInputError(
                    SimulationRejectionCode.BACKTEST_DATA_INCOMPLETE,
                    "JSON replay input must be a list of event objects",
                )
            yield from payload
            return
        with self._path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [
                column for column in CSV_REQUIRED_COLUMNS if column not in (reader.fieldnames or [])
            ]
            if missing:
                raise ReplayInputError(
                    SimulationRejectionCode.BACKTEST_DATA_INCOMPLETE,
                    f"CSV replay input missing required column(s): {', '.join(missing)}",
                )
            yield from reader

    def events(self, start_at: datetime, end_at: datetime) -> Iterable[ReplayEvent]:
        seen: set[tuple[datetime, str, str, int]] = set()
        collected: list[ReplayEvent] = []
        for index, row in enumerate(self._rows()):
            as_of = _parse_timestamp(row["as_of"])
            category = str(row["category"]).strip()
            try:
                ReplayEventCategory(category)
            except ValueError as error:
                raise ReplayInputError(
                    SimulationRejectionCode.BACKTEST_DATA_INCOMPLETE,
                    f"unknown replay category {category!r} in local input",
                ) from error
            symbol = str(row["symbol"]).strip()
            sequence = int(row.get("sequence") or index)
            identity = (as_of, category, symbol, sequence)
            if identity in seen:
                continue  # duplicate rows are dropped deterministically
            seen.add(identity)
            if not start_at <= as_of <= end_at:
                continue
            payload = {
                key: value
                for key, value in row.items()
                if key not in ("as_of", "category", "symbol", "sequence")
            }
            collected.append(
                ReplayEvent(
                    as_of=as_of,
                    category=category,
                    symbol=symbol,
                    sequence=sequence,
                    payload=payload,
                )
            )
        return collected


class ReplayEventSource:
    """Ordered, guarded iteration over a provider's events."""

    def __init__(
        self,
        provider: HistoricalDataProvider,
        clock: ReplayClock,
        policy: EventOrderingPolicy | None = None,
        *,
        max_events: int | None = None,
    ) -> None:
        self._provider = provider
        self._clock = clock
        self._policy = policy or EventOrderingPolicy()
        self._max_events = max_events
        self.events_consumed = 0
        self.truncated = False

    @property
    def source_metadata(self) -> dict[str, Any]:
        return dict(self._provider.source_metadata)

    def __iter__(self) -> Iterator[ReplayEvent]:
        raw = self._provider.events(self._clock.start_at, self._clock.end_at)
        ordered = self._policy.sorted_events(raw)
        for event in self._policy.validate_stream(ordered):
            if self._max_events is not None and self.events_consumed >= self._max_events:
                self.truncated = True
                return
            # the clock always advances TO the event instant before the
            # pipeline may observe it - never past it
            self._clock.advance_to(event.as_of)
            self._clock.guard(event.as_of, f"{event.category} event")
            self.events_consumed += 1
            yield event


def assert_no_lookahead(clock: ReplayClock, moment: datetime | None, what: str) -> None:
    """Explicit guard for pipeline stages reading dated inputs."""
    clock.guard(moment, what)


__all__ = [
    "FixtureDataProvider",
    "HistoricalDataProvider",
    "LocalFileDataProvider",
    "LookaheadError",
    "ReplayEventSource",
    "ReplayInputError",
    "assert_no_lookahead",
]
