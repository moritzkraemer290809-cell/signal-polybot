"""Central domain enums. String-valued for stable persistence and logging."""

from __future__ import annotations

from enum import StrEnum


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"
    CRYPTO = "CRYPTO"
    COMMODITY = "COMMODITY"
    FX = "FX"
    UNKNOWN = "UNKNOWN"


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


class TelegramDeliveryType(StrEnum):
    SYSTEM_STARTUP = "SYSTEM_STARTUP"
    SYSTEM_SHUTDOWN = "SYSTEM_SHUTDOWN"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    SYSTEM_WARNING = "SYSTEM_WARNING"
    DATA_QUALITY_WARNING = "DATA_QUALITY_WARNING"
    DATA_STALE = "DATA_STALE"
    WEBSOCKET_RECONNECTING = "WEBSOCKET_RECONNECTING"
    WEBSOCKET_DEGRADED = "WEBSOCKET_DEGRADED"
    BOT_PAUSED = "BOT_PAUSED"
    BOT_RESUMED = "BOT_RESUMED"
    DAILY_STATUS = "DAILY_STATUS"
    WATCHLIST = "WATCHLIST"
    SIGNAL_OPEN = "SIGNAL_OPEN"
    SIGNAL_UPDATE = "SIGNAL_UPDATE"
    SIGNAL_PARTIAL = "SIGNAL_PARTIAL"
    SIGNAL_BREAK_EVEN = "SIGNAL_BREAK_EVEN"
    SIGNAL_TRAILING = "SIGNAL_TRAILING"
    SIGNAL_EXIT = "SIGNAL_EXIT"
    SIGNAL_STOP = "SIGNAL_STOP"
    SIGNAL_INVALIDATED = "SIGNAL_INVALIDATED"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"


class TelegramDeliveryOperation(StrEnum):
    SEND = "SEND"
    EDIT = "EDIT"
    DELETE_OPTIONAL = "DELETE_OPTIONAL"


class TelegramDeliveryStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SENT = "SENT"
    EDITED = "EDITED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    CANCELLED = "CANCELLED"


#: Delivery priority: lower number = more important, never dropped.
#: 1 kritisch · 2 hoch · 3 normal · 4 niedrig
TELEGRAM_PRIORITY_BY_TYPE: dict[TelegramDeliveryType, int] = {
    TelegramDeliveryType.SYSTEM_ERROR: 1,
    TelegramDeliveryType.SIGNAL_STOP: 1,
    TelegramDeliveryType.SIGNAL_EXIT: 1,
    TelegramDeliveryType.SIGNAL_INVALIDATED: 1,
    TelegramDeliveryType.DATA_STALE: 2,
    TelegramDeliveryType.WEBSOCKET_DEGRADED: 2,
    TelegramDeliveryType.BOT_PAUSED: 2,
    TelegramDeliveryType.BOT_RESUMED: 2,
    TelegramDeliveryType.SYSTEM_STARTUP: 2,
    TelegramDeliveryType.SYSTEM_SHUTDOWN: 2,
    TelegramDeliveryType.SYSTEM_WARNING: 2,
    TelegramDeliveryType.DATA_QUALITY_WARNING: 2,
    TelegramDeliveryType.SIGNAL_EXPIRED: 2,
    TelegramDeliveryType.WEBSOCKET_RECONNECTING: 3,
    TelegramDeliveryType.SIGNAL_OPEN: 3,
    TelegramDeliveryType.SIGNAL_UPDATE: 3,
    TelegramDeliveryType.SIGNAL_PARTIAL: 3,
    TelegramDeliveryType.SIGNAL_BREAK_EVEN: 3,
    TelegramDeliveryType.SIGNAL_TRAILING: 3,
    TelegramDeliveryType.WATCHLIST: 4,
    TelegramDeliveryType.DAILY_STATUS: 4,
}


class TelegramSubsystemState(StrEnum):
    DISABLED = "DISABLED"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


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


class WsConnectionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"


class Channel(StrEnum):
    """Public WebSocket data channels."""

    TICKER = "ticker"
    BBO = "bbo"
    TRADES = "trades"
    ORDERBOOK = "orderbook"
    KLINES = "klines"
    STATISTICS = "statistics"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    AGING = "AGING"
    STALE = "STALE"
    INVALID = "INVALID"
    RESYNCING = "RESYNCING"
    UNKNOWN = "UNKNOWN"


class InstrumentQualityStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DATA_STALE = "DATA_STALE"
    DATA_INVALID = "DATA_INVALID"
    ORDERBOOK_RESYNCING = "ORDERBOOK_RESYNCING"
    UNAVAILABLE = "UNAVAILABLE"
