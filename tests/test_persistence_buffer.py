"""Persistence buffer: batching, dedup, overflow, failure resilience."""

from __future__ import annotations

import asyncio

from app.data.persistence_buffer import PersistenceBuffer


async def test_flush_by_batch_size() -> None:
    flushed: list[list[int]] = []

    async def flush(batch: list[int]) -> None:
        flushed.append(list(batch))

    buffer = PersistenceBuffer("t", flush, batch_size=3, flush_interval_seconds=999)
    await buffer.start()
    try:
        for value in range(3):
            buffer.append(value)
        for _ in range(100):
            await asyncio.sleep(0.005)
            if flushed:
                break
        assert flushed == [[0, 1, 2]]
        assert len(buffer) == 0
    finally:
        await buffer.stop()


async def test_flush_by_interval() -> None:
    flushed: list[list[int]] = []

    async def flush(batch: list[int]) -> None:
        flushed.append(list(batch))

    buffer = PersistenceBuffer("t", flush, batch_size=100, flush_interval_seconds=0.02)
    await buffer.start()
    try:
        buffer.append(1)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if flushed:
                break
        assert flushed == [[1]]
    finally:
        await buffer.stop()


async def test_dedup_keeps_latest_record() -> None:
    flushed: list[list[tuple[int, str]]] = []

    async def flush(batch):
        flushed.append(list(batch))

    buffer = PersistenceBuffer(
        "t", flush, batch_size=10, flush_interval_seconds=999, dedup_key=lambda r: r[0]
    )
    buffer.append((1, "old"))
    buffer.append((1, "new"))
    buffer.append((2, "x"))
    assert len(buffer) == 2
    await buffer.stop()  # final flush
    assert flushed == [[(1, "new"), (2, "x")]]


async def test_overflow_drops_oldest() -> None:
    async def flush(batch):
        raise AssertionError("should not flush")

    buffer = PersistenceBuffer(
        "t", flush, batch_size=100, flush_interval_seconds=999, max_buffered=2
    )
    buffer.append(1)
    buffer.append(2)
    buffer.append(3)
    assert len(buffer) == 2
    assert buffer.dropped_records == 1


async def test_flush_failure_marks_degraded_and_recovers() -> None:
    fail = {"value": True}
    flushed: list[list[int]] = []

    async def flush(batch):
        if fail["value"]:
            raise ConnectionError("db down")
        flushed.append(list(batch))

    buffer = PersistenceBuffer("t", flush, batch_size=2, flush_interval_seconds=999)
    buffer.append(1)
    buffer.append(2)
    await buffer._try_flush()
    assert buffer.degraded is True
    assert buffer.failed_flushes == 1
    assert len(buffer) == 2  # records retained

    fail["value"] = False
    await buffer._try_flush()
    assert buffer.degraded is False
    assert flushed == [[1, 2]]
    assert len(buffer) == 0


async def test_stop_performs_final_flush() -> None:
    flushed: list[list[int]] = []

    async def flush(batch):
        flushed.append(list(batch))

    buffer = PersistenceBuffer("t", flush, batch_size=100, flush_interval_seconds=999)
    await buffer.start()
    buffer.append(7)
    await buffer.stop()
    assert flushed == [[7]]
