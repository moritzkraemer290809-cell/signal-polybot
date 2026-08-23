"""Fee resolution per execution mode.

Maps a versioned execution mode to the fee kind and computes the fee on
NOTIONAL basis (executed price * quantity) - never on margin basis.
"""

from __future__ import annotations

from app.costs.enums import ExecutionMode, FeeKind
from app.costs.models import FeeScheduleSnapshot

_MAKER_MODES = (ExecutionMode.ENTRY_MAKER, ExecutionMode.TARGET_MAKER)


def fee_kind_for(mode: ExecutionMode) -> FeeKind:
    return FeeKind.MAKER if mode in _MAKER_MODES else FeeKind.TAKER


def fee_cost(
    schedule: FeeScheduleSnapshot, mode: ExecutionMode, execution_price: float, quantity: float
) -> float:
    """Fee in pUSD for one leg on notional basis."""
    if execution_price <= 0 or quantity <= 0:
        return 0.0
    return schedule.rate_for(fee_kind_for(mode)) * execution_price * quantity
