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
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
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
    ws_url: str = "wss://ws.perpetuals.polymarket.com/v1/ws"
    request_timeout_seconds: float = 10.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 30.0
    rate_limit_budget_per_minute: int = 800
    instrument_cache_ttl_seconds: float = 300.0
    klines_cache_ttl_seconds: float = 60.0
    endpoint_weights: EndpointWeights = Field(default_factory=EndpointWeights)


class TelegramSettings(_EnvSettings):
    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM_", env_file=str(ENV_FILE), extra="ignore"
    )

    bot_token: SecretStr | None = None
    group_id: int | None = None
    admin_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

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

    ticker_stale_after_seconds: float = 30.0
    orderbook_stale_after_seconds: float = 30.0
    candle_stale_after_seconds: float = 180.0


class Settings(BaseModel):
    """Aggregated, fully typed application configuration."""

    app: AppSettings = Field(default_factory=AppSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    polymarket: PolymarketSettings = Field(default_factory=PolymarketSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    universe: UniverseSettings = Field(default_factory=UniverseSettings)
    data_quality: DataQualitySettings = Field(default_factory=DataQualitySettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
