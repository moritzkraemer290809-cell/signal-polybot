"""Market quality score: components, conservative handling of missing data."""

from __future__ import annotations

from app.domain.enums import InstrumentQualityStatus
from app.selection.market_quality import evaluate_market_quality
from app.selection.models import MarketSnapshot, ResolvedThresholds


def thresholds(**overrides) -> ResolvedThresholds:
    defaults = dict(
        max_spread_bps=20.0,
        min_book_depth_pusd=5_000.0,
        min_volume_24h_pusd=100_000.0,
        max_mark_index_mid_deviation_bps=75.0,
        depth_window_bps=25.0,
        require_volume=True,
        require_fresh_orderbook=True,
        allow_degraded_data=False,
        min_quality_score=70,
    )
    defaults.update(overrides)
    return ResolvedThresholds(**defaults)


def snapshot(**overrides) -> MarketSnapshot:
    defaults = dict(
        instrument_active=True,
        data_quality_status=InstrumentQualityStatus.HEALTHY,
        book_reliable=True,
        book_fresh=True,
        bid_depth_pusd=20_000.0,
        ask_depth_pusd=20_000.0,
        spread_bps=5.0,
        bbo_present=True,
        mark_price=100.0,
        index_price=100.02,
        mid_price=100.01,
        volume_24h_pusd=500_000.0,
        volume_fresh=True,
    )
    defaults.update(overrides)
    return MarketSnapshot(**defaults)


def test_healthy_market_scores_full_points() -> None:
    result = evaluate_market_quality(snapshot(), thresholds())
    assert result.score == 100
    assert result.components == {
        "data_quality": 30.0,
        "spread": 20.0,
        "depth": 20.0,
        "volume": 15.0,
        "market_status": 10.0,
        "price_consistency": 5.0,
    }
    assert result.reasons == []


def test_degraded_data_scores_half_data_points() -> None:
    result = evaluate_market_quality(
        snapshot(data_quality_status=InstrumentQualityStatus.DEGRADED), thresholds()
    )
    assert result.components["data_quality"] == 15.0
    assert any(reason.code == "DATA_DEGRADED" for reason in result.reasons)


def test_stale_invalid_unavailable_score_zero_data_points() -> None:
    for status, code in (
        (InstrumentQualityStatus.DATA_STALE, "DATA_STALE"),
        (InstrumentQualityStatus.DATA_INVALID, "DATA_INVALID"),
        (InstrumentQualityStatus.ORDERBOOK_RESYNCING, "ORDERBOOK_RESYNCING"),
        (InstrumentQualityStatus.UNAVAILABLE, "DATA_UNAVAILABLE"),
        (None, "DATA_UNAVAILABLE"),
    ):
        result = evaluate_market_quality(snapshot(data_quality_status=status), thresholds())
        assert result.components["data_quality"] == 0.0
        assert any(reason.code == code for reason in result.reasons)


def test_missing_bbo_scores_no_spread_points() -> None:
    result = evaluate_market_quality(snapshot(bbo_present=False, spread_bps=None), thresholds())
    assert result.components["spread"] == 0.0
    assert any(reason.code == "SPREAD_UNAVAILABLE" for reason in result.reasons)


def test_spread_scoring_around_threshold() -> None:
    # limit 20: <=10 full, on-limit and above zero, in-between scaled
    assert (
        evaluate_market_quality(snapshot(spread_bps=10.0), thresholds()).components["spread"]
        == 20.0
    )
    just_under = evaluate_market_quality(snapshot(spread_bps=19.9), thresholds())
    assert 0.0 < just_under.components["spread"] < 1.0
    on_limit = evaluate_market_quality(snapshot(spread_bps=20.0), thresholds())
    assert on_limit.components["spread"] == 0.0
    assert any(r.code == "SPREAD_TOO_WIDE" for r in on_limit.reasons)
    above = evaluate_market_quality(snapshot(spread_bps=25.0), thresholds())
    assert above.components["spread"] == 0.0


