"""Configurable asset classification.

Rule order (first match wins):
1. metadata category via the configurable category map (confidence 0.95)
2. configurable symbol map fallback (confidence 0.6; EMPTY by default - no
   hard-wired symbol->class assumptions ship with the project)
3. UNKNOWN (confidence 0.0) - excluded from analysis by default

Every decision records source, rule, confidence and timestamp and is
persisted as classification history.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from app.config import MarketSelectionSettings
from app.domain.enums import AssetClass
from app.selection.models import ClassificationResult


class AssetClassifier:
    def __init__(
        self,
        settings: MarketSelectionSettings,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._category_map = {
            key.strip().lower(): value.upper()
            for key, value in settings.classification_category_map.items()
        }
        self._symbol_map = {
            key.strip().upper(): value.upper()
            for key, value in settings.classification_symbol_map.items()
        }
        self._now = now_fn

    def classify(self, symbol: str, category: str) -> ClassificationResult:
        from app.selection.models import ClassificationResult

        now = self._now()
        category_key = category.strip().lower()
        if category_key and category_key in self._category_map:
            mapped = self._category_map[category_key]
            if mapped in AssetClass.__members__:
                return ClassificationResult(
                    asset_class=AssetClass(mapped),
                    source="metadata_category",
                    rule=f"category:{category_key}->{mapped}",
                    confidence=0.95,
                    classified_at=now,
                )
        symbol_key = symbol.strip().upper()
        if symbol_key in self._symbol_map:
            mapped = self._symbol_map[symbol_key]
            if mapped in AssetClass.__members__:
                return ClassificationResult(
                    asset_class=AssetClass(mapped),
                    source="symbol_rule",
                    rule=f"symbol:{symbol_key}->{mapped}",
                    confidence=0.6,
                    classified_at=now,
                )
        return ClassificationResult(
            asset_class=AssetClass.UNKNOWN,
            source="fallback",
            rule="no_rule_matched",
            confidence=0.0,
            classified_at=now,
        )
