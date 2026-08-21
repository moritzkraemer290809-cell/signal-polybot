"""JSON logging and secret redaction."""

from __future__ import annotations

import json

from app.observability.logging import configure_logging, get_logger, redact_processor


def test_log_output_is_json_with_level_and_timestamp(capsys) -> None:
    configure_logging("INFO")
    log = get_logger("test")
    log.info("hello_event", foo="bar")
    captured = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(captured)
    assert payload["event"] == "hello_event"
    assert payload["foo"] == "bar"
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_secrets_are_redacted(capsys) -> None:
    configure_logging("INFO")
    log = get_logger("test")
    log.info("connect", bot_token="123:abc", database_dsn="postgres://user:pw@host/db")
    captured = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(captured)
    assert payload["bot_token"] == "[REDACTED]"
    assert payload["database_dsn"] == "[REDACTED]"
    assert "123:abc" not in captured
    assert "pw@host" not in captured


def test_redaction_is_recursive() -> None:
    event = {"outer": {"api_key": "xyz", "safe": 1}, "event": "e"}
    result = redact_processor(None, "info", event)
    assert result["outer"]["api_key"] == "[REDACTED]"
    assert result["outer"]["safe"] == 1
