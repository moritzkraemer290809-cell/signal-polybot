"""Per-chat Telegram rate limiter (sends and edits).

Enforces, per chat:
- at most ``group_messages_per_minute`` new messages in a sliding window
- ``group_min_interval_seconds`` between new messages
- a separate, more conservative ``edit_min_interval_seconds`` between edits

The limiter is pure computation over an injectable monotonic clock: callers
ask for the required wait time and record completed operations.  The queue
worker sleeps exactly that long - no busy loops.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from app.config import TelegramSettings


@dataclass
class _ChatWindow:
    send_times: deque[float] = field(default_factory=deque)
    last_send: float | None = None
    last_edit: float | None = None


class TelegramRateLimiter:
    def __init__(
        self,
        settings: TelegramSettings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._chats: dict[int, _ChatWindow] = {}
        self.total_wait_seconds = 0.0

    def _window(self, chat_id: int) -> _ChatWindow:
        if chat_id not in self._chats:
            self._chats[chat_id] = _ChatWindow()
        return self._chats[chat_id]

    def send_wait_seconds(self, chat_id: int) -> float:
        """Seconds to wait before a new message may be sent to this chat."""
        now = self._clock()
        window = self._window(chat_id)
        waits = [0.0]
        if window.last_send is not None:
            waits.append(window.last_send + self._settings.group_min_interval_seconds - now)
        while window.send_times and now - window.send_times[0] > 60.0:
            window.send_times.popleft()
        if len(window.send_times) >= self._settings.group_messages_per_minute:
            waits.append(window.send_times[0] + 60.0 - now)
        return max(waits)

    def edit_wait_seconds(self, chat_id: int) -> float:
        """Seconds to wait before the next edit in this chat."""
        now = self._clock()
        window = self._window(chat_id)
        if window.last_edit is None:
            return 0.0
        return max(0.0, window.last_edit + self._settings.edit_min_interval_seconds - now)

    def record_send(self, chat_id: int) -> None:
        now = self._clock()
        window = self._window(chat_id)
        window.send_times.append(now)
        window.last_send = now

    def record_edit(self, chat_id: int) -> None:
        self._window(chat_id).last_edit = self._clock()

    def record_wait(self, seconds: float) -> None:
        if seconds > 0:
            self.total_wait_seconds += seconds
