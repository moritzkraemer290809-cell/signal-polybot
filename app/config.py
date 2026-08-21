"""Typed application settings.

All secrets, ids, parameters, thresholds, feature flags and limits are loaded
from environment variables / the repository-local ``.env`` file only.  The
``.env`` file is resolved strictly relative to the repository root - no parent
directories, no home directory, no foreign projects.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"


def _split_csv(value: object) -> object:
    """Accept comma separated strings or JSON arrays for list fields."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            return json.loads(stripped)
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return value


class _EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )


class AppSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", env_file=str(ENV_FILE), extra="ignore")

    environment: Literal["local", "staging", "production", "test"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    kill_switch: bool = False
    config_version: str = "v1"
    strategy_version: str = "v1"
    display_timezone: str = "Europe/Berlin"


class ApiSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="API_", env_file=str(ENV_FILE), extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000


class DatabaseSettings(_EnvSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATABASE_", env_file=str(ENV_FILE), extra="ignore"
    )

    url: str = (
        "postgresql+asyncpg://polysignal_user:change-me@localhost:5432/polysignal_intelligence"
    )
    pool_size: int = 10
    echo: bool = False


class RedisSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_", env_file=str(ENV_FILE), extra="ignore")

    url: str = "redis://localhost:6379/0"
    key_prefix: str = "polysignal:"


class EndpointWeights(BaseModel):
    """Weighted REST rate-limit costs per endpoint (Polymarket token budget)."""

    instruments: int = 20
    instrument_single: int = 1
    tickers_all: int = 20
    ticker_single: int = 1
    book_depth_10: int = 2
    book_depth_100: int = 5
    book_depth_500: int = 10
    book_depth_1000: int = 20
    klines: int = 5
    trades: int = 5
    funding: int = 2

    def for_book_depth(self, depth: int) -> int:
        if depth <= 10:
            return self.book_depth_10
        if depth <= 100:
            return self.book_depth_100
        if depth <= 500:
            return self.book_depth_500
        return self.book_depth_1000


class PolymarketSettings(_EnvSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLYMARKET_", env_file=str(ENV_FILE), extra="ignore"
    )

    rest_base_url: str = "https://api.perpetuals.polymarket.com"
    request_timeout_seconds: float = 10.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 30.0
    rate_limit_budget_per_minute: int = 800
    instrument_cache_ttl_seconds: float = 300.0
    klines_cache_ttl_seconds: float = 60.0
    endpoint_weights: EndpointWeights = Field(default_factory=EndpointWeights)


class PolymarketWsSettings(_EnvSettings):
    """Public Polymarket Perps WebSocket configuration (read-only market data)."""

    model_config = SettingsConfigDict(
        env_prefix="POLYMARKET_WS_", env_file=str(ENV_FILE), extra="ignore"
    )

    url: str = Field(
        default="wss://ws.perpetuals.polymarket.com/v1/ws",
        validation_alias=AliasChoices("POLYMARKET_PERPS_WS_URL", "POLYMARKET_WS_URL"),
    )
    enabled: bool = True
    connect_timeout_seconds: float = 10.0
    ping_interval_seconds: float = 20.0
    ping_timeout_seconds: float = 10.0
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    reconnect_jitter_seconds: float = 2.0
    #: reconnect attempts allowed inside ``reconnect_window_seconds`` before the
    #: connection is marked DEGRADED (it keeps retrying at max backoff).
    max_reconnect_attempts: int = 10
    reconnect_window_seconds: float = 300.0
    #: hard cap from the public API: 100 subscriptions per connection.
    max_subscriptions: int = 100
    event_queue_size: int = 10_000
    #: depth requested/kept for the in-memory order book per instrument.
    orderbook_depth: int = 100
    persist_batch_size: int = 200
    persist_flush_seconds: float = 5.0
    #: candle timeframes subscribed per instrument.
    kline_timeframes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["1m", "5m", "15m", "1h"]
    )

    @field_validator("kline_timeframes", mode="before")
    @classmethod
    def _parse_timeframes(cls, value: object) -> object:
        return _split_csv(value)


