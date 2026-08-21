"""Telegram error classification and retry backoff.

Classification (documented contract):

- 429 Too Many Requests      -> retryable; honour ``retry_after`` when given
- network/transport errors   -> retryable (exponential backoff + jitter)
- HTTP 5xx / Telegram 5xx    -> retryable
- 400/403/404 (bad request, bot blocked/kicked, chat not found) -> permanent
- 401 (invalid token)        -> permanent AND degrades the whole subsystem

Retryable failures are bounded by ``max_retry_attempts``; exhausted retries
become DEAD_LETTER, permanent failures become FAILED.  Never an endless loop.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from app.config import TelegramSettings


class TelegramError(Exception):
    """Base error raised by the Telegram client. Carries no secrets."""

    error_class = "telegram_error"

    def __init__(self, message: str = "", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TelegramRetryableError(TelegramError):
    """Transient failure: network error, 5xx, or 429 (with retry_after)."""

    error_class = "retryable"


class TelegramRateLimitedError(TelegramRetryableError):
    error_class = "rate_limited"


class TelegramPermanentError(TelegramError):
    """Non-retryable failure (bad request, forbidden, chat not found)."""

    error_class = "permanent"


class TelegramAuthError(TelegramPermanentError):
    """Invalid bot token: permanent and degrades the subsystem."""

    error_class = "unauthorized"


@dataclass(frozen=True)
class RetryDecision:
    """Outcome of classifying one failed attempt.

    ``retryable`` describes the ERROR NATURE only; the worker additionally
    bounds attempts: a retryable error with exhausted attempts becomes
    DEAD_LETTER, a permanent error becomes FAILED.
    """

    retryable: bool
    delay_seconds: float
    error_class: str


def decide_retry(
    error: Exception,
    attempt: int,
    settings: TelegramSettings,
    rng: Callable[[], float] = random.random,
) -> RetryDecision:
    """Classify an error and compute the backoff delay for the next attempt.

    ``attempt`` is the number of attempts already made (>= 1); it drives the
    exponential backoff, not the retryability itself.
    """
    if isinstance(error, TelegramPermanentError):
        return RetryDecision(False, 0.0, error.error_class)

    error_class = getattr(error, "error_class", "transport")
    if not isinstance(error, TelegramError):
        # unknown exception from the transport layer: transient but bounded
        error_class = "transport"

    base = min(
        settings.retry_min_seconds * (2 ** max(0, attempt - 1)),
        settings.retry_max_seconds,
    )
    delay = base + settings.retry_jitter_seconds * rng()
    retry_after = getattr(error, "retry_after", None)
    if retry_after is not None:
        delay = max(delay, float(retry_after))
    return RetryDecision(True, min(delay, settings.retry_max_seconds * 2), error_class)
