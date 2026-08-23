"""Hypothetical reference entry zone.

Anchored at the candidate's confirmed technical level, constrained by fresh
BBO data.  Bullish candidates are priced conservatively taker/ask-oriented,
bearish ones bid-oriented.  The zone is an internal model value - never a
Telegram output and never an instruction to trade.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from app.config import RiskSettings
from app.risk.enums import EntryBasis, PlanRejectionCode
from app.risk.models import ReferenceEntryZone, RiskEngineError
from app.risk.rounding import round_price

if TYPE_CHECKING:
    from app.risk.models import RiskEvaluationContext

_BASIS_BY_CANDIDATE_TYPE = {
    "BULLISH_SWEEP_REVERSAL": EntryBasis.RECLAIM_LEVEL,
    "BEARISH_SWEEP_REVERSAL": EntryBasis.RECLAIM_LEVEL,
    "BULLISH_RECLAIM_CONTINUATION": EntryBasis.RETEST_ZONE,
    "BEARISH_REJECTION_CONTINUATION": EntryBasis.STRUCTURE_LEVEL_ZONE,
    "RANGE_BREAKOUT_UP": EntryBasis.BREAKOUT_CLOSE_ZONE,
    "RANGE_BREAKOUT_DOWN": EntryBasis.BREAKOUT_CLOSE_ZONE,
}


def derive_entry_zone(context: RiskEvaluationContext, settings: RiskSettings) -> ReferenceEntryZone:
    candidate = context.candidate
    if not candidate.referenced_levels:
        raise RiskEngineError(
            PlanRejectionCode.REFERENCE_ENTRY_UNAVAILABLE,
            "candidate carries no technical level to anchor an entry zone",
        )
    try:
        anchor = float(candidate.referenced_levels[0]["price"])
    except (KeyError, TypeError, ValueError):
        raise RiskEngineError(
            PlanRejectionCode.REFERENCE_ENTRY_UNAVAILABLE, "malformed candidate level"
        ) from None
    if anchor <= 0:
        raise RiskEngineError(
            PlanRejectionCode.REFERENCE_ENTRY_UNAVAILABLE, "non-positive anchor level"
        )
    if not context.bbo_fresh or context.best_bid is None or context.best_ask is None:
        raise RiskEngineError(
            PlanRejectionCode.REFERENCE_ENTRY_UNAVAILABLE,
            "no fresh BBO - reference entry cannot be established",
        )
    spread_bps = context.book.spread_bps
    if spread_bps is None or spread_bps > 2 * settings.max_entry_chase_bps:
        raise RiskEngineError(
            PlanRejectionCode.REFERENCE_ENTRY_UNAVAILABLE,
            f"spread {spread_bps if spread_bps is not None else 'n/a'}bps makes the "
            f"entry zone unusable (limit {2 * settings.max_entry_chase_bps:.0f}bps)",
        )

    bullish = candidate.bullish
    # conservative executable side: ask for bullish, bid for bearish
    executable = context.best_ask if bullish else context.best_bid
    chase_limit = anchor * (
        1 + settings.max_entry_chase_bps / 10_000
        if bullish
        else 1 - settings.max_entry_chase_bps / 10_000
    )
    ran_away = executable > chase_limit if bullish else executable < chase_limit
    if ran_away:
        raise RiskEngineError(
            PlanRejectionCode.REFERENCE_ENTRY_UNAVAILABLE,
            f"executable price {executable:.6g} chased beyond the technical zone "
            f"around {anchor:.6g} (max {settings.max_entry_chase_bps:.0f}bps)",
        )

    low, high = sorted((anchor, executable))
    price_decimals = context.instrument.price_decimals
    tick = context.instrument.tick_size
    entry_low = round_price(low, price_decimals=price_decimals, tick_size=tick, mode="down")
    entry_high = round_price(high, price_decimals=price_decimals, tick_size=tick, mode="up")
    entry_reference = round_price(
        executable, price_decimals=price_decimals, tick_size=tick, mode="up" if bullish else "down"
    )
    basis = _BASIS_BY_CANDIDATE_TYPE.get(candidate.candidate_type, EntryBasis.STRUCTURE_LEVEL_ZONE)
    return ReferenceEntryZone(
        entry_low=entry_low,
        entry_high=entry_high,
        entry_reference_price=entry_reference,
        entry_basis=basis,
        direction=candidate.direction,
        valid_until=context.as_of + timedelta(seconds=settings.plan_expiry_seconds),
        bbo_snapshot_at=context.bbo_at,
        bbo_stale=not context.bbo_fresh,
        execution_mode=context.execution_assumptions.entry.value,
        rationale=(
            f"{basis.value} anchored at {anchor:.6g}, conservatively priced at the "
            f"{'ask' if bullish else 'bid'} ({executable:.6g}) - internal model zone, "
            "not a trading instruction"
        ),
    )
