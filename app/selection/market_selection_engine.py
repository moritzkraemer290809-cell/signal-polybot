"""Pure market selection evaluation.

``evaluate_instrument`` is a pure function over :class:`SelectionInputs` -
unit-testable without database, Redis, Telegram or network.  The surrounding
``MarketSelectionService`` (bootstrap-wired) gathers live inputs from the
data layer and session managers.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.config import MarketSelectionSettings, SelectionThresholdSettings
from app.data.data_quality import DataQualityService
from app.data.market_data_service import MarketDataService
from app.data.orderbook_service import OrderbookManager
from app.domain.enums import AssetClass, Channel, FreshnessStatus
from app.selection.classification import AssetClassifier
from app.selection.eligibility import evaluate_eligibility
from app.selection.enums import InstrumentEligibilityStatus, MarketSelectionState
from app.selection.liquidity_filters import resolve_thresholds
from app.selection.market_quality import evaluate_market_quality
from app.selection.models import (
    MarketSnapshot,
    SelectionDecisionData,
    SelectionInputs,
    SessionVerdict,
)
from app.sessions.models import (
    CryptoSessionSnapshot,
    CryptoSessionState,
    EquitySessionSnapshot,
    EquitySessionState,
)


def evaluate_instrument(inputs: SelectionInputs) -> SelectionDecisionData:
    """Evaluate one instrument: quality score + hard gates -> decision."""
    quality = evaluate_market_quality(inputs.market, inputs.thresholds)
    eligibility, reasons = evaluate_eligibility(inputs, quality)

    if eligibility is InstrumentEligibilityStatus.ELIGIBLE:
        state = MarketSelectionState.WATCHLIST_ELIGIBLE
    elif inputs.market.data_quality_status is None:
        state = MarketSelectionState.PENDING_QUALITY
    else:
        state = MarketSelectionState.REJECTED

    return SelectionDecisionData(
        selection_id=uuid.uuid4(),
        instrument_pk=inputs.instrument_pk,
        instrument_id=inputs.instrument_id,
        symbol=inputs.symbol,
        asset_class=inputs.classification.asset_class,
        classification_source=inputs.classification.source,
        session_state=inputs.session.session_state,
        calendar_version=inputs.session.calendar_version,
        market_status=inputs.instrument_status,
        data_quality_status=(
            inputs.market.data_quality_status.value
            if inputs.market.data_quality_status
            else "UNKNOWN"
        ),
        selection_state=state,
        eligibility_status=eligibility,
        market_quality_score=quality.score,
        quality_components=quality.components,
        reasons=reasons,
        configuration_version=inputs.configuration_version,
        evaluated_at=inputs.evaluated_at,
    )


def session_verdict_for_asset_class(
    asset_class: AssetClass,
    equity: EquitySessionSnapshot,
    crypto: CryptoSessionSnapshot,
) -> SessionVerdict:
    """Map session snapshots to the per-asset-class gate result."""
    if asset_class is AssetClass.EQUITY:
        if not equity.calendar_available:
            return SessionVerdict(
                session_state=equity.state.value,
                analysis_allowed=False,
                blocking_status=InstrumentEligibilityStatus.CALENDAR_UNAVAILABLE,
                calendar_version=equity.calendar_version,
                detail=equity.detail or "equity calendar unavailable",
            )
        blocking: InstrumentEligibilityStatus | None = None
        detail: str | None = None
        if equity.state is EquitySessionState.EQUITY_WEEKEND:
            blocking = InstrumentEligibilityStatus.WEEKEND_CLOSED
            detail = "weekend"
        elif equity.state is EquitySessionState.EQUITY_HOLIDAY:
            blocking = InstrumentEligibilityStatus.HOLIDAY_CLOSED
            detail = equity.holiday_name or "market holiday"
        elif not equity.analysis_allowed:
            if equity.early_close_reason is not None and equity.state in (
                EquitySessionState.EQUITY_AFTER_HOURS,
                EquitySessionState.EQUITY_CLOSED,
            ):
                blocking = InstrumentEligibilityStatus.EARLY_CLOSE_ENDED
                detail = f"early close ended ({equity.early_close_reason})"
            else:
                blocking = InstrumentEligibilityStatus.SESSION_CLOSED
                detail = f"session {equity.state.value}"
        return SessionVerdict(
            session_state=equity.state.value,
            analysis_allowed=blocking is None,
            blocking_status=blocking,
            calendar_version=equity.calendar_version,
            detail=detail,
        )

    # CRYPTO and any other enabled 24/7 class
    if crypto.state is CryptoSessionState.CRYPTO_DISABLED or not crypto.analysis_allowed:
        return SessionVerdict(
            session_state=crypto.state.value,
            analysis_allowed=False,
            blocking_status=InstrumentEligibilityStatus.SESSION_CLOSED,
            calendar_version=None,
            detail="crypto session disabled",
        )
    return SessionVerdict(
        session_state=crypto.state.value,
        analysis_allowed=True,
        blocking_status=None,
        calendar_version=None,
        limited_liquidity=crypto.limited,
        detail=crypto.window_detail,
    )


class _InstrumentRow(Protocol):
    id: int
    instrument_id: int
    symbol: str
    category: str
    status: str
    enabled: bool


class MarketSelectionService:
    """Gathers live inputs and runs the pure engine per instrument."""

    def __init__(
        self,
        selection_settings: MarketSelectionSettings,
        threshold_settings: SelectionThresholdSettings,
        classifier: AssetClassifier,
        market_data: MarketDataService | None,
        data_quality: DataQualityService | None,
        books: OrderbookManager | None,
        configuration_version: str,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._settings = selection_settings
        self._thresholds = threshold_settings
        self._classifier = classifier
        self._market_data = market_data
        self._data_quality = data_quality
        self._books = books
        self._configuration_version = configuration_version
        self._now = now_fn

    def build_inputs(
        self,
        row: _InstrumentRow,
        equity: EquitySessionSnapshot,
        crypto: CryptoSessionSnapshot,
    ) -> SelectionInputs:
        classification = self._classifier.classify(row.symbol, row.category)
        asset_class = classification.asset_class

        session = session_verdict_for_asset_class(asset_class, equity, crypto)
        policy_blocked = self._policy_block(asset_class)
        if policy_blocked is not None and session.blocking_status is None:
            session = SessionVerdict(
                session_state=session.session_state,
                analysis_allowed=False,
                blocking_status=policy_blocked,
                calendar_version=session.calendar_version,
                limited_liquidity=session.limited_liquidity,
                detail=f"asset class {asset_class.value} disabled by policy",
            )

        thresholds = resolve_thresholds(
            asset_class,
            row.symbol,
            self._settings,
            self._thresholds,
            thin_liquidity=session.limited_liquidity,
        )
        market = self._market_snapshot(row.instrument_id, thresholds)
        symbol_upper = row.symbol.strip().upper()
        allowlist = [entry.strip().upper() for entry in self._settings.default_allowlist]
        denylist = [entry.strip().upper() for entry in self._settings.denylist]
        return SelectionInputs(
            instrument_pk=row.id,
            instrument_id=row.instrument_id,
            symbol=row.symbol,
            instrument_enabled=row.enabled,
            instrument_status=row.status,
            classification=classification,
            session=session,
            market=market,
            thresholds=thresholds,
            denylisted=symbol_upper in denylist,
            allowlisted=not allowlist or symbol_upper in allowlist,
            configuration_version=self._configuration_version,
            evaluated_at=self._now(),
        )

    def _policy_block(self, asset_class: AssetClass) -> InstrumentEligibilityStatus | None:
        """Asset-class policy: allowed list + explicit JSON policy overrides."""
        policy = self._settings.asset_class_policies_json.get(asset_class.value, {})
        if "enabled" in policy:
            return None if policy["enabled"] else InstrumentEligibilityStatus.DISABLED_BY_POLICY
        if asset_class is AssetClass.UNKNOWN:
            return InstrumentEligibilityStatus.ASSET_CLASS_UNSUPPORTED
        if asset_class.value not in self._settings.allowed_asset_classes:
            return InstrumentEligibilityStatus.DISABLED_BY_POLICY
        return None

    def _market_snapshot(self, instrument_id: int, thresholds: Any) -> MarketSnapshot:
        """Build the data snapshot from cached live data only - no REST calls."""
        if self._market_data is None or self._data_quality is None or self._books is None:
            return MarketSnapshot(
                instrument_active=False,
                data_quality_status=None,
                book_reliable=False,
                book_fresh=False,
                bid_depth_pusd=None,
                ask_depth_pusd=None,
                spread_bps=None,
                bbo_present=False,
                mark_price=None,
                index_price=None,
                mid_price=None,
                volume_24h_pusd=None,
            )
        tracker = self._market_data.tracker_for(instrument_id)
        report = self._data_quality.evaluate(instrument_id)
        book = self._books.book(instrument_id)
        book_freshness = self._data_quality.channel_freshness(instrument_id, Channel.ORDERBOOK)
        book_fresh = book_freshness in (FreshnessStatus.FRESH, FreshnessStatus.AGING)

        mid = book.mid_price if book.reliable else None
        bid_depth = ask_depth = None
        if book.reliable and mid is not None:
            window = Decimal(str(thresholds.depth_window_bps))
            bid_depth = float(book.depth_within_bps("bid", window) * mid)
            ask_depth = float(book.depth_within_bps("ask", window) * mid)

        bbo = tracker.last_bbo if tracker is not None else None
        bbo_fresh = self._data_quality.channel_freshness(instrument_id, Channel.BBO) in (
            FreshnessStatus.FRESH,
            FreshnessStatus.AGING,
        )
        spread = None
        if bbo is not None and bbo_fresh and bbo.spread_bps is not None:
            spread = float(bbo.spread_bps)
        elif book.reliable and book_fresh and book.spread_bps is not None:
            spread = float(book.spread_bps)

        stats_fresh = self._data_quality.channel_freshness(instrument_id, Channel.STATISTICS) in (
            FreshnessStatus.FRESH,
            FreshnessStatus.AGING,
        )
        return MarketSnapshot(
            instrument_active=True,
            data_quality_status=report.status,
            book_reliable=book.reliable,
            book_fresh=book_fresh,
            bid_depth_pusd=bid_depth,
            ask_depth_pusd=ask_depth,
            spread_bps=spread,
            bbo_present=bbo is not None and bbo_fresh,
            mark_price=(
                float(tracker.last_mark_price)
                if tracker is not None and tracker.last_mark_price is not None
                else None
            ),
            index_price=(
                float(tracker.last_index_price)
                if tracker is not None and tracker.last_index_price is not None
                else None
            ),
            mid_price=float(mid) if mid is not None else None,
            volume_24h_pusd=(
                float(tracker.last_volume_24h)
                if tracker is not None and tracker.last_volume_24h is not None
                else None
            ),
            volume_fresh=stats_fresh,
        )
