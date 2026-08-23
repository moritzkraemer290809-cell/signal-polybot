"""Deterministic idempotency keys for events and updates.

An event key is stable for one logical transition attempt: it hashes the
signal ID, the event type, the target state and the state version the
transition was computed AGAINST.  A retry after restart recomputes the same
key and the unique constraint deduplicates it.  Update keys bucket
repetitive observations into aggregation windows.
"""

from __future__ import annotations

import hashlib
from datetime import datetime


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def event_idempotency_key(
    *,
    signal_id: str,
    event_type: str,
    to_state: str,
    expected_state_version: int,
    snapshot_marker: str = "",
) -> str:
    return _digest(
        "|".join([signal_id, event_type, to_state, str(expected_state_version), snapshot_marker])
    )


def update_idempotency_key(
    *,
    signal_id: str,
    update_type: str,
    as_of: datetime,
    window_seconds: float,
    detail_marker: str = "",
) -> str:
    epoch = int(as_of.timestamp())
    bucket = epoch - epoch % max(1, int(window_seconds))
    return _digest("|".join([signal_id, update_type, str(bucket), detail_marker]))
