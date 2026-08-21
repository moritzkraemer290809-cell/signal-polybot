"""Eligibility gates, policies and the pure selection engine."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.test_market_quality import snapshot, thresholds

from app.config import MarketSelectionSettings, SelectionThresholdSettings
from app.domain.enums import AssetClass, InstrumentQualityStatus
from app.selection.enums import InstrumentEligibilityStatus as ES
from app.selection.enums import MarketSelectionState
from app.selection.liquidity_filters import resolve_thresholds
from app.selection.market_selection_engine import (
    evaluate_instrument,
    session_verdict_for_asset_class,
)
from app.selection.models import (
    ClassificationResult,
    SelectionInputs,
    SessionVerdict,
)

NOW = datetime(2026, 8, 21, 15, 0, tzinfo=UTC)


def classification(asset_class: AssetClass = AssetClass.CRYPTO) -> ClassificationResult:
    return ClassificationResult(
        asset_class=asset_class,
        source="metadata_category",
        rule="test",
        confidence=0.95,
        classified_at=NOW,
    )


def open_session(**overrides) -> SessionVerdict:
    defaults = dict(
        session_state="CRYPTO_24_7",
        analysis_allowed=True,
        blocking_status=None,
        calendar_version=None,
    )
    defaults.update(overrides)
    return SessionVerdict(**defaults)


def inputs(**overrides) -> SelectionInputs:
    defaults = dict(
        instrument_pk=10,
        instrument_id=1,
        symbol="BTC-PERP",
        instrument_enabled=True,
        instrument_status="ACTIVE",
        classification=classification(),
        session=open_session(),
        market=snapshot(),
        thresholds=thresholds(),
        denylisted=False,
        allowlisted=True,
        configuration_version="v1",
        evaluated_at=NOW,
    )
    defaults.update(overrides)
    return SelectionInputs(**defaults)


def evaluate(**overrides):
    return evaluate_instrument(inputs(**overrides))


def test_fully_healthy_instrument_is_eligible() -> None:
    decision = evaluate()
    assert decision.eligibility_status is ES.ELIGIBLE
    assert decision.selection_state is MarketSelectionState.WATCHLIST_ELIGIBLE
    assert decision.market_quality_score == 100
    assert decision.reasons == []


def test_denylist_always_wins() -> None:
    decision = evaluate(denylisted=True, allowlisted=True)
    assert decision.eligibility_status is ES.DISABLED_BY_CONFIG


def test_allowlist_cannot_bypass_session_or_data_blocks() -> None:
    session_blocked = evaluate(
        allowlisted=True,
        session=open_session(
            session_state="EQUITY_WEEKEND",
            analysis_allowed=False,
            blocking_status=ES.WEEKEND_CLOSED,
        ),
    )
    assert session_blocked.eligibility_status is ES.WEEKEND_CLOSED
    data_blocked = evaluate(
        allowlisted=True,
        market=snapshot(data_quality_status=InstrumentQualityStatus.DATA_STALE),
    )
    assert data_blocked.eligibility_status is ES.DATA_STALE


def test_unknown_asset_class_is_unsupported() -> None:
    decision = evaluate(classification=classification(AssetClass.UNKNOWN))
    assert decision.eligibility_status is ES.ASSET_CLASS_UNSUPPORTED


def test_session_blocks_map_through() -> None:
    for status in (
        ES.SESSION_CLOSED,
        ES.HOLIDAY_CLOSED,
        ES.WEEKEND_CLOSED,
        ES.EARLY_CLOSE_ENDED,
        ES.CALENDAR_UNAVAILABLE,
        ES.DISABLED_BY_POLICY,
    ):
        decision = evaluate(session=open_session(analysis_allowed=False, blocking_status=status))
        assert decision.eligibility_status is status


def test_delisted_and_unconfigured_instruments() -> None:
    assert evaluate(instrument_status="DELISTED").eligibility_status is ES.INSTRUMENT_DELISTED
    assert evaluate(instrument_enabled=False).eligibility_status is ES.INSTRUMENT_NOT_CONFIGURED


def test_data_quality_blocks() -> None:
    cases = {
        InstrumentQualityStatus.DATA_STALE: ES.DATA_STALE,
        InstrumentQualityStatus.DATA_INVALID: ES.DATA_INVALID,
        InstrumentQualityStatus.ORDERBOOK_RESYNCING: ES.ORDERBOOK_RESYNCING,
        InstrumentQualityStatus.UNAVAILABLE: ES.DATA_UNAVAILABLE,
        None: ES.DATA_UNAVAILABLE,
    }
    for status, expected in cases.items():
        assert evaluate(market=snapshot(data_quality_status=status)).eligibility_status is expected


def test_degraded_data_policy() -> None:
    degraded = snapshot(data_quality_status=InstrumentQualityStatus.DEGRADED)
    blocked = evaluate(market=degraded)
    assert blocked.eligibility_status is ES.INELIGIBLE
    allowed = evaluate(
        market=degraded,
        thresholds=thresholds(allow_degraded_data=True, min_quality_score=60),
    )
    assert allowed.eligibility_status is ES.ELIGIBLE


def test_stale_orderbook_blocks_when_required() -> None:
    result = evaluate(market=snapshot(book_fresh=False))
    assert result.eligibility_status is ES.DATA_STALE
    relaxed = evaluate(
        market=snapshot(book_fresh=False),
        thresholds=thresholds(require_fresh_orderbook=False, min_quality_score=50),
    )
    assert relaxed.eligibility_status is ES.ELIGIBLE


def test_price_gates() -> None:
    assert evaluate(market=snapshot(bbo_present=False)).eligibility_status is ES.PRICE_INVALID
    assert evaluate(market=snapshot(mark_price=None)).eligibility_status is ES.PRICE_INVALID
    diverged = evaluate(market=snapshot(index_price=105.0))
    assert diverged.eligibility_status is ES.PRICE_INVALID


def test_volume_gates() -> None:
    missing = evaluate(market=snapshot(volume_24h_pusd=None, volume_fresh=False))
    assert missing.eligibility_status is ES.VOLUME_UNAVAILABLE
    low = evaluate(market=snapshot(volume_24h_pusd=50_000.0))
    assert low.eligibility_status is ES.LOW_VOLUME
    optional = evaluate(
        market=snapshot(volume_24h_pusd=None, volume_fresh=False),
        thresholds=thresholds(require_volume=False, min_quality_score=60),
    )
    assert optional.eligibility_status is ES.ELIGIBLE


def test_spread_and_depth_gates() -> None:
    wide = evaluate(market=snapshot(spread_bps=20.0))  # on-limit blocks
    assert wide.eligibility_status is ES.SPREAD_TOO_WIDE
    thin = evaluate(market=snapshot(bid_depth_pusd=1_000.0))
    assert thin.eligibility_status is ES.INSUFFICIENT_DEPTH


def test_minimum_score_gate() -> None:
    # healthy but degraded-scoring market below min score
    decision = evaluate(
        market=snapshot(spread_bps=19.0, volume_24h_pusd=100_000.0, index_price=100.6),
        thresholds=thresholds(min_quality_score=95),
    )
    assert decision.eligibility_status is ES.INELIGIBLE
    assert any(r.code == "QUALITY_SCORE_BELOW_MINIMUM" for r in decision.reasons)


def test_multiple_reasons_are_collected() -> None:
    decision = evaluate(
        market=snapshot(
            data_quality_status=InstrumentQualityStatus.DATA_STALE,
            spread_bps=50.0,
            volume_24h_pusd=1_000.0,
        )
    )
    codes = {reason.code for reason in decision.reasons}
    assert "DATA_STALE" in codes
    assert "SPREAD_TOO_WIDE" in codes
    assert "LOW_VOLUME" in codes
    assert decision.eligibility_status is ES.DATA_STALE  # first blocking gate wins


def test_symbol_overrides_take_precedence() -> None:
    selection = MarketSelectionSettings(
        _env_file=None,
        symbol_overrides_json={"BTC-PERP": {"max_spread_bps": 50.0, "require_volume": False}},
    )
    resolved = resolve_thresholds(
        AssetClass.CRYPTO, "BTC-PERP", selection, SelectionThresholdSettings(_env_file=None)
    )
    assert resolved.max_spread_bps == 50.0
    assert resolved.require_volume is False
    default = resolve_thresholds(
        AssetClass.CRYPTO, "ETH-PERP", selection, SelectionThresholdSettings(_env_file=None)
    )
    assert default.max_spread_bps == 10.0


def test_thin_liquidity_tightens_thresholds() -> None:
    selection = MarketSelectionSettings(_env_file=None)
    base = resolve_thresholds(
        AssetClass.CRYPTO, "BTC-PERP", selection, SelectionThresholdSettings(_env_file=None)
    )
    tightened = resolve_thresholds(
        AssetClass.CRYPTO,
        "BTC-PERP",
        selection,
        SelectionThresholdSettings(_env_file=None),
        thin_liquidity=True,
    )
    assert tightened.max_spread_bps < base.max_spread_bps
    assert tightened.min_book_depth_pusd > base.min_book_depth_pusd
    assert tightened.min_volume_24h_pusd > base.min_volume_24h_pusd
    assert tightened.tightened_for_thin_liquidity is True


def test_session_verdict_mapping() -> None:
    from app.sessions.models import (
        CryptoSessionSnapshot,
        CryptoSessionState,
        EquitySessionSnapshot,
        EquitySessionState,
    )

    equity_regular = EquitySessionSnapshot(
        state=EquitySessionState.EQUITY_REGULAR,
        evaluated_at=NOW,
        calendar_available=True,
        calendar_version="us-equity-2026.1",
        analysis_allowed=True,
    )
    crypto_open = CryptoSessionSnapshot(
        state=CryptoSessionState.CRYPTO_24_7, evaluated_at=NOW, analysis_allowed=True
    )
    verdict = session_verdict_for_asset_class(AssetClass.EQUITY, equity_regular, crypto_open)
    assert verdict.blocking_status is None

    unavailable = EquitySessionSnapshot(
        state=EquitySessionState.EQUITY_CLOSED,
        evaluated_at=NOW,
        calendar_available=False,
        calendar_version=None,
        analysis_allowed=False,
        detail="calendar disabled",
    )
    verdict = session_verdict_for_asset_class(AssetClass.EQUITY, unavailable, crypto_open)
    assert verdict.blocking_status is ES.CALENDAR_UNAVAILABLE

    early_ended = EquitySessionSnapshot(
        state=EquitySessionState.EQUITY_AFTER_HOURS,
        evaluated_at=NOW,
        calendar_available=True,
        calendar_version="us-equity-2026.1",
        analysis_allowed=False,
        early_close_reason="Day after Thanksgiving",
    )
    verdict = session_verdict_for_asset_class(AssetClass.EQUITY, early_ended, crypto_open)
    assert verdict.blocking_status is ES.EARLY_CLOSE_ENDED
    # crypto is unaffected by the equity calendar
    verdict = session_verdict_for_asset_class(AssetClass.CRYPTO, unavailable, crypto_open)
    assert verdict.blocking_status is None
