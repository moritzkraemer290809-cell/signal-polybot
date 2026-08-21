"""Asynchronous batch buffer for market-data persistence.

Decouples the real-time feed from the database:
- records are appended non-blockingly (bounded buffer, drop-oldest on overflow)
- a background task flushes by batch size or flush interval
- optional dedup key keeps only the latest record per key (e.g. the currently
  forming candle)
- flush failures never crash the feed: the buffer marks itself degraded,
  keeps the records (up to capacity) and retries on the next interval
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from app.observability.logging import get_logger
from app.observability.metrics import metrics


class PersistenceBuffer[T]:
    def __init__(
        self,
        name: str,
        flush: Callable[[list[T]], Awaitable[None]],
        *,
        batch_size: int = 200,
        flush_interval_seconds: float = 5.0,
        max_buffered: int = 10_000,
        dedup_key: Callable[[T], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._flush = flush
        self._batch_size = batch_size
        self._flush_interval = flush_interval_seconds
        self._max_buffered = max_buffered
        self._dedup_key = dedup_key
        self._clock = clock
        self._log = get_logger("persistence_buffer", buffer=name)

        self._records: OrderedDict[object, T] = OrderedDict()
        self._auto_key = 0
        self._task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()

        self.dropped_records = 0
        self.flushed_records = 0
        self.failed_flushes = 0
        self.degraded = False
        self.last_flush_ok_at: float | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"buffer_{self.name}")

    async def stop(self) -> None:
        """Stop the background task and attempt one final flush."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._try_flush()

    # -------------------------------------------------------------- append

    def append(self, record: T) -> None:
        if self._dedup_key is not None:
            key: object = self._dedup_key(record)
        else:
            self._auto_key += 1
            key = self._auto_key
        if key in self._records:
            self._records.pop(key)
        elif len(self._records) >= self._max_buffered:
            self._records.popitem(last=False)
            self.dropped_records += 1
            metrics.increment(f"buffer.{self.name}.dropped")
        self._records[key] = record
        if len(self._records) >= self._batch_size:
            self._wakeup.set()

    def __len__(self) -> int:
        return len(self._records)

    # --------------------------------------------------------------- flush

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._flush_interval)
            self._wakeup.clear()
            await self._try_flush()

    async def _try_flush(self) -> None:
        if not self._records:
            return
        batch_keys = list(self._records.keys())[: self._batch_size]
        batch = [self._records[key] for key in batch_keys]
        try:
            await self._flush(batch)
        except Exception as exc:
            self.failed_flushes += 1
            if not self.degraded:
                self._log.error(
                    "buffer_flush_failed", error=type(exc).__name__, pending=len(self._records)
                )
            self.degraded = True
            metrics.increment(f"buffer.{self.name}.failed_flushes")
            return
        for key in batch_keys:
            self._records.pop(key, None)
        self.flushed_records += len(batch)
        if self.degraded:
            self._log.info("buffer_flush_recovered", flushed=len(batch))
        self.degraded = False
        self.last_flush_ok_at = self._clock()
        metrics.increment(f"buffer.{self.name}.flushed", float(len(batch)))
        # more waiting? flush again promptly
        if len(self._records) >= self._batch_size:
            self._wakeup.set()

    def stats(self) -> dict[str, float | int | bool]:
        return {
            "pending": len(self._records),
            "flushed": self.flushed_records,
            "dropped": self.dropped_records,
            "failed_flushes": self.failed_flushes,
            "degraded": self.degraded,
        }
