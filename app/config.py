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


class StrategySettings(_EnvSettings):
    """Strategy research foundation configuration (phase 8).

    Conservative defaults; invalid values (weight sums, percentiles,
    timeframe sets) fail loudly at startup - never a silently relaxed rule.
    The full model feeds the deterministic configuration hash.
    """

    model_config = SettingsConfigDict(
        env_prefix="STRATEGY_", env_file=str(ENV_FILE), extra="ignore"
    )

    enabled: bool = True
    name: str = "market_structure_v1"
    version: str = "1.0.0"
    evaluation_refresh_seconds: float = 20.0
    max_concurrent_evaluations: int = 4
    require_active_watchlist: bool = True
    require_healthy_data: bool = True
    allow_degraded_data: bool = False
    require_fresh_orderbook: bool = True
    min_setup_score: int = 75
    candidate_dedupe_seconds: float = 1800.0
    #: identical rejection (instrument+code) is aggregated within this window.
    rejection_persist_seconds: float = 300.0
    max_active_candidates_per_instrument: int = 2
    #: candidate evaluation expiry: N closed 5m candles without confirmation.
    expire_after_5m_candles: int = 6
    persist_feature_snapshots: bool = True
    feature_retention_days: int = 14

    # --- candle requirements ---------------------------------------------
    required_timeframes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["5m", "15m", "1h"]
    )
    min_candles_1m: int = 60
    min_candles_5m: int = 60
    min_candles_15m: int = 40
    min_candles_1h: int = 30
    #: consecutive candle spacing beyond tf * multiplier counts as a gap.
    max_candle_gap_multiplier: float = 1.5
    allow_1m_optional: bool = True

    # --- structure / liquidity -------------------------------------------
    swing_left_bars: int = 2
    swing_right_bars: int = 2
    swing_atr_multiplier: float = 0.5
    equal_level_tolerance_bps: float = 5.0
    structure_break_confirmation_closes: int = 1
    liquidity_level_lookback: int = 50
    sweep_min_overshoot_bps: float = 3.0
    reclaim_tolerance_bps: float = 2.0
    retest_tolerance_bps: float = 10.0
    retest_max_5m_candles: int = 12

    # --- volatility / momentum / volume ----------------------------------
    atr_period: int = 14
    volatility_lookback: int = 100
    high_volatility_percentile: float = 90.0
    momentum_lookback: int = 10
    volume_lookback: int = 20
    min_relative_volume: float = 1.2
    require_volume_confirmation: bool = False
    require_momentum_confirmation: bool = True

    # --- scoring / regime / overrides -------------------------------------
    #: weights must sum to exactly 100.
    score_weights_json: dict[str, int] = Field(
        default_factory=lambda: {
            "htf_bias": 20,
            "structure_15m": 20,
            "liquidity_event": 15,
            "local_confirmation_5m": 15,
            "momentum": 10,
            "volume": 10,
            "market_context": 5,
            "volatility_fit": 5,
        }
    )
    regime_rules_json: dict[str, float] = Field(
        default_factory=lambda: {
            "trend_efficiency_ratio_min": 0.35,
            "range_efficiency_ratio_max": 0.2,
            "breakout_lookback": 20,
            "trend_lookback": 30,
            "low_liquidity_quality_below": 60.0,
        }
    )
    symbol_overrides_json: dict[str, dict[str, float | bool | int]] = Field(default_factory=dict)
    event_risk_enabled: bool = False
    #: UTC windows [{"start": "2026-09-17T17:00:00Z", "end": "...", "label": "FOMC"}]
    event_risk_windows_json: list[dict[str, str]] = Field(default_factory=list)

    @field_validator("required_timeframes", mode="before")
    @classmethod
    def _parse_timeframes(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("required_timeframes")
    @classmethod
    def _validate_timeframes(cls, value: list[str]) -> list[str]:
        valid = {"1m", "5m", "15m", "1h"}
        unknown = set(value) - valid
        if unknown:
            raise ValueError(f"invalid timeframes: {sorted(unknown)}")
        for mandatory in ("5m", "15m", "1h"):
            if mandatory not in value:
                raise ValueError(f"timeframe {mandatory} is mandatory for candidates")
        return value

    @field_validator("score_weights_json")
    @classmethod
    def _validate_weights(cls, value: dict[str, int]) -> dict[str, int]:
        expected = {
            "htf_bias",
            "structure_15m",
            "liquidity_event",
            "local_confirmation_5m",
            "momentum",
            "volume",
            "market_context",
            "volatility_fit",
        }
        if set(value) != expected:
            raise ValueError(f"score weights must define exactly {sorted(expected)}")
        total = sum(value.values())
        if total != 100:
            raise ValueError(f"score weights must sum to 100, got {total}")
        if any(weight < 0 for weight in value.values()):
            raise ValueError("score weights must be non-negative")
        return value

    @field_validator("high_volatility_percentile")
    @classmethod
    def _validate_percentile(cls, value: float) -> float:
        if not 50.0 <= value <= 100.0:
            raise ValueError("high_volatility_percentile must be within 50..100")
        return value

    @field_validator("min_setup_score")
    @classmethod
    def _validate_min_score(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("min_setup_score must be within 0..100")
        return value


class CostSettings(_EnvSettings):
    """Cost engine configuration (phase 9).

    Rate conventions: fields ending in ``_rate`` are decimal fractions
    (0.0005 = 0.05 %); fields ending in ``_bps`` are basis points; fields
    ending in ``_pct`` are percent points (15.0 = 15 %).  Conservative
    defaults; invalid values fail loudly at startup.
    """

    model_config = SettingsConfigDict(env_prefix="COST_", env_file=str(ENV_FILE), extra="ignore")

    engine_enabled: bool = Field(default=True, validation_alias=AliasChoices("COST_ENGINE_ENABLED"))
    model_name: str = "conservative_costs_v1"
    model_version: str = "1.0.0"

    # --- fee schedule (assumption, validate against official docs) ---------
    fee_schedule_source: str = "local_config_assumption"
    fee_schedule_version: str = "polymarket-perps-assumed-1"
    assumed_fee_tier: str = "default"
    #: decimal fraction, e.g. 0.0002 = 0.02 %
    default_maker_fee_rate: float = 0.0002
    #: decimal fraction, e.g. 0.0007 = 0.07 %
    default_taker_fee_rate: float = 0.0007
    require_active_fee_schedule: bool = True

    # --- execution assumptions --------------------------------------------
    execution_assumption_version: str = "taker-conservative-1"
    entry_execution_mode: Literal["ENTRY_TAKER", "ENTRY_MAKER"] = "ENTRY_TAKER"
    target_execution_mode: Literal["TARGET_TAKER", "TARGET_MAKER"] = "TARGET_TAKER"
    invalidation_execution_mode: Literal["STOP_TAKER", "STOP_STRESS_TAKER"] = "STOP_STRESS_TAKER"

    # --- slippage / orderbook ----------------------------------------------
    slippage_enabled: bool = True
    require_fresh_orderbook: bool = True
    orderbook_max_levels: int = 25
    orderbook_max_distance_bps: float = 50.0
    entry_slippage_stress_bps: float = 2.0
    target_slippage_stress_bps: float = 2.0
    invalidation_slippage_stress_bps: float = 8.0
    #: percent points: slippage cost may consume at most this share of risk
    max_slippage_to_risk_pct: float = 8.0

    # --- funding -------------------------------------------------------------
    funding_enabled: bool = True
    require_funding_data: bool = True
    funding_lookback_hours: int = 72
    #: conservative percentile of the adverse funding-rate history (0..100)
    funding_conservative_percentile: float = 75.0
    #: technical, conservative research hold assumption - not a forecast
    reference_hold_minutes: int = 240
    equity_overnight_blocked: bool = True
    #: extra buffer applied when funding data is thin (bps of notional)
    funding_buffer_bps: float = 1.0
    #: assumed funding interval when the instrument does not specify one
    default_funding_interval_hours: float = 1.0

    #: additional flat conservative cost buffer (bps of notional, per plan)
    conservative_cost_buffer_bps: float = 1.0

    @field_validator("default_maker_fee_rate", "default_taker_fee_rate")
    @classmethod
    def _validate_fee_rates(cls, value: float) -> float:
        if not 0.0 <= value <= 0.05:
            raise ValueError("fee rates are decimal fractions and must be within 0..0.05")
        return value

    @field_validator("funding_conservative_percentile")
    @classmethod
    def _validate_funding_percentile(cls, value: float) -> float:
        if not 50.0 <= value <= 100.0:
            raise ValueError("funding_conservative_percentile must be within 50..100")
        return value

    @field_validator("orderbook_max_levels", "reference_hold_minutes", "funding_lookback_hours")
    @classmethod
    def _validate_positive_ints(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("value must be positive")
        return value

    @field_validator(
        "orderbook_max_distance_bps",
        "entry_slippage_stress_bps",
        "target_slippage_stress_bps",
        "invalidation_slippage_stress_bps",
        "max_slippage_to_risk_pct",
        "funding_buffer_bps",
        "conservative_cost_buffer_bps",
        "default_funding_interval_hours",
    )
    @classmethod
    def _validate_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("value must be non-negative")
        return value


class RiskSettings(_EnvSettings):
    """Risk engine configuration (phase 9).

    All monetary values are hypothetical research references in pUSD - the
    engine never reads real account, balance or position data.  Rate
    conventions: ``_pct`` fields are percent points (0.5 = 0.5 %), ``_bps``
    fields are basis points, ``_rate`` fields are decimal fractions.
    """

    model_config = SettingsConfigDict(env_prefix="RISK_", env_file=str(ENV_FILE), extra="ignore")

    engine_enabled: bool = Field(default=True, validation_alias=AliasChoices("RISK_ENGINE_ENABLED"))
    model_name: str = "conservative_eligibility_v1"
    model_version: str = "1.0.0"
    plan_evaluation_refresh_seconds: float = 20.0
    max_concurrent_evaluations: int = 4
    require_confirmed_candidate: bool = True
    require_healthy_data: bool = True
    allow_degraded_data: bool = False
    require_fresh_orderbook: bool = True
    plan_dedupe_seconds: float = 900.0
    max_active_plans_per_instrument: int = 2
    plan_expiry_seconds: float = 1800.0
    #: identical rejections (candidate+code) aggregate within this window
    rejection_persist_seconds: float = 300.0
    eligibility_min_score: int = 75

    # --- virtual reference account and technical risk ----------------------
    #: hypothetical reference account in pUSD - never a real balance
    virtual_reference_account_pusd: float = 10_000.0
    #: percent points of the virtual account risked per plan (0.5 = 0.5 %)
    reference_risk_per_plan_pct: float = 0.5
    min_distance_bps: float = 15.0
    max_distance_bps: float = 400.0
    min_distance_atr_multiple: float = 0.5
    max_distance_atr_multiple: float = 4.0
    invalidation_buffer_bps: float = 5.0
    invalidation_buffer_atr_multiple: float = 0.25
    #: minimum relevance (0..1) a level needs to qualify as reference target
    target_min_relevance_score: float = 0.3
    #: entry may not chase further than this beyond the technical zone
    max_entry_chase_bps: float = 20.0
    max_reference_notional_pusd: float = 50_000.0
    #: hard V1 research cap - values above 3 are rejected at startup
    max_reference_leverage: float = 3.0
    min_net_rr: float = 1.8
    #: percent points: total costs may consume at most this share of risk
    max_cost_to_risk_pct: float = 15.0

    # --- margin / liquidation ----------------------------------------------
    require_instrument_margin_data: bool = True
    allow_approximated_margin_model: bool = False
    #: decimal fraction, only used by the opt-in approximated model
    approx_initial_margin_rate: float = 0.34
    #: decimal fraction, only used by the opt-in approximated model
    approx_maintenance_margin_rate: float = 0.1
    min_liquidation_buffer_bps: float = 150.0
    min_liquidation_buffer_atr_multiple: float = 1.0
    margin_stress_buffer_bps: float = 50.0
    isolated_margin_only: bool = True

    # --- eligibility score ---------------------------------------------------
    #: weights must sum to exactly 100
    eligibility_score_weights_json: dict[str, int] = Field(
        default_factory=lambda: {
            "invalidation_quality": 20,
            "target_quality": 15,
            "net_rr": 25,
            "executability": 15,
            "cost_quality": 10,
            "margin_buffer": 10,
            "data_confidence": 5,
        }
    )
    symbol_overrides_json: dict[str, dict[str, float | bool | int]] = Field(default_factory=dict)
    asset_class_overrides_json: dict[str, dict[str, float | bool | int]] = Field(
        default_factory=dict
    )

    @field_validator("eligibility_score_weights_json")
    @classmethod
    def _validate_weights(cls, value: dict[str, int]) -> dict[str, int]:
        expected = {
            "invalidation_quality",
            "target_quality",
            "net_rr",
            "executability",
            "cost_quality",
            "margin_buffer",
            "data_confidence",
        }
        if set(value) != expected:
            raise ValueError(f"eligibility weights must define exactly {sorted(expected)}")
        total = sum(value.values())
        if total != 100:
            raise ValueError(f"eligibility weights must sum to 100, got {total}")
        if any(weight < 0 for weight in value.values()):
            raise ValueError("eligibility weights must be non-negative")
        return value

    @field_validator("max_reference_leverage")
    @classmethod
    def _validate_leverage_cap(cls, value: float) -> float:
        if not 1.0 <= value <= 3.0:
            raise ValueError("max_reference_leverage is hard-capped to 1..3 in V1")
        return value

    @field_validator("eligibility_min_score")
    @classmethod
    def _validate_min_score(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("eligibility_min_score must be within 0..100")
        return value

    @field_validator("reference_risk_per_plan_pct")
    @classmethod
    def _validate_risk_pct(cls, value: float) -> float:
        if not 0.0 < value <= 5.0:
            raise ValueError("reference_risk_per_plan_pct (percent points) must be in (0, 5]")
        return value

    @field_validator("max_cost_to_risk_pct")
    @classmethod
    def _validate_cost_pct(cls, value: float) -> float:
        if not 0.0 < value <= 100.0:
            raise ValueError("max_cost_to_risk_pct must be in (0, 100]")
        return value

    @field_validator("approx_initial_margin_rate", "approx_maintenance_margin_rate")
    @classmethod
    def _validate_margin_rates(cls, value: float) -> float:
        if not 0.0 < value < 1.0:
            raise ValueError("margin rates are decimal fractions and must be in (0, 1)")
        return value

    @field_validator("virtual_reference_account_pusd", "max_reference_notional_pusd")
    @classmethod
    def _validate_positive_amounts(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("reference amounts must be positive")
        return value

    @field_validator("min_net_rr")
    @classmethod
    def _validate_min_rr(cls, value: float) -> float:
        if value < 1.0:
            raise ValueError("min_net_rr below 1.0 is not a conservative configuration")
        return value

    @field_validator(
        "min_distance_bps",
        "max_distance_bps",
        "min_distance_atr_multiple",
        "max_distance_atr_multiple",
        "invalidation_buffer_bps",
        "invalidation_buffer_atr_multiple",
        "min_liquidation_buffer_bps",
        "min_liquidation_buffer_atr_multiple",
        "margin_stress_buffer_bps",
        "max_entry_chase_bps",
    )
    @classmethod
    def _validate_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("value must be non-negative")
        return value


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
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    costs: CostSettings = Field(default_factory=CostSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
