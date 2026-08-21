"""Selection value objects.  Pure data - unit-testable without any I/O."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.domain.enums import AssetClass, InstrumentQualityStatus
from app.selection.enums import InstrumentEligibilityStatus, MarketSelectionState


@dataclass(frozen=True)
class ClassificationResult:
    asset_class: AssetClass
    source: str  # "metadata_category" | "symbol_rule" | "fallback"
    rule: str
    confidence: float
    classified_at: datetime


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class ResolvedThresholds:
    """Effective thresholds after asset-class defaults, symbol overrides and
    (for crypto thin-liquidity windows) LIMITED_SESSION tightening."""

    max_spread_bps: float
    min_book_depth_pusd: float
    min_volume_24h_pusd: float
    max_mark_index_mid_deviation_bps: float
    depth_window_bps: float
    require_volume: bool
    require_fresh_orderbook: bool
    allow_degraded_data: bool
    min_quality_score: int
    tightened_for_thin_liquidity: bool = False


@dataclass(frozen=True)
class MarketSnapshot:
    """Point-in-time public market data inputs.  Carries no direction info."""

    instrument_active: bool
    data_quality_status: InstrumentQualityStatus | None
    book_reliable: bool
    book_fresh: bool
    bid_depth_pusd: float | None
    ask_depth_pusd: float | None
    spread_bps: float | None
    bbo_present: bool
    mark_price: float | None
    index_price: float | None
    mid_price: float | None
    volume_24h_pusd: float | None
    volume_fresh: bool = False


@dataclass(frozen=True)
class QualityResult:
    score: int
    components: dict[str, float]
    reasons: list[Reason] = field(default_factory=list)


@dataclass(frozen=True)
class SessionVerdict:
    """Session gate result for one asset class (no strategy semantics)."""

    session_state: str
    analysis_allowed: bool
    blocking_status: InstrumentEligibilityStatus | None
    calendar_version: str | None
    limited_liquidity: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class SelectionInputs:
    """Everything the pure engine needs to evaluate one instrument."""

    instrument_pk: int
    instrument_id: int
    symbol: str
    instrument_enabled: bool
    instrument_status: str  # ACTIVE / DELISTED / ...
    classification: ClassificationResult
    session: SessionVerdict
    market: MarketSnapshot
    thresholds: ResolvedThresholds
    denylisted: bool
    allowlisted: bool
    configuration_version: str
    evaluated_at: datetime


@dataclass(frozen=True)
class SelectionDecisionData:
    """Immutable outcome of one selection evaluation (persisted verbatim)."""

    selection_id: uuid.UUID
    instrument_pk: int
    instrument_id: int
    symbol: str
    asset_class: AssetClass
    classification_source: str
    session_state: str
    calendar_version: str | None
    market_status: str
    data_quality_status: str
    selection_state: MarketSelectionState
    eligibility_status: InstrumentEligibilityStatus
    market_quality_score: int | None
    quality_components: dict[str, float]
    reasons: list[Reason]
    configuration_version: str
    evaluated_at: datetime
    expires_at: datetime | None = None
    previous_selection_id: uuid.UUID | None = None

    @property
    def eligible(self) -> bool:
        return self.eligibility_status is InstrumentEligibilityStatus.ELIGIBLE

    def reasons_json(self) -> list[dict[str, Any]]:
        return [reason.as_dict() for reason in self.reasons]