class DataFreshnessSettings(_EnvSettings):
    """Per-channel staleness thresholds in seconds.

    Age <= aging_fraction * threshold  -> FRESH
    Age <= threshold                   -> AGING
    Age >  threshold                   -> STALE
    """

    model_config = SettingsConfigDict(
        env_prefix="DATA_FRESHNESS_", env_file=str(ENV_FILE), extra="ignore"
    )

    ticker_seconds: float = 15.0
    bbo_seconds: float = 10.0
    orderbook_seconds: float = 15.0
    trades_seconds: float = 300.0
    candles_seconds: float = 180.0
    aging_fraction: float = 0.5


class TelegramSettings(_EnvSettings):
    """Telegram delivery + admin command configuration.

    ``enabled=False`` is the safe default: the app starts without any Telegram
    client or polling.  With ``enabled=True`` but missing/invalid token or
    group id, the Telegram subsystem reports DEGRADED while the data engine
    keeps running.
    """

    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM_", env_file=str(ENV_FILE), extra="ignore"
    )

    enabled: bool = False
    bot_token: SecretStr | None = None
    group_id: int | None = None
    admin_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    #: message formatting; HTML is the supported default (escaping handled).
    parse_mode: Literal["HTML"] = "HTML"
    commands_enabled: bool = True
    api_base_url: str = "https://api.telegram.org"
    polling_timeout_seconds: float = 25.0
    #: max open deliveries (PENDING/RETRYING/PROCESSING) before backpressure.
    delivery_queue_size: int = 500
    delivery_batch_size: int = 10
    #: idle worker wake interval; new deliveries also wake the worker directly.
    delivery_flush_seconds: float = 1.0
    #: lease for PROCESSING rows; expired leases are reclaimed on next claim.
    delivery_lease_seconds: float = 60.0
    max_retry_attempts: int = 5
    retry_min_seconds: float = 1.0
    retry_max_seconds: float = 60.0
    retry_jitter_seconds: float = 1.0
    #: conservative send budget per group chat (Telegram allows ~20/min).
    group_messages_per_minute: int = 18
    group_min_interval_seconds: float = 1.5
    edit_min_interval_seconds: float = 3.0
    #: identical content within this window is skipped as duplicate.
    deduplication_window_seconds: float = 300.0
    system_alerts_enabled: bool = True
    status_command_cooldown_seconds: float = 15.0
    admin_command_audit_enabled: bool = True

    @field_validator("bot_token", "group_id", mode="before")
    @classmethod
    def _empty_str_as_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def _parse_ids(cls, value: object) -> object:
        return _split_csv(value)

    @property
    def configured(self) -> bool:
        return self.bot_token is not None and self.group_id is not None

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids


