"""Hypothetical drawdown of a modelled equity curve.

The curve is built from modelled net results only - in R (technical risk
units) or against the configured VIRTUAL reference account.  It is never
a real account curve and never a statement about actual capital.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.simulation.models import DrawdownResult

R_BASIS = "R"
VIRTUAL_BASIS = "VIRTUAL_ACCOUNT_PUSD"


def equity_curve(values: Sequence[float], *, starting_value: float = 0.0) -> list[float]:
    """Cumulative modelled curve (first point is the starting value)."""
    curve = [starting_value]
    running = starting_value
    for value in values:
        running += value
        curve.append(running)
    return curve


def compute_drawdown(
    curve: Sequence[float],
    *,
    basis: str = R_BASIS,
    percentage_reference: float | None = None,
) -> DrawdownResult | None:
    """Maximum modelled drawdown of a curve.

    ``percentage_reference`` is the virtual account size; without it no
    percentage drawdown is claimed (R-based analysis stays available).
    """
    if len(curve) < 2:
        return None
    peak = curve[0]
    peak_index = 0
    max_drawdown = 0.0
    best_peak = curve[0]
    best_peak_index = 0
    trough = curve[0]
    trough_index = 0
    for index, value in enumerate(curve):
        if value > peak:
            peak = value
            peak_index = index
        drawdown = value - peak
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            best_peak = peak
            best_peak_index = peak_index
            trough = value
            trough_index = index

    recovery_index: int | None = None
    for index in range(trough_index + 1, len(curve)):
        if curve[index] >= best_peak:
            recovery_index = index
            break

    percentage: float | None = None
    if percentage_reference and percentage_reference > 0:
        percentage = abs(max_drawdown) / percentage_reference * 100.0

    return DrawdownResult(
        basis=basis,
        peak_value=best_peak,
        trough_value=trough,
        max_drawdown=max_drawdown,
        max_drawdown_pct=percentage,
        peak_index=best_peak_index,
        trough_index=trough_index,
        recovery_index=recovery_index,
        drawdown_duration_steps=max(0, trough_index - best_peak_index),
        recovered=recovery_index is not None,
    )


def max_consecutive_losses(values: Sequence[float]) -> int:
    """Longest streak of modelled negative outcomes."""
    longest = 0
    current = 0
    for value in values:
        if value < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest
