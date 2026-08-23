"""Net expectancy: gross vs net PnL and R:R per reference target.

Sign conventions (documented once, used everywhere):

- ``gross_target_pnl``/``gross_invalidation_loss`` are PURE technical moves
  measured from the reference entry price - no frictions.
- ``net_*`` subtracts/adds every friction exactly once: entry slippage,
  exit slippage, entry fee, exit fee, expected funding cost and the flat
  conservative cost buffer.  This equals executing at the estimated VWAP+
  stress prices and paying fees on executed notional.
- Funding is always a cost (>= 0) - a favourable rate is never credited.

All values are hypothetical research estimates, never realised results.
"""

from __future__ import annotations

from app.costs.models import (
    FundingProjection,
    LegExecutionEstimate,
    TargetCostBreakdown,
)


def invalidation_economics(
    *,
    bullish: bool,
    entry: LegExecutionEstimate,
    invalidation: LegExecutionEstimate,
    quantity: float,
    funding: FundingProjection,
    cost_buffer: float,
) -> tuple[float, float]:
    """(gross_invalidation_loss, net_invalidation_loss), both > 0."""
    sign = 1.0 if bullish else -1.0
    gross = sign * (entry.reference_price - invalidation.reference_price) * quantity
    net = (
        gross
        + entry.fee_cost
        + invalidation.fee_cost
        + entry.slippage_cost
        + invalidation.slippage_cost
        + funding.expected_cost
        + cost_buffer
    )
    return gross, net


def target_economics(
    *,
    bullish: bool,
    target_index: int,
    target_price: float,
    target_leg: LegExecutionEstimate,
    entry: LegExecutionEstimate,
    quantity: float,
    funding: FundingProjection,
    cost_buffer: float,
    gross_invalidation_loss: float,
    net_invalidation_loss: float,
) -> TargetCostBreakdown:
    sign = 1.0 if bullish else -1.0
    gross = sign * (target_price - entry.reference_price) * quantity
    net = (
        gross
        - entry.fee_cost
        - target_leg.fee_cost
        - entry.slippage_cost
        - target_leg.slippage_cost
        - funding.expected_cost
        - cost_buffer
    )
    gross_rr = gross / gross_invalidation_loss if gross_invalidation_loss > 0 else None
    net_rr = net / net_invalidation_loss if net_invalidation_loss > 0 else None
    return TargetCostBreakdown(
        target_price=target_price,
        target_index=target_index,
        gross_target_pnl=gross,
        net_target_pnl=net,
        gross_rr=gross_rr,
        net_rr=net_rr,
        exit_fee=target_leg.fee_cost,
        exit_slippage_cost=target_leg.slippage_cost,
    )
