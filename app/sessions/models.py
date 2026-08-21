"""Session domain models and enums."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class EquitySessionState(StrEnum):
    EQUITY_CLOSED = "EQUITY_CLOSED"
    EQUITY_PREMARKET = "EQUITY_PREMARKET"
    EQUITY_REGULAR = "EQUITY_REGULAR"
    EQUITY_EARLY_CLOSE = "EQUITY_EARLY_CLOSE"
    EQUITY_AFTER_HOURS = "EQUITY_AFTER_HOURS"
    EQUITY_HOLIDAY = "EQUITY_HOLIDAY"
    EQUITY_WEEKEND = "EQUITY_WEEKEND"


class CryptoSessionState(StrEnum):
    CRYPTO_24_7 = "CRYPTO_24_7"
    CRYPTO_THIN_LIQUIDITY = "CRYPTO_THIN_LIQUIDITY"
    CRYPTO_DISABLED = "CRYPTO_DISABLED"


@dataclass(frozen=True)
class EquitySessionSnapshot:
    """Result of one equity session evaluation (all timestamps UTC)."""

    state: EquitySessionState
    evaluated_at: datetime
    calendar_available: bool
    calendar_version: str | None
    #: analysis allowed per V1 policy (REGULAR / EARLY_CLOSE before its end)
    analysis_allowed: bool
    holiday_name: str | None = None
    early_close_reason: str | None = None
    #: next state transition, when computable (UTC)
    next_transition_at: datetime | None = None
    next_transition_state: EquitySessionState | None = None
    detail: str | None = None


@dataclass(frozen=True)
class CryptoSessionSnapshot:
    state: CryptoSessionState
    evaluated_at: datetime
    analysis_allowed: bool
    #: True inside a configured thin-liquidity window with LIMITED_SESSION policy
    limited: bool = False
    window_detail: str | None = None


@dataclass(frozen=True)
class SessionOverview:
    equity: EquitySessionSnapshot
    crypto: CryptoSessionSnapshot
    extra: dict[str, str] = field(default_factory=dict)
