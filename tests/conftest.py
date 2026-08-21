"""Shared fixtures. No test performs real network or Telegram calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import (
    ApiSettings,
    AppSettings,
    DatabaseSettings,
    DataQualitySettings,
    PolymarketSettings,
    RedisSettings,
    Settings,
    TelegramSettings,
    UniverseSettings,
)
from app.repositories.orm import Base

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock:
    """Deterministic monotonic clock for rate limiter / cache tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_settings(**overrides: Any) -> Settings:
    """Build Settings that ignore any local .env file (test isolation)."""
    defaults: dict[str, Any] = {
        "app": AppSettings(_env_file=None),
        "api": ApiSettings(_env_file=None),
        "database": DatabaseSettings(_env_file=None),
        "redis": RedisSettings(_env_file=None),
        "polymarket": PolymarketSettings(_env_file=None),
        "telegram": TelegramSettings(_env_file=None),
        "universe": UniverseSettings(_env_file=None),
        "data_quality": DataQualitySettings(_env_file=None),
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def instruments_payload() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "instruments.json").read_text())


@pytest.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)
