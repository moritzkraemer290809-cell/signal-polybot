"""Redis-backed runtime cache with an enforced project key prefix.

Used for cross-process runtime state: last-update timestamps per data source
and instrument (data freshness), and small JSON snapshots.  All keys are
namespaced with the configured prefix (default ``polysignal:``) so the
instance never collides with other projects sharing a Redis server.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.config import RedisSettings
from app.domain.enums import DataSource


class MarketCache:
    def __init__(self, redis: Redis, settings: RedisSettings) -> None:
        self._redis = redis
        self._prefix = settings.key_prefix

    @classmethod
    def from_settings(cls, settings: RedisSettings) -> MarketCache:
        redis: Redis = Redis.from_url(settings.url, decode_responses=True)
        return cls(redis, settings)

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._redis.aclose()

    # ------------------------------------------------------------- freshness

    async def set_last_update(
        self, source: DataSource, channel: str, symbol: str, ts: datetime | None = None
    ) -> None:
        ts = ts or datetime.now(tz=UTC)
        await self._redis.set(
            self._key(f"freshness:{source.value}:{channel}:{symbol}"), ts.isoformat()
        )

    async def get_last_update(
        self, source: DataSource, channel: str, symbol: str
    ) -> datetime | None:
        raw = await self._redis.get(self._key(f"freshness:{source.value}:{channel}:{symbol}"))
        if raw is None:
            return None
        text = raw.decode() if isinstance(raw, bytes) else raw
        return datetime.fromisoformat(text)

    # ------------------------------------------------------------------ json

    async def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        await self._redis.set(self._key(key), json.dumps(value), ex=ttl_seconds)

    async def get_json(self, key: str) -> Any | None:
        raw = await self._redis.get(self._key(key))
        return None if raw is None else json.loads(raw)