class UniverseSettings(_EnvSettings):
    model_config = SettingsConfigDict(
        env_prefix="UNIVERSE_", env_file=str(ENV_FILE), extra="ignore"
    )

    equity_symbols: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["AAPL-PERP"])
    crypto_symbols: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["BTC-PERP"])
    refresh_enabled: bool = True
    refresh_interval_seconds: float = 300.0
    refresh_jitter_seconds: float = 15.0

    @field_validator("equity_symbols", "crypto_symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, value: object) -> object:
        return _split_csv(value)

    @property
    def all_symbols(self) -> set[str]:
        return {*self.equity_symbols, *self.crypto_symbols}


class DataQualitySettings(_EnvSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATA_QUALITY_", env_file=str(ENV_FILE), extra="ignore"
    )

    #: invalid events per instrument inside ``invalid_event_window_seconds``
    #: after which the instrument is classified DATA_INVALID.
    invalid_event_threshold: int = 20
    invalid_event_window_seconds: float = 60.0
    #: max plausible deviation of an incoming price vs the last mark price.
    outlier_max_deviation_bps: float = Field(
        default=500.0,
        validation_alias=AliasChoices(
            "DATA_OUTLIER_MAX_DEVIATION_BPS", "DATA_QUALITY_OUTLIER_MAX_DEVIATION_BPS"
        ),
    )
    #: mark/index/mid divergence beyond this marks the instrument DEGRADED.
    mark_divergence_degraded_bps: float = 300.0


class MarketSelectionSettings(_EnvSettings):
    """Market selection engine configuration (phase 7).

    Conservative defaults: unknown asset classes are excluded, volume and a
    fresh order book are required, degraded data is NOT acceptable, no
    Telegram notifications.  Invalid JSON fields fail loudly at startup -
    never a silently relaxed policy.
    """

    model_config = SettingsConfigDict(
        env_prefix="MARKET_SELECTION_", env_file=str(ENV_FILE), extra="ignore"
    )

    enabled: bool = True
    refresh_seconds: float = 30.0
    min_quality_score: int = 70
    max_watchlist_size: int = 5
    #: asset classes that may ever become analysable.
    allowed_asset_classes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["EQUITY", "CRYPTO"]
    )
    #: only these symbols may become WATCHLIST_ACTIVE (empty = any discovered).
    default_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["AAPL-PERP", "BTC-PERP"]
    )
    #: denylist always wins over allowlist and overrides.
    denylist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    require_volume: bool = True
    require_fresh_orderbook: bool = True
    allow_degraded_data: bool = False
    notify_state_changes: bool = False
    notify_session_changes: bool = False
    #: per-symbol threshold overrides, e.g. {"AAPL-PERP": {"max_spread_bps": 30}}
    symbol_overrides_json: dict[str, dict[str, float | bool]] = Field(default_factory=dict)
    #: per-asset-class policy, e.g. {"INDEX": {"enabled": true}}
    asset_class_policies_json: dict[str, dict[str, bool]] = Field(default_factory=dict)
    #: cumulative depth window around mid used for the depth checks.
    depth_window_bps: float = 25.0
    #: configurable classification rules (documented in docs/architecture.md):
    #: metadata category -> asset class (primary, high confidence)
    classification_category_map: dict[str, str] = Field(
        default_factory=lambda: {
            "crypto": "CRYPTO",
            "cryptocurrency": "CRYPTO",
            "equity": "EQUITY",
            "equities": "EQUITY",
            "stock": "EQUITY",
            "stocks": "EQUITY",
            "index": "INDEX",
            "indices": "INDEX",
            "commodity": "COMMODITY",
            "commodities": "COMMODITY",
            "fx": "FX",
            "forex": "FX",
            "currency": "FX",
        }
    )
    #: symbol -> asset class fallback (empty by default: no hard-wired symbol
    #: assumptions; operators may add e.g. {"XYZ-PERP": "COMMODITY"}).
    classification_symbol_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("allowed_asset_classes", "default_allowlist", "denylist", mode="before")
    @classmethod
    def _parse_lists(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("allowed_asset_classes")
    @classmethod
    def _validate_asset_classes(cls, value: list[str]) -> list[str]:
        valid = {"EQUITY", "INDEX", "CRYPTO", "COMMODITY", "FX", "UNKNOWN"}
        upper = [item.upper() for item in value]
        unknown = set(upper) - valid
        if unknown:
            raise ValueError(f"invalid asset classes: {sorted(unknown)}")
        return upper

    @field_validator("min_quality_score")
    @classmethod
    def _validate_score(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("min_quality_score must be within 0..100")
        return value


class EquitySessionSettings(_EnvSettings):
    """Equity session + calendar configuration (America/New_York only)."""

    model_config = SettingsConfigDict(env_prefix="EQUITY_", env_file=str(ENV_FILE), extra="ignore")

    calendar_enabled: bool = True
    #: must match the shipped calendar data version; mismatch => CALENDAR_UNAVAILABLE.
    calendar_version: str = "us-equity-2026.1"
    calendar_min_year: int = 2026
    calendar_max_year: int = 2028
    regular_session_open: str = "09:30"
    regular_session_close: str = "16:00"
    early_close_default: str = "13:00"
    premarket_enabled: bool = True
    after_hours_enabled: bool = True
    premarket_analysis_enabled: bool = False
    after_hours_analysis_enabled: bool = False

    @field_validator("regular_session_open", "regular_session_close", "early_close_default")
    @classmethod
    def _validate_time(cls, value: str) -> str:
        from datetime import time as _time

        hour, _, minute = value.partition(":")
        _time(int(hour), int(minute))  # raises on invalid input
        return value


class CryptoSessionSettings(_EnvSettings):
    """Crypto session configuration: 24/7 with optional thin-liquidity windows."""

    model_config = SettingsConfigDict(env_prefix="CRYPTO_", env_file=str(ENV_FILE), extra="ignore")

    session_enabled: bool = True
    #: UTC windows, e.g. [{"days": [5, 6], "start": "22:00", "end": "04:00"}]
    #: (days: 0=Monday .. 6=Sunday; end < start spans midnight)
    thin_liquidity_windows_json: list[dict[str, Any]] = Field(default_factory=list)
    thin_liquidity_policy: Literal["LIMITED_SESSION", "IGNORE"] = "LIMITED_SESSION"

    @field_validator("thin_liquidity_windows_json")
    @classmethod
    def _validate_windows(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from datetime import time as _time

        for window in value:
            for key in ("start", "end"):
                raw = str(window.get(key, ""))
                hour, _, minute = raw.partition(":")
                _time(int(hour), int(minute))  # raises on invalid input
            days = window.get("days", list(range(7)))
            if not all(isinstance(day, int) and 0 <= day <= 6 for day in days):
                raise ValueError("thin liquidity window days must be 0..6")
        return value


class SelectionThresholdSettings(_EnvSettings):
    """Per-asset-class market quality thresholds (pUSD notionals, bps spreads).

    Conservative defaults documented in docs/architecture.md; overridable per
    symbol via MARKET_SELECTION_SYMBOL_OVERRIDES_JSON.
    """

    model_config = SettingsConfigDict(env_file=str(ENV_FILE), extra="ignore")

    equity_max_spread_bps: float = Field(
        default=20.0, validation_alias=AliasChoices("EQUITY_MAX_SPREAD_BPS")
    )
    crypto_max_spread_bps: float = Field(
        default=10.0, validation_alias=AliasChoices("CRYPTO_MAX_SPREAD_BPS")
    )
    equity_min_book_depth_pusd: float = Field(
        default=5_000.0, validation_alias=AliasChoices("EQUITY_MIN_BOOK_DEPTH_PUSD")
    )
    crypto_min_book_depth_pusd: float = Field(
        default=10_000.0, validation_alias=AliasChoices("CRYPTO_MIN_BOOK_DEPTH_PUSD")
    )
    equity_min_volume_24h_pusd: float = Field(
        default=100_000.0, validation_alias=AliasChoices("EQUITY_MIN_VOLUME_24H_PUSD")
    )
    crypto_min_volume_24h_pusd: float = Field(
        default=500_000.0, validation_alias=AliasChoices("CRYPTO_MIN_VOLUME_24H_PUSD")
    )
    equity_max_mark_index_mid_deviation_bps: float = Field(
        default=75.0,
        validation_alias=AliasChoices("EQUITY_MAX_MARK_INDEX_MID_DEVIATION_BPS"),
    )
    crypto_max_mark_index_mid_deviation_bps: float = Field(
        default=50.0,
        validation_alias=AliasChoices("CRYPTO_MAX_MARK_INDEX_MID_DEVIATION_BPS"),
    )


class Settings(BaseModel):
    """Aggregated, fully typed application configuration."""

    app: AppSettings = Field(default_factory=AppSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    polymarket: PolymarketSettings = Field(default_factory=PolymarketSettings)
    ws: PolymarketWsSettings = Field(default_factory=PolymarketWsSettings)
    freshness: DataFreshnessSettings = Field(default_factory=DataFreshnessSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    universe: UniverseSettings = Field(default_factory=UniverseSettings)
    data_quality: DataQualitySettings = Field(default_factory=DataQualitySettings)
    selection: MarketSelectionSettings = Field(default_factory=MarketSelectionSettings)
    equity_sessions: EquitySessionSettings = Field(default_factory=EquitySessionSettings)
    crypto_sessions: CryptoSessionSettings = Field(default_factory=CryptoSessionSettings)
    thresholds: SelectionThresholdSettings = Field(default_factory=SelectionThresholdSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
