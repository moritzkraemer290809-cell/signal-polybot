"""Immutable lifecycle versioning and deterministic configuration hashing.

Rule/trigger/parameter changes never rewrite old signals: a changed
configuration yields a new hash, and re-evaluated lifecycle instances get a
new signal ID and a fresh event history.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.config import SignalLifecycleSettings


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def lifecycle_configuration_hash(settings: SignalLifecycleSettings) -> str:
    """Deterministic hash over the full lifecycle configuration."""
    return hashlib.sha256(_canonical(settings.model_dump(mode="json")).encode()).hexdigest()[:16]


def signal_dedupe_key(
    *,
    plan_id: str,
    candidate_id: str,
    lifecycle_model_version: str,
    lifecycle_config_hash: str,
) -> str:
    """Stable identity of one lifecycle instance's plan/config context."""
    raw = "|".join([plan_id, candidate_id, lifecycle_model_version, lifecycle_config_hash])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]
