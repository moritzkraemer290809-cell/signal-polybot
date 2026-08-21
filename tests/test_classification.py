"""Configurable asset classification with history semantics."""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import MarketSelectionSettings
from app.domain.enums import AssetClass
from app.selection.classification import AssetClassifier

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def make_classifier(**overrides) -> AssetClassifier:
    return AssetClassifier(MarketSelectionSettings(_env_file=None, **overrides), now_fn=lambda: NOW)


def test_metadata_category_classification() -> None:
    classifier = make_classifier()
    result = classifier.classify("BTC-PERP", "crypto")
    assert result.asset_class is AssetClass.CRYPTO
    assert result.source == "metadata_category"
    assert result.confidence == 0.95
    assert result.classified_at == NOW
    equity = classifier.classify("AAPL-PERP", "equity")
    assert equity.asset_class is AssetClass.EQUITY
    assert classifier.classify("SPX-PERP", "index").asset_class is AssetClass.INDEX
    assert classifier.classify("GOLD-PERP", "commodity").asset_class is AssetClass.COMMODITY
    assert classifier.classify("EURUSD-PERP", "fx").asset_class is AssetClass.FX


def test_no_hardcoded_symbol_assumptions_by_default() -> None:
    """Without metadata category, even well-known symbols are UNKNOWN."""
    classifier = make_classifier()
    for symbol in ("BTC-PERP", "AAPL-PERP", "SPY-PERP"):
        result = classifier.classify(symbol, "")
        assert result.asset_class is AssetClass.UNKNOWN
        assert result.source == "fallback"
        assert result.confidence == 0.0


def test_configurable_symbol_fallback() -> None:
    classifier = make_classifier(classification_symbol_map={"xyz-perp": "COMMODITY"})
    result = classifier.classify("XYZ-PERP", "")
    assert result.asset_class is AssetClass.COMMODITY
    assert result.source == "symbol_rule"
    assert result.confidence == 0.6
    # metadata still wins over symbol rule
    assert classifier.classify("XYZ-PERP", "crypto").asset_class is AssetClass.CRYPTO


def test_unknown_category_and_invalid_mapping_fall_through() -> None:
    classifier = make_classifier(classification_category_map={"weird": "NOT_A_CLASS"})
    assert classifier.classify("A-PERP", "weird").asset_class is AssetClass.UNKNOWN
    assert classifier.classify("A-PERP", "unmapped").asset_class is AssetClass.UNKNOWN


async def test_classification_history_persisted_only_on_change(session_factory) -> None:
    from app.domain.models import InstrumentMeta
    from app.repositories.instrument_repository import InstrumentRepository
    from app.repositories.market_selection_repository import MarketSelectionRepository

    instrument_repo = InstrumentRepository(session_factory)
    await instrument_repo.upsert_discovered(
        [InstrumentMeta.from_api({"instrument_id": 1, "symbol": "BTC-PERP", "category": "crypto"})],
        {"BTC-PERP"},
    )
    row = await instrument_repo.get_by_symbol("BTC-PERP")
    repo = MarketSelectionRepository(session_factory)
    classifier = make_classifier()
    result = classifier.classify("BTC-PERP", "crypto")
    await repo.add_classification(row.id, "BTC-PERP", result)
    stored = await repo.latest_classification(row.id)
    assert stored is not None
    assert stored.asset_class == "CRYPTO"
    assert stored.source == "metadata_category"
    assert stored.rule == "category:crypto->CRYPTO"
    assert float(stored.confidence) == 0.95
