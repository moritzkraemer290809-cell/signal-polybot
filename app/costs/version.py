"""Immutable cost model versioning and deterministic configuration hashing.

Every cost estimate references the cost model name/version, the fee schedule
version, the execution assumption version and the configuration hash.  A
configuration change produces a new hash - existing estimates are never
rewritten.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.config import CostSettings


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def cost_configuration_hash(settings: CostSettings) -> str:
    """Deterministic hash over the full cost configuration."""
    return hashlib.sha256(_canonical(settings.model_dump(mode="json")).encode()).hexdigest()[:16]
