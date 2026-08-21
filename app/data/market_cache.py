"""Redis-backed runtime cache with an enforced project key prefix.

All keys are namespaced with the configured prefix (default ``polysignal:``)
so this instance never collides with other projects sharing a Redis server.

Documented key layout (all under the prefix):

- ``freshness:{source}:{channel}:{symbol}``  ISO timestamp of last valid event
- ``ticker:{instrument_id}``                 latest valid ticker (JSON)
- ``bbo:{instrument_id}``                    latest valid BBO (JSON)
- ``orderbook:status:{instrument_id}``       orderbook freshness status string
- ``quality:{instrument_id}``                instrument data quality status
- ``ws:state``                               current WebSocket connection state
- ``heartbeat:{name}``                       liveness timestamps (ISO)
- ``json:{key}``                             generic JSON blobs

Availability: Redis being down must never crash the feed.  Every operation is
wrapped; failures set ``degraded`` and are counted, reads return ``None``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from redis.asyncio import Redis

from app.config import RedisSettings
from app.domain.enums import DataSource, FreshnessStatus, WsConnectionState
from app.observability.logging import get_logger
from app.observability.metrics import metrics

T = TypeVar("T")


class MarketCache:
    def __init__(self, redis: Redis, settings: RedisSettings) -> None:
        self._redis = redis
        self._prefix = settings.key_prefix
        self._log = get_logger("market_cache")
        self.degraded = False
        self.failure_count = 0

    @classmethod
    def from_settings(cls, settings: RedisSettings) -> MarketCache:
        redis: Redis = Redis.from_url(settings.url, decode_responses=True)
        return cls(redis, settings)

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def _safe(self, operation: Callable[[], Awaitable[T]]) -> T | None:
        """Run a redis operation; failures degrade the cache, never the process."""
        try:
            result = await operation()
        except Exception as exc:
            self.failure_count += 1
            if not self.degraded:
                self._log.warning("cache_unavailable", error=type(exc).__name__)
            self.degraded = True
            metrics.increment("cache.failures")
            return None
        if self.degraded:
            self._log.info("cache_recovered")
        self.degraded = False
        return result

    async def ping(self) -> bool:
        result = await self._safe(lambda: self._redis.ping())
        return bool(result)

    async def aclose(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:
            pass

    # ------------------------------------------------------------- freshness

    async def set_last_update(
        self, source: DataSource, channel: str, symbol: str, ts: datetime | None = None
    ) -> None:
        ts = ts or datetime.now(tz=UTC)
        key = self._key(f"freshness:{source.value}:{channel}:{symbol}")
        await self._safe(lambda: self._redis.set(key, ts.isoformat()))

    async def get_last_update(
        self, source: DataSource, channel: str, symbol: str
    ) -> datetime | None:
        key = self._key(f"freshness:{source.value}:{channel}:{symbol}")
        raw = await self._safe(lambda: self._redis.get(key))
        if raw is None:
            return None
        text = raw.decode() if isinstance(raw, bytes) else raw
        return datetime.fromisoformat(text)

    # --------------------------------------------------------- market state

    async def set_ticker(self, instrument_id: int, payload: dict[str, Any]) -> None:
        key = self._key(f"ticker:{instrument_id}")
        await self._safe(lambda: self._redis.set(key, json.dumps(payload)))

    async def set_bbo(self, instrument_id: int, payload: dict[str, Any]) -> None:
        key = self._key(f"bbo:{instrument_id}")
        await self._safe(lambda: self._redis.set(key, json.dumps(payload)))

    async def set_orderbook_status(self, instrument_id: int, status: FreshnessStatus) -> None:
        key = self._key(f"orderbook:status:{instrument_id}")
        await self._safe(lambda: self._redis.set(key, status.value))

    async def set_quality_status(self, instrument_id: int, status: str) -> None:
        key = self._key(f"quality:{instrument_id}")
        await self._safe(lambda: self._redis.set(key, status))

    async def set_ws_state(self, state: WsConnectionState) -> None:
        key = self._key("ws:state")
        await self._safe(lambda: self._redis.set(key, state.value))

    async def heartbeat(self, name: str, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(tz=UTC)
        key = self._key(f"heartbeat:{name}")
        await self._safe(lambda: self._redis.set(key, ts.isoformat()))

    # ------------------------------------------------------------------ json

    async def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        full_key = self._key(f"json:{key}")
        await self._safe(lambda: self._redis.set(full_key, json.dumps(value), ex=ttl_seconds))

    async def get_json(self, key: str) -> Any | None:
        full_key = self._key(f"json:{key}")
        raw = await self._safe(lambda: self._redis.get(full_key))
        if raw is None:
            return None
        text = raw.decode() if isinstance(raw, bytes) else raw
        return json.loads(text)