def test_depth_scoring_and_asymmetry() -> None:
    # both sides well above 1.5x -> full
    assert evaluate_market_quality(snapshot(), thresholds()).components["depth"] == 20.0
    # weaker side between min and 1.5x -> half points
    partial = evaluate_market_quality(
        snapshot(bid_depth_pusd=6_000.0, ask_depth_pusd=50_000.0), thresholds()
    )
    assert partial.components["depth"] == 10.0
    assert any(r.code == "DEPTH_ASYMMETRY" for r in partial.reasons)
    # weaker side below min -> zero + reason (asymmetric book punished via min)
    weak = evaluate_market_quality(
        snapshot(bid_depth_pusd=1_000.0, ask_depth_pusd=100_000.0), thresholds()
    )
    assert weak.components["depth"] == 0.0
    assert any(r.code == "INSUFFICIENT_DEPTH" for r in weak.reasons)


def test_depth_requires_fresh_reliable_book() -> None:
    for kwargs in ({"book_reliable": False}, {"book_fresh": False}):
        result = evaluate_market_quality(snapshot(**kwargs), thresholds())
        assert result.components["depth"] == 0.0


def test_volume_scoring_around_threshold() -> None:
    missing = evaluate_market_quality(
        snapshot(volume_24h_pusd=None, volume_fresh=False), thresholds()
    )
    assert missing.components["volume"] == 0.0
    assert any(r.code == "VOLUME_UNAVAILABLE" for r in missing.reasons)

    below = evaluate_market_quality(snapshot(volume_24h_pusd=99_999.0), thresholds())
    assert below.components["volume"] == 0.0
    assert any(r.code == "LOW_VOLUME" for r in below.reasons)

    on_limit = evaluate_market_quality(snapshot(volume_24h_pusd=100_000.0), thresholds())
    assert on_limit.components["volume"] == 7.5  # >= min but < 2x
    assert (
        evaluate_market_quality(snapshot(volume_24h_pusd=200_000.0), thresholds()).components[
            "volume"
        ]
        == 15.0
    )


def test_stale_volume_is_never_positive() -> None:
    result = evaluate_market_quality(
        snapshot(volume_24h_pusd=1_000_000.0, volume_fresh=False), thresholds()
    )
    assert result.components["volume"] == 0.0


def test_inactive_market_scores_zero_status_points() -> None:
    result = evaluate_market_quality(snapshot(instrument_active=False), thresholds())
    assert result.components["market_status"] == 0.0
    assert any(r.code == "MARKET_INACTIVE" for r in result.reasons)


def test_price_consistency_scoring() -> None:
    # tight prices: full 5 points (default snapshot ~2bps deviation)
    assert evaluate_market_quality(snapshot(), thresholds()).components["price_consistency"] == 5.0
    # moderate divergence (<= limit, > half): reduced
    moderate = evaluate_market_quality(
        snapshot(index_price=100.5),
        thresholds(),  # ~50bps < 75 limit
    )
    assert moderate.components["price_consistency"] == 2.0
    # beyond the limit: zero + PRICE_INVALID reason
    diverged = evaluate_market_quality(snapshot(index_price=101.0), thresholds())
    assert diverged.components["price_consistency"] == 0.0
    assert any(r.code == "PRICE_INVALID" for r in diverged.reasons)


def test_missing_reference_prices_never_score() -> None:
    result = evaluate_market_quality(snapshot(index_price=None, mid_price=None), thresholds())
    assert result.components["price_consistency"] == 0.0
    assert any(r.code == "PRICE_CONSISTENCY_UNAVAILABLE" for r in result.reasons)


def test_score_is_sum_of_components_and_bounded() -> None:
    result = evaluate_market_quality(
        snapshot(
            data_quality_status=InstrumentQualityStatus.DEGRADED,
            spread_bps=12.0,
            volume_24h_pusd=150_000.0,
        ),
        thresholds(),
    )
    assert result.score == round(sum(result.components.values()))
    assert 0 <= result.score <= 100
