"""Bounded, deduplicating queue of internal lifecycle observations.

Shadow mode consumes ONLY internal, already persisted phase-10 lifecycle
signals - never a live external feed.  The queue is bounded and drops the
oldest entry under pressure (with a counter), so a burst can never grow
memory without bound.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ShadowWorkItem:
    """One lifecycle to (re-)evaluate in shadow mode."""

    signal_id: uuid.UUID
    state: str
    state_version: int
    observed_at: datetime


class ShadowEventQueue:
    """Bounded FIFO keyed by signal id (newest state wins per signal)."""

    def __init__(self, max_size: int) -> None:
        self._items: OrderedDict[uuid.UUID, ShadowWorkItem] = OrderedDict()
        self._max_size = max(1, max_size)
        self.dropped = 0
        self.coalesced = 0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def capacity(self) -> int:
        return self._max_size

    def offer(self, item: ShadowWorkItem) -> bool:
        """Enqueue or refresh one lifecycle; False when it displaced one."""
        existing = self._items.get(item.signal_id)
        if existing is not None:
            self.coalesced += 1
            if item.state_version >= existing.state_version:
                self._items[item.signal_id] = item
                self._items.move_to_end(item.signal_id)
            return True
        if len(self._items) >= self._max_size:
            self._items.popitem(last=False)
            self.dropped += 1
            self._items[item.signal_id] = item
            return False
        self._items[item.signal_id] = item
        return True

    def drain(self, limit: int) -> list[ShadowWorkItem]:
        """Take up to ``limit`` items in insertion order."""
        taken: list[ShadowWorkItem] = []
        for _ in range(max(0, limit)):
            if not self._items:
                break
            _, item = self._items.popitem(last=False)
            taken.append(item)
        return taken

    def clear(self) -> None:
        self._items.clear()
