"""Domain value objects for public Polymarket Perps market data.

All models are immutable pydantic models with ``Decimal`` prices and
timezone-aware UTC timestamps.  They mirror the public REST/WebSocket payloads
of the Polymarket Perps API without any account or order data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import AssetClass, InstrumentStatus, Timeframe

# Metadata-category hint mapping.  The authoritative, configurable
# classification lives in app/selection/classification.py; this property is
# only a coarse hint derived from public instrument metadata.
_CATEGORY_HINTS: dict[str, AssetClass] = {}


def ms_to_utc(timestamp_ms: int) -> datetime:
    """Convert unix milliseconds to a timezone-aware UTC datetime."""
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=UTC)


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class RiskTier(_Frozen):
    lower_bound: Decimal
    max_leverage: int


class InstrumentMeta(_Frozen):
    """Instrument metadata as discovered from ``/v1/info/instruments``."""

    instrument_id: int
    symbol: str
    instrument_type: str = "perpetual"
    category: str = ""
    base_asset: str = ""
    quote_asset: str = ""
    price_decimals: int = 2
    quantity_decimals: int = 4
    min_notional: Decimal = Decimal("0")
    max_leverage: int = 1
    funding_interval: str | None = None
    liquidation_fee: Decimal | None = None
    max_order_count: int | None = None
    max_market_notional: Decimal | None = None
    max_limit_notional: Decimal | None = None
    isolated_only: bool = False
    risk_tiers: tuple[RiskTier, ...] = ()
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("liquidation_fee", "max_market_notional", "max_limit_notional", mode="before")
    @classmethod
    def _empty_as_none(cls, value: object) -> object:
        if value in ("", None):
            return None
        return value

    @property
    def asset_class(self) -> AssetClass:
        if not _CATEGORY_HINTS:
            _CATEGORY_HINTS.update(
                {
                    "crypto": AssetClass.CRYPTO,
                    "cryptocurrency": AssetClass.CRYPTO,
                    "equity": AssetClass.EQUITY,
                    "equities": AssetClass.EQUITY,
                    "stock": AssetClass.EQUITY,
                    "stocks": AssetClass.EQUITY,
                    "index": AssetClass.INDEX,
                    "indices": AssetClass.INDEX,
                    "commodity": AssetClass.COMMODITY,
                    "commodities": AssetClass.COMMODITY,
                    "fx": AssetClass.FX,
                    "forex": AssetClass.FX,
                    "currency": AssetClass.FX,
                }
            )
        return _CATEGORY_HINTS.get(self.category.strip().lower(), AssetClass.UNKNOWN)

    @property
    def status(self) -> InstrumentStatus:
        # The public payload has no explicit status flag; ``ui_live_time`` in
        # the future (or unset symbol) would indicate a not-yet-live market.
        return InstrumentStatus.ACTIVE

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> InstrumentMeta:
        tiers = tuple(
            RiskTier(
                lower_bound=Decimal(str(tier.get("lower_bound", "0"))),
                max_leverage=int(tier.get("max_leverage", 1)),
            )
            for tier in payload.get("risk_tiers") or []
        )
        return cls(
            instrument_id=int(payload["instrument_id"]),
            symbol=str(payload["symbol"]),
            instrument_type=str(payload.get("instrument_type", "perpetual")),
            category=str(payload.get("category", "")),
            base_asset=str(payload.get("base_asset", "")),
            quote_asset=str(payload.get("quote_asset", "")),
            price_decimals=int(payload.get("price_decimals", 2)),
            quantity_decimals=int(payload.get("quantity_decimals", 4)),
            min_notional=Decimal(str(payload.get("min_notional", "0"))),
            max_leverage=int(payload.get("max_leverage", 1)),
            funding_interval=payload.get("funding_interval"),
            liquidation_fee=payload.get("liquidation_fee"),
            max_order_count=payload.get("max_order_count"),
            max_market_notional=payload.get("max_market_notional"),
            max_limit_notional=payload.get("max_limit_notional"),
            isolated_only=bool(payload.get("isolated_only", False)),
            risk_tiers=tiers,
            raw=payload,
        )


class TickerData(_Frozen):
    """Ticker snapshot from ``/v1/info/tickers`` or the ``tickers`` channel."""

    instrument_id: int
    symbol: str
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    last_price: Decimal | None = None
    mid_price: Decimal | None = None
    open_interest: Decimal | None = None
    funding_rate: Decimal | None = None
    next_funding_at: datetime | None = None
    ts: datetime

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> TickerData:
        def dec(key: str) -> Decimal | None:
            value = payload.get(key)
            return None if value in (None, "") else Decimal(str(value))

        next_funding = payload.get("next_funding")
        return cls(
            instrument_id=int(payload["instrument_id"]),
            symbol=str(payload.get("symbol", "")),
            mark_price=dec("mark_price"),
            index_price=dec("index_price"),
            last_price=dec("last_price"),
            mid_price=dec("mid_price"),
            open_interest=dec("open_interest"),
            funding_rate=dec("funding_rate"),
            next_funding_at=ms_to_utc(int(next_funding)) if next_funding else None,
            ts=ms_to_utc(int(payload["timestamp"])),
        )


class CandleData(_Frozen):
    """A single OHLCV bucket from ``/v1/info/klines``."""

    timeframe: Timeframe
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int = 0

    @classmethod
    def from_api_row(cls, row: list[Any], timeframe: Timeframe) -> CandleData:
        return cls(
            timeframe=timeframe,
            open_time=ms_to_utc(int(row[0])),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
            trade_count=int(row[6]) if len(row) > 6 else 0,
        )


class BookLevel(_Frozen):
    price: Decimal
    quantity: Decimal


class OrderbookData(_Frozen):
    """Order book snapshot from ``/v1/info/book`` or the ``book`` channel."""

    instrument_id: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    ts: datetime
    sequence: int | None = None

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid_price(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid.price + self.best_ask.price) / 2

    @property
    def spread_bps(self) -> Decimal | None:
        mid = self.mid_price
        if mid is None or mid == 0:
            return None
        assert self.best_bid is not None and self.best_ask is not None
        return (self.best_ask.price - self.best_bid.price) / mid * Decimal(10_000)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> OrderbookData:
        def levels(side: str) -> tuple[BookLevel, ...]:
            return tuple(
                BookLevel(price=Decimal(str(price)), quantity=Decimal(str(quantity)))
                for price, quantity in payload.get(side) or []
            )

        return cls(
            instrument_id=int(payload["instrument_id"]),
            bids=levels("bids"),
            asks=levels("asks"),
            ts=ms_to_utc(int(payload["timestamp"])),
            sequence=payload.get("sequence"),
        )


class TradeData(_Frozen):
    """Public trade from ``/v1/info/trades`` or the ``trades`` channel."""

    trade_id: int
    instrument_id: int
    side: str
    price: Decimal
    quantity: Decimal
    ts: datetime

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> TradeData:
        return cls(
            trade_id=int(payload["trade_id"]),
            instrument_id=int(payload["instrument_id"]),
            side=str(payload.get("side", "")),
            price=Decimal(str(payload["price"])),
            quantity=Decimal(str(payload["quantity"])),
            ts=ms_to_utc(int(payload["timestamp"])),
        )


class FundingRateData(_Frozen):
    """Historical funding rate entry from ``/v1/info/funding``."""

    funding_rate: Decimal
    ts: datetime

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> FundingRateData:
        return cls(
            funding_rate=Decimal(str(payload["funding_rate"])),
            ts=ms_to_utc(int(payload["timestamp"])),
        )
