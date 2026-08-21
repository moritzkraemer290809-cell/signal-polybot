"""Crypto sessions: 24/7, thin-liquidity windows, invalid configuration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import CryptoSessionSettings
from app.sessions.models import CryptoSessionState
from app.sessions.session_manager import CryptoSessionManager


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def test_default_is_24_7() -> None:
    manager = CryptoSessionManager(CryptoSessionSettings(_env_file=None))
    snapshot = manager.evaluate(at(2026, 8, 22, 3, 0))  # Saturday night
    assert snapshot.state is CryptoSessionState.CRYPTO_24_7
    assert snapshot.analysis_allowed is True
    assert snapshot.limited is False


def test_thin_liquidity_window_limits_but_does_not_block() -> None:
    settings = CryptoSessionSettings(
        _env_file=None,
        thin_liquidity_windows_json=[{"days": [5, 6], "start": "22:00", "end": "23:59"}],
    )
    manager = CryptoSessionManager(settings)
    inside = manager.evaluate(at(2026, 8, 22, 22, 30))  # Saturday 22:30 UTC
    assert inside.state is CryptoSessionState.CRYPTO_THIN_LIQUIDITY
    assert inside.analysis_allowed is True  # stricter quality, not a block
    assert inside.limited is True
    outside = manager.evaluate(at(2026, 8, 22, 12, 0))
    assert outside.state is CryptoSessionState.CRYPTO_24_7


def test_thin_window_spanning_midnight() -> None:
    settings = CryptoSessionSettings(
        _env_file=None,
        thin_liquidity_windows_json=[{"days": [4], "start": "22:00", "end": "04:00"}],
    )
    manager = CryptoSessionManager(settings)
    # Friday (weekday 4) 23:00 -> inside
    assert manager.evaluate(at(2026, 8, 21, 23, 0)).limited is True
    # Saturday 03:00 belongs to Friday's window
    assert manager.evaluate(at(2026, 8, 22, 3, 0)).limited is True
    # Saturday 05:00 -> outside
    assert manager.evaluate(at(2026, 8, 22, 5, 0)).limited is False


def test_ignore_policy_disables_limitation() -> None:
    settings = CryptoSessionSettings(
        _env_file=None,
        thin_liquidity_windows_json=[{"days": [5], "start": "00:00", "end": "23:59"}],
        thin_liquidity_policy="IGNORE",
    )
    snapshot = CryptoSessionManager(settings).evaluate(at(2026, 8, 22, 12, 0))
    assert snapshot.state is CryptoSessionState.CRYPTO_24_7


def test_disabled_crypto_session() -> None:
    manager = CryptoSessionManager(CryptoSessionSettings(_env_file=None, session_enabled=False))
    snapshot = manager.evaluate(at(2026, 8, 21, 12, 0))
    assert snapshot.state is CryptoSessionState.CRYPTO_DISABLED
    assert snapshot.analysis_allowed is False


def test_invalid_window_configuration_fails_loudly() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CryptoSessionSettings(
            _env_file=None,
            thin_liquidity_windows_json=[{"days": [9], "start": "22:00", "end": "23:00"}],
        )
    with pytest.raises(ValidationError):
        CryptoSessionSettings(
            _env_file=None,
            thin_liquidity_windows_json=[{"days": [1], "start": "bad", "end": "23:00"}],
        )
