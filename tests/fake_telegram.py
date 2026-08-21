"""Shared fake Telegram client and helpers for phase-6 tests.

All payload values here are clearly local test data - no real market or
trading parameters.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.update_batches: deque[list[dict[str, Any]]] = deque()
        self.failures: deque[Exception] = deque()
        self.get_updates_calls: list[int | None] = []
        self._next_message_id = 100
        self.closed = False

    def fail_next(self, *errors: Exception) -> None:
        self.failures.extend(errors)

    def queue_updates(self, batch: list[dict[str, Any]]) -> None:
        self.update_batches.append(batch)

    async def send_message(self, chat_id: int, text: str, parse_mode: str) -> int:
        if self.failures:
            raise self.failures.popleft()
        self._next_message_id += 1
        self.sent.append((chat_id, text))
        return self._next_message_id

    async def edit_message(self, chat_id: int, message_id: int, text: str, parse_mode: str) -> None:
        if self.failures:
            raise self.failures.popleft()
        self.edited.append((chat_id, message_id, text))

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[dict[str, Any]]:
        self.get_updates_calls.append(offset)
        if self.failures:
            raise self.failures.popleft()
        if self.update_batches:
            return self.update_batches.popleft()
        await asyncio.Event().wait()  # block like real long polling
        return []

    async def get_me(self) -> dict[str, Any]:
        return {"username": "polysignal_test_bot"}

    async def aclose(self) -> None:
        self.closed = True


def command_update(
    command: str,
    user_id: int = 1,
    chat_id: int = -100_123,
    chat_type: str = "supergroup",
    update_id: int = 1,
    date: int = 2_000_000_000,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "date": date,
            "text": command,
            "from": {"id": user_id},
            "chat": {"id": chat_id, "type": chat_type},
        },
    }
