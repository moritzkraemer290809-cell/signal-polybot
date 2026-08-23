"""Fee schedule assembly and validation.

Fee schedules are administered assumptions (config/seed/migration) - never
fetched from unverified sources and never hardcoded in the strategy core.
The configured default must be validated against the official Polymarket
documentation before live operation; every snapshot carries that caveat via
``source``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import CostSettings
from app.costs.models import CostModelError, FeeScheduleSnapshot
from app.costs.version import cost_configuration_hash


def build_default_schedule(settings: CostSettings) -> dict[str, Any]:
    """The configured default fee schedule as a persistable document."""
    return {
        "version": settings.fee_schedule_version,
        "source": settings.fee_schedule_source,
        "tiers": {
            settings.assumed_fee_tier: {
                "maker_fee_rate": settings.default_maker_fee_rate,
                "taker_fee_rate": settings.default_taker_fee_rate,
            }
        },
        "assumed_tier": settings.assumed_fee_tier,
        "note": (
            "Assumption from administered configuration - validate against "
            "official Polymarket fee documentation before live operation."
        ),
    }


def snapshot_from_schedule(
    document: dict[str, Any],
    settings: CostSettings,
    *,
    active: bool,
    effective_at: datetime | None = None,
) -> FeeScheduleSnapshot:
    """Build the immutable snapshot for one evaluation, validating rates."""
    tiers = document.get("tiers") or {}
    tier_name = str(document.get("assumed_tier") or settings.assumed_fee_tier)
    tier = tiers.get(tier_name)
    if tier is None:
        raise CostModelError(
            "FEE_SCHEDULE_UNAVAILABLE",
            f"fee schedule {document.get('version')} has no tier {tier_name!r}",
        )
    try:
        maker = float(tier["maker_fee_rate"])
        taker = float(tier["taker_fee_rate"])
    except (KeyError, TypeError, ValueError):
        raise CostModelError(
            "FEE_SCHEDULE_UNAVAILABLE", "fee schedule tier is missing maker/taker rates"
        ) from None
    if not (0.0 <= maker <= 0.05 and 0.0 <= taker <= 0.05):
        raise CostModelError(
            "FEE_SCHEDULE_UNAVAILABLE",
            "fee rates outside the plausible 0..0.05 decimal-fraction range",
        )
    return FeeScheduleSnapshot(
        version=str(document.get("version", settings.fee_schedule_version)),
        source=str(document.get("source", settings.fee_schedule_source)),
        tier=tier_name,
        maker_fee_rate=maker,
        taker_fee_rate=taker,
        config_hash=cost_configuration_hash(settings),
        active=active,
        effective_at=effective_at,
    )
