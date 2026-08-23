"""Transition event construction (idempotent, versioned)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.signals.idempotency import event_idempotency_key
from app.signals.models import TransitionStep


def build_transition_event(
    *,
    signal_id: uuid.UUID,
    step: TransitionStep,
    expected_state_version: int,
    event_schema_version: str,
    as_of: datetime,
    correlation_id: str,
) -> dict[str, Any]:
    """One persistable event per transition attempt.

    The idempotency key hashes the state version the transition was computed
    against - a retry after a crash recomputes the same key and the unique
    constraint deduplicates it.
    """
    return {
        "signal_id": signal_id,
        "event_type": step.event_type.value,
        "from_state": step.from_state.value,
        "to_state": step.to_state.value,
        "state_version": expected_state_version + 1,
        "priority": step.priority,
        "idempotency_key": event_idempotency_key(
            signal_id=str(signal_id),
            event_type=step.event_type.value,
            to_state=step.to_state.value,
            expected_state_version=expected_state_version,
        ),
        "detail": {
            "reason": step.reason,
            "metadata": step.metadata,
            "schema": event_schema_version,
            "correlation_id": correlation_id,
        },
        "as_of": as_of,
    }
