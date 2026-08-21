"""Immutable strategy versioning and deterministic configuration hashing.

Every evaluation, feature snapshot, regime record, candidate and rejection
references strategy_name, strategy_version, feature_schema_version and the
configuration hash.  Rule or parameter changes require a new version; old
decisions are never rewritten.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.config import StrategySettings

FEATURE_SCHEMA_VERSION = "fs-1"
#: hash over the rule implementations' identity; bump on any rule change.
RULESET_ID = "market_structure_v1/ruleset-1"


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def configuration_hash(settings: StrategySettings) -> str:
    """Deterministic hash over the full strategy configuration."""
    return hashlib.sha256(_canonical(settings.model_dump(mode="json")).encode()).hexdigest()[:16]


def ruleset_hash() -> str:
    return hashlib.sha256(RULESET_ID.encode()).hexdigest()[:16]
