"""Error classification and backoff computation."""

from __future__ import annotations

import pytest

from app.config import TelegramSettings
from app.telegram.retry import (
    TelegramAuthError,
    TelegramPermanentError,
    TelegramRateLimitedError,
    TelegramRetryableError,
    decide_retry,
)

settings = TelegramSettings(
    _env_file=None,
    retry_min_seconds=1.0,
    retry_max_seconds=30.0,
    retry_jitter_seconds=0.0,
    max_retry_attempts=5,
)


def test_permanent_errors_are_not_retryable() -> None:
    for error in (TelegramPermanentError("400: bad request"), TelegramAuthError("401")):
        decision = decide_retry(error, attempt=1, settings=settings, rng=lambda: 0.0)
        assert decision.retryable is False


def test_transient_errors_use_exponential_backoff() -> None:
    delays = [
        decide_retry(
            TelegramRetryableError("transport"), attempt, settings, rng=lambda: 0.0
        ).delay_seconds
        for attempt in (1, 2, 3, 4)
    ]
    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_backoff_is_capped_and_jittered() -> None:
    jittered = TelegramSettings(
        _env_file=None, retry_min_seconds=1.0, retry_max_seconds=10.0, retry_jitter_seconds=2.0
    )
    decision = decide_retry(
        TelegramRetryableError("transport"), attempt=10, settings=jittered, rng=lambda: 0.5
    )
    assert decision.delay_seconds == pytest.approx(10.0 + 1.0)


def test_retry_after_takes_precedence() -> None:
    error = TelegramRateLimitedError("429", retry_after=42.0)
    decision = decide_retry(error, attempt=1, settings=settings, rng=lambda: 0.0)
    assert decision.retryable is True
    assert decision.delay_seconds == pytest.approx(42.0)
    assert decision.error_class == "rate_limited"


def test_unknown_exceptions_are_bounded_transient() -> None:
    decision = decide_retry(ConnectionResetError(), attempt=1, settings=settings)
    assert decision.retryable is True
    assert decision.error_class == "transport"
