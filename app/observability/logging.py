"""Structured JSON logging via structlog.

- ISO-8601 UTC timestamps
- correlation id support via contextvars
- secret redaction: values of sensitive keys are never emitted
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

SENSITIVE_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "authorization",
    "private_key",
    "dsn",
)

REDACTED = "[REDACTED]"


def _redact_value(key: str, value: Any) -> Any:
    key_lower = key.lower()
    if any(marker in key_lower for marker in SENSITIVE_KEY_MARKERS):
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    return value


def redact_processor(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor that redacts values of sensitive keys."""
    return {key: _redact_value(key, value) for key, value in event_dict.items()}


def configure_logging(level: str = "INFO") -> None:
    """Configure stdlib + structlog for JSON output on stdout."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_processor,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        # PrintLoggerFactory without a fixed stream + no caching resolves
        # sys.stdout at call time (robust under test capture and re-config).
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name, **initial_values)  # type: ignore[no-any-return]


def bind_correlation_id(correlation_id: str) -> None:
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)


def clear_correlation_id() -> None:
    structlog.contextvars.unbind_contextvars("correlation_id")
