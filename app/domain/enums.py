"""Central domain enums. String-valued for stable persistence and logging."""

from __future__ import annotations

from enum import StrEnum


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    CRYPTO = "CRYPTO"
    OTHER = "OTHER"


class InstrumentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    DELISTED = "DELISTED"
    UNKNOWN = "UNKNOWN"


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"

    @property
    def milliseconds(self) -> int:
        return _TIMEFRAME_MS[self]


_TIMEFRAME_MS: dict[Timeframe, int] = {
    Timeframe.M1: 60_000,
    Timeframe.M5: 300_000,
    Timeframe.M15: 900_000,
    Timeframe.H1: 3_600_000,
}


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    REJECTED = "REJECTED"
    WATCHING = "WATCHING"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    BREAK_EVEN = "BREAK_EVEN"
    TRAILING = "TRAILING"
    CLOSED_TP = "CLOSED_TP"
    CLOSED_STOP = "CLOSED_STOP"
    CLOSED_STRUCTURE_EXIT = "CLOSED_STRUCTURE_EXIT"
    CLOSED_TIME_EXIT = "CLOSED_TIME_EXIT"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    PAUSED = "PAUSED"


class MarketRegime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    BREAKOUT = "BREAKOUT"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    EVENT_RISK = "EVENT_RISK"
    NO_TRADE = "NO_TRADE"


class RejectionReason(StrEnum):
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    STALE_DATA = "STALE_DATA"
    LOW_VOLUME = "LOW_VOLUME"
    MARKET_INACTIVE = "MARKET_INACTIVE"
    EXCESSIVE_VOLATILITY = "EXCESSIVE_VOLATILITY"
    CORRELATED_RISK = "CORRELATED_RISK"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    MIN_NOTIONAL_UNSATISFIED = "MIN_NOTIONAL_UNSATISFIED"
    INVALID_PRICE_DATA = "INVALID_PRICE_DATA"
    COST_MODEL_UNAVAILABLE = "COST_MODEL_UNAVAILABLE"


class DecisionType(StrEnum):
    SIGNAL_SENT = "SIGNAL_SENT"
    WATCHLIST = "WATCHLIST"
    REJECTED = "REJECTED"
    NO_TRADE = "NO_TRADE"


class DataSource(StrEnum):
    REST = "REST"
    WEBSOCKET = "WEBSOCKET"


class DataQualityStatus(StrEnum):
    FRESH = "FRESH"
    DATA_STALE = "DATA_STALE"
    UNKNOWN = "UNKNOWN"


class TradingSession(StrEnum):
    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"
    CLOSED = "CLOSED"
    CRYPTO_24_7 = "CRYPTO_24_7"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    EDITED = "EDITED"
    FAILED = "FAILED"


class BotState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DEGRADED = "DEGRADED"
    SHUTTING_DOWN = "SHUTTING_DOWN"


class SystemEventLevel(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
