"""Cost engine enums."""

from __future__ import annotations

from enum import StrEnum


class ExecutionMode(StrEnum):
    """Versioned execution assumption for one leg of a hypothetical plan."""

    ENTRY_TAKER = "ENTRY_TAKER"
    ENTRY_MAKER = "ENTRY_MAKER"
    TARGET_TAKER = "TARGET_TAKER"
    TARGET_MAKER = "TARGET_MAKER"
    STOP_TAKER = "STOP_TAKER"
    STOP_STRESS_TAKER = "STOP_STRESS_TAKER"


class FeeKind(StrEnum):
    MAKER = "MAKER"
    TAKER = "TAKER"


class CostComponent(StrEnum):
    ENTRY_FEE = "ENTRY_FEE"
    EXIT_FEE = "EXIT_FEE"
    ENTRY_SLIPPAGE = "ENTRY_SLIPPAGE"
    EXIT_SLIPPAGE = "EXIT_SLIPPAGE"
    SPREAD = "SPREAD"
    FUNDING = "FUNDING"
    COST_BUFFER = "COST_BUFFER"


class FundingModelState(StrEnum):
    MODELED = "MODELED"
    BUFFERED_ONLY = "BUFFERED_ONLY"  # thin data, conservative buffer applied
    UNAVAILABLE = "UNAVAILABLE"


class CostSubsystemState(StrEnum):
    DISABLED = "DISABLED"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
