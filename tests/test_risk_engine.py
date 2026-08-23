"""Risk engine: hard gates, technical framing, sizing, leverage/margin,
cost gates, eligibility scoring and overrides.

All scenarios are synthetic fixtures (tests/risk_helpers).  The engine core
runs without DB, Redis or network.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.risk_helpers import (
    AS_OF,
    COST_SETTINGS,
    RISK_SETTINGS,
    bearish_context,
    default_levels,
    make_book,
    make_candidate,
    make_context,
    make_fee_schedule,
    make_funding,
    make_instrument,
)

from app.config import CostSettings, RiskSettings
from app.risk.enums import PlanRejectionCode, PlanStatus
from app.risk.explainability import dashboard_plan_details, summarize_plan
from app.risk.models import TechnicalLevel
from app.risk.risk_engine import effective_risk_settings, evaluate_candidate


def evaluate(context, risk=RISK_SETTINGS, costs=COST_SETTINGS):
    return evaluate_candidate(context, risk, costs)


def assert_rejected(outcome, code: PlanRejectionCode):
    assert outcome.plan is None
    assert outcome.rejection is not None
    assert outcome.rejection.primary_code is code, outcome.rejection.detail
    return outcome.rejection


# --------------------------------------------------------------- hard gates


def test_bot_paused_blocks_new_plans() -> None:
    outcome = evaluate(make_context(bot_paused=True))
    assert_rejected(outcome, PlanRejectionCode.BOT_PAUSED)


def test_inactive_watchlist_blocks() -> None:
    outcome = evaluate(make_context(watchlist_active=False))
    assert_rejected(outcome, PlanRejectionCode.INSTRUMENT_INACTIVE)


def test_blocked_or_unknown_session_rejects() -> None:
    outcome = evaluate(make_context(session_allowed=False, session_state="UNKNOWN"))
    rejection = assert_rejected(outcome, PlanRejectionCode.SESSION_NOT_ALLOWED)
    assert "UNKNOWN" in rejection.detail


def test_only_confirmed_candidates_by_default() -> None:
    outcome = evaluate(make_context(candidate=make_candidate(state="DETECTED")))
    assert_rejected(outcome, PlanRejectionCode.CANDIDATE_NOT_CONFIRMED)
    relaxed = RiskSettings(
        _env_file=None,
        require_confirmed_candidate=False,
        invalidation_buffer_atr_multiple=1.5,
    )
    outcome = evaluate(make_context(candidate=make_candidate(state="DETECTED")), risk=relaxed)
    assert outcome.plan is not None  # gate is configuration-driven


def test_expired_and_superseded_candidates() -> None:
    expired = make_candidate(expiry_at=AS_OF - timedelta(minutes=1))
    assert_rejected(evaluate(make_context(candidate=expired)), PlanRejectionCode.CANDIDATE_EXPIRED)
    superseded = make_candidate(state="SUPERSEDED")
    assert_rejected(
        evaluate(make_context(candidate=superseded)), PlanRejectionCode.CANDIDATE_SUPERSEDED
    )


def test_instrument_and_data_gates() -> None:
    assert_rejected(
        evaluate(make_context(instrument=make_instrument(status="DELISTED"))),
        PlanRejectionCode.INSTRUMENT_INACTIVE,
    )
    assert_rejected(
        evaluate(
            make_context(
                instrument=make_instrument(data_quality_ok=False, data_quality_status="STALE")
            )
        ),
        PlanRejectionCode.DATA_QUALITY_INSUFFICIENT,
    )
    assert_rejected(
        evaluate(make_context(book=make_book(fresh=False))),
        PlanRejectionCode.ORDERBOOK_NOT_FRESH,
    )


def test_missing_instrument_risk_data_blocks_conservatively() -> None:
    assert_rejected(
        evaluate(make_context(instrument=make_instrument(mark_price=None, mid_price=None))),
        PlanRejectionCode.INSTRUMENT_RISK_DATA_MISSING,
    )
    assert_rejected(
        evaluate(make_context(instrument=make_instrument(min_notional=None))),
        PlanRejectionCode.INSTRUMENT_RISK_DATA_MISSING,
    )
    assert_rejected(
        evaluate(make_context(instrument=make_instrument(max_leverage=None))),
        PlanRejectionCode.MAX_LEVERAGE_UNAVAILABLE,
    )


def test_fee_schedule_required() -> None:
    assert_rejected(
        evaluate(make_context(fee_schedule=None)), PlanRejectionCode.FEE_SCHEDULE_UNAVAILABLE
    )
    inactive = make_fee_schedule(active=False)
    assert_rejected(
        evaluate(make_context(fee_schedule=inactive)),
        PlanRejectionCode.FEE_SCHEDULE_UNAVAILABLE,
    )


def test_equity_overnight_blocked_by_default() -> None:
    candidate = make_candidate(asset_class="EQUITY")
    instrument = make_instrument(asset_class="EQUITY")
    outcome = evaluate(
        make_context(candidate=candidate, instrument=instrument, equity_overnight_risk=True)
    )
    assert_rejected(outcome, PlanRejectionCode.EQUITY_OVERNIGHT_BLOCKED)
    # a hold fitting inside the session passes this gate
    outcome = evaluate(
        make_context(candidate=candidate, instrument=instrument, equity_overnight_risk=False)
    )
    assert (
        outcome.rejection is None
        or outcome.rejection.primary_code is not PlanRejectionCode.EQUITY_OVERNIGHT_BLOCKED
    )


# ------------------------------------------------- invalidation and framing


def test_full_eligible_plan_bullish() -> None:
    outcome = evaluate(make_context())
    assert outcome.rejection is None
    plan = outcome.plan
    assert plan is not None
    assert plan.status is PlanStatus.ELIGIBLE
    # invalidation below the reclaimed level with bps+ATR buffer, tick-rounded
    assert plan.invalidation.invalidation_price == pytest.approx(96.45)
    assert plan.entry_zone.entry_reference_price == pytest.approx(99.6)
    assert plan.risk_distance.bps == pytest.approx(316, rel=0.01)
    assert plan.targets[0].price == pytest.approx(107.0)  # nearest technical level
    assert plan.net_rr_primary is not None and plan.net_rr_primary >= 1.8
    assert plan.costs.cost_to_risk_pct <= 15.0
    assert plan.leverage.recommended_reference_leverage <= 3.0
    assert plan.eligibility_score >= RISK_SETTINGS.eligibility_min_score
    assert not plan.margin.approximated
    assert plan.liquidation_buffer.sufficient


def test_full_eligible_plan_bearish_mirrored() -> None:
    outcome = evaluate(bearish_context())
    plan = outcome.plan
    assert plan is not None
    assert plan.direction == "BEARISH"
    # invalidation ABOVE the level for bearish research context
    assert plan.invalidation.invalidation_price == pytest.approx(102.75)
    assert plan.invalidation.invalidation_price > plan.entry_zone.entry_reference_price
    assert plan.targets[0].price < plan.entry_zone.entry_reference_price


def test_invalidation_wrong_side_rejected() -> None:
    candidate = make_candidate(
        referenced_levels=({"type": "SWING_LOW", "price": 105.0, "timeframe": "15m"},)
    )
    outcome = evaluate(make_context(candidate=candidate))
    assert_rejected(outcome, PlanRejectionCode.INVALIDATION_WRONG_SIDE)


def test_invalidation_too_close_inside_atr_noise() -> None:
    tight = RiskSettings(_env_file=None, invalidation_buffer_atr_multiple=0.0)
    outcome = evaluate(make_context(risk_settings=tight), risk=tight)
    assert_rejected(outcome, PlanRejectionCode.INVALIDATION_TOO_CLOSE)


def test_invalidation_too_far_disproportionate() -> None:
    wide = RiskSettings(_env_file=None, invalidation_buffer_atr_multiple=4.5)
    outcome = evaluate(make_context(risk_settings=wide), risk=wide)
    assert_rejected(outcome, PlanRejectionCode.INVALIDATION_TOO_FAR)


def test_missing_atr_blocks_invalidation() -> None:
    outcome = evaluate(make_context(atr_5m=None))
    assert_rejected(outcome, PlanRejectionCode.INVALIDATION_UNAVAILABLE)


def test_entry_unavailable_without_levels_or_bbo() -> None:
    candidate = make_candidate(referenced_levels=())
    assert_rejected(
        evaluate(make_context(candidate=candidate)),
        PlanRejectionCode.REFERENCE_ENTRY_UNAVAILABLE,
    )
    assert_rejected(
        evaluate(make_context(bbo_fresh=False)),
        PlanRejectionCode.REFERENCE_ENTRY_UNAVAILABLE,
    )


def test_entry_chase_limit() -> None:
    # price ran >20bps beyond the technical zone -> no entry reference
    outcome = evaluate(make_context(best_ask=100.5, book=make_book(asks=[(100.5, 50.0)])))
    assert_rejected(outcome, PlanRejectionCode.REFERENCE_ENTRY_UNAVAILABLE)


def test_wide_spread_makes_entry_unusable() -> None:
    outcome = evaluate(
        make_context(
            best_bid=98.0,
            best_ask=99.6,
            book=make_book(bids=[(98.0, 50.0)], asks=[(99.6, 50.0)]),
        )
    )
    assert_rejected(outcome, PlanRejectionCode.REFERENCE_ENTRY_UNAVAILABLE)


def test_targets_only_from_real_levels_never_invented() -> None:
    outcome = evaluate(make_context())
    plan = outcome.plan
    assert plan is not None
    provided = {level.price for level in default_levels()}
    assert {target.price for target in plan.targets} <= provided
    assert len(plan.targets) == 2


def test_no_target_and_wrong_side_targets() -> None:
    no_levels = make_context(levels=())
    assert_rejected(evaluate(no_levels), PlanRejectionCode.REFERENCE_TARGET_UNAVAILABLE)
    below_only = make_context(
        levels=(
            TechnicalLevel(
                price=95.0, kind="SWING_LOW", timeframe="15m", relevance=0.8, source_id="swing:9"
            ),
        )
    )
    assert_rejected(evaluate(below_only), PlanRejectionCode.TARGET_WRONG_SIDE)
    irrelevant = make_context(
        levels=(
            TechnicalLevel(
                price=107.0, kind="SWING_HIGH", timeframe="15m", relevance=0.1, source_id="swing:9"
            ),
        )
    )
    assert_rejected(evaluate(irrelevant), PlanRejectionCode.REFERENCE_TARGET_UNAVAILABLE)


# ------------------------------------------------------- reference sizing


def test_reference_sizing_formulas_and_rounding() -> None:
    plan = evaluate(make_context()).plan
    assert plan is not None
    position = plan.position
    assert position.virtual_account_pusd == 10_000.0
    assert position.reference_cash_risk == pytest.approx(50.0)  # 0.5 % of 10k
    assert position.risk_per_unit == pytest.approx(99.6 - 96.45)
    assert position.reference_quantity == pytest.approx(15.873)  # floored at 3 decimals
    assert position.reference_quantity <= position.raw_quantity
    assert position.reference_notional == pytest.approx(15.873 * 99.6)
    assert position.effective_cash_risk <= position.reference_cash_risk + 1e-6


def test_min_notional_violation_rejects_instead_of_upsizing() -> None:
    instrument = make_instrument(min_notional=5_000.0)
    outcome = evaluate(make_context(instrument=instrument))
    assert_rejected(outcome, PlanRejectionCode.MIN_NOTIONAL_VIOLATION)


def test_quantity_rounding_to_zero_rejects() -> None:
    tiny = RiskSettings(
        _env_file=None,
        virtual_reference_account_pusd=10.0,
        invalidation_buffer_atr_multiple=1.5,
    )
    instrument = make_instrument(quantity_decimals=0, min_notional=0.0)
    outcome = evaluate(make_context(risk_settings=tiny, instrument=instrument), risk=tiny)
    assert_rejected(outcome, PlanRejectionCode.REFERENCE_QUANTITY_INVALID)


def test_max_reference_notional_caps_quantity() -> None:
    capped = RiskSettings(
        _env_file=None,
        max_reference_notional_pusd=800.0,
        invalidation_buffer_atr_multiple=1.5,
    )
    plan = evaluate(make_context(risk_settings=capped), risk=capped).plan
    assert plan is not None
    assert plan.position.reference_notional <= 800.0 + 1e-6
    # the cap must never push the reference below the exchange minimum
    small_min = make_instrument(min_notional=1_000.0)
    outcome = evaluate(make_context(risk_settings=capped, instrument=small_min), risk=capped)
    assert_rejected(outcome, PlanRejectionCode.MIN_NOTIONAL_VIOLATION)


# ------------------------------------------------- leverage and margin


def test_leverage_hard_cap_and_market_cap() -> None:
    plan = evaluate(make_context()).plan
    assert plan is not None
    assert plan.leverage.recommended_reference_leverage == pytest.approx(3.0)
    assert plan.leverage.max_market_leverage == 10

    low_market = make_instrument(
        max_leverage=2, risk_tiers=({"lower_bound": 0, "max_leverage": 2},)
    )
    plan = evaluate(make_context(instrument=low_market)).plan
    assert plan is not None
    assert plan.leverage.recommended_reference_leverage <= 2.0


def test_elevated_volatility_reduces_leverage_range() -> None:
    candidate = make_candidate(features={"atr_percentile_1h": 85.0})
    plan = evaluate(make_context(candidate=candidate)).plan
    assert plan is not None
    assert plan.leverage.recommended_reference_leverage <= 2.0
    assert any("volatility" in reason for reason in plan.leverage.suitability_reasons)


def test_margin_model_unavailable_blocks_by_default() -> None:
    instrument = make_instrument(initial_margin_rate=None, maintenance_margin_rate=None)
    outcome = evaluate(make_context(instrument=instrument))
    assert_rejected(outcome, PlanRejectionCode.MARGIN_MODEL_UNAVAILABLE)


def test_partial_margin_data_blocks() -> None:
    instrument = make_instrument(maintenance_margin_rate=None)
    outcome = evaluate(make_context(instrument=instrument))
    assert_rejected(outcome, PlanRejectionCode.MAINTENANCE_MARGIN_UNAVAILABLE)


def test_approximated_margin_model_requires_optin_and_is_flagged() -> None:
    instrument = make_instrument(initial_margin_rate=None, maintenance_margin_rate=None)
    optin = RiskSettings(
        _env_file=None,
        allow_approximated_margin_model=True,
        invalidation_buffer_atr_multiple=1.5,
    )
    outcome = evaluate(make_context(risk_settings=optin, instrument=instrument), risk=optin)
    plan = outcome.plan
    assert plan is not None, outcome.rejection
    assert plan.margin.approximated
    assert "APPROXIMATED_MARGIN_MODEL" in plan.margin.approximation_flags
    assert any("APPROXIMATED" in warning for warning in plan.warnings)
    # the approximated initial margin bound caps the reference leverage
    assert plan.leverage.recommended_reference_leverage <= 1.0 / optin.approx_initial_margin_rate


def test_liquidation_buffer_insufficient_blocks() -> None:
    strict = RiskSettings(
        _env_file=None,
        min_liquidation_buffer_bps=50_000.0,
        invalidation_buffer_atr_multiple=1.5,
    )
    outcome = evaluate(make_context(risk_settings=strict), risk=strict)
    assert_rejected(outcome, PlanRejectionCode.LIQUIDATION_BUFFER_INSUFFICIENT)


def test_no_cross_margin_modeling() -> None:
    invalid = RiskSettings(
        _env_file=None, isolated_margin_only=False, invalidation_buffer_atr_multiple=1.5
    )
    outcome = evaluate(make_context(risk_settings=invalid), risk=invalid)
    assert_rejected(outcome, PlanRejectionCode.CONFIGURATION_INVALID)


def test_liquidation_check_uses_mark_price_reference() -> None:
    plan = evaluate(make_context()).plan
    assert plan is not None
    assert plan.margin.mark_price_reference == pytest.approx(100.0)
    assert "no liquidation guarantee" in plan.margin.detail


# --------------------------------------------------------- cost hard gates


def test_net_rr_below_threshold() -> None:
    strict = RiskSettings(_env_file=None, min_net_rr=2.5, invalidation_buffer_atr_multiple=1.5)
    outcome = evaluate(make_context(risk_settings=strict), risk=strict)
    assert_rejected(outcome, PlanRejectionCode.NET_RR_BELOW_THRESHOLD)


def test_cost_to_risk_excessive() -> None:
    strict = RiskSettings(
        _env_file=None, max_cost_to_risk_pct=5.0, invalidation_buffer_atr_multiple=1.5
    )
    outcome = evaluate(make_context(risk_settings=strict), risk=strict)
    assert_rejected(outcome, PlanRejectionCode.COST_TO_RISK_EXCESSIVE)


def test_funding_cost_excessive() -> None:
    heavy = make_funding(
        current_rate=0.01,
        history=tuple((AS_OF - timedelta(hours=h), 0.01) for h in range(6, 0, -1)),
    )
    outcome = evaluate(make_context(funding=heavy))
    assert_rejected(outcome, PlanRejectionCode.FUNDING_COST_EXCESSIVE)


def test_net_target_non_positive() -> None:
    expensive = CostSettings(_env_file=None, default_taker_fee_rate=0.005)
    near_target = make_context(
        cost_settings=expensive,
        levels=(
            TechnicalLevel(
                price=100.2, kind="SWING_HIGH", timeframe="15m", relevance=0.8, source_id="swing:9"
            ),
        ),
    )
    outcome = evaluate(near_target, costs=expensive)
    assert_rejected(outcome, PlanRejectionCode.NET_TARGET_NON_POSITIVE)


def test_orderbook_depth_insufficient_via_cost_engine() -> None:
    shallow = make_book(asks=[(99.6, 0.5)], bids=[(99.55, 0.5)])
    outcome = evaluate(make_context(book=shallow, best_bid=99.55, best_ask=99.6))
    assert_rejected(outcome, PlanRejectionCode.ORDERBOOK_DEPTH_INSUFFICIENT)


def test_high_candidate_score_never_overrides_risk_gates() -> None:
    strict = RiskSettings(_env_file=None, min_net_rr=2.5, invalidation_buffer_atr_multiple=1.5)
    perfect = make_candidate(setup_score=100)
    outcome = evaluate(make_context(risk_settings=strict, candidate=perfect), risk=strict)
    assert_rejected(outcome, PlanRejectionCode.NET_RR_BELOW_THRESHOLD)


def test_eligibility_score_threshold() -> None:
    strict = RiskSettings(
        _env_file=None, eligibility_min_score=95, invalidation_buffer_atr_multiple=1.5
    )
    outcome = evaluate(make_context(risk_settings=strict), risk=strict)
    rejection = assert_rejected(outcome, PlanRejectionCode.ELIGIBILITY_SCORE_BELOW_THRESHOLD)
    assert "not a win probability" in rejection.detail


# ----------------------------------------------- overrides and determinism


def test_symbol_overrides_only_whitelisted_numeric_keys() -> None:
    settings = RiskSettings(
        _env_file=None,
        invalidation_buffer_atr_multiple=1.5,
        symbol_overrides_json={
            "BTC-PERP": {
                "min_net_rr": 2.5,
                "require_fresh_orderbook": False,  # NOT overridable
                "max_reference_leverage": 10.0,  # clamped to 3
            }
        },
    )
    effective = effective_risk_settings(settings, "BTC-PERP", "CRYPTO")
    assert effective.min_net_rr == 2.5
    assert effective.require_fresh_orderbook is True  # safety gate survives
    assert effective.max_reference_leverage == 3.0  # V1 hard cap survives
    outcome = evaluate(make_context(risk_settings=settings), risk=settings)
    assert_rejected(outcome, PlanRejectionCode.NET_RR_BELOW_THRESHOLD)


def test_asset_class_override_applies() -> None:
    settings = RiskSettings(
        _env_file=None,
        invalidation_buffer_atr_multiple=1.5,
        asset_class_overrides_json={"CRYPTO": {"eligibility_min_score": 90}},
    )
    outcome = evaluate(make_context(risk_settings=settings), risk=settings)
    assert_rejected(outcome, PlanRejectionCode.ELIGIBILITY_SCORE_BELOW_THRESHOLD)


def test_plan_is_deterministic_with_stable_dedupe_key() -> None:
    first = evaluate(make_context(candidate=make_candidate(candidate_id=_FIXED_ID))).plan
    second = evaluate(make_context(candidate=make_candidate(candidate_id=_FIXED_ID))).plan
    assert first is not None and second is not None
    assert first.plan_id != second.plan_id  # fresh uuid per evaluation
    assert first.dedupe_key == second.dedupe_key
    assert first.eligibility_score == second.eligibility_score
    assert first.invalidation.invalidation_price == second.invalidation.invalidation_price


def test_plan_versioning_and_score_component_sum() -> None:
    plan = evaluate(make_context()).plan
    assert plan is not None
    assert plan.risk_model_version == RISK_SETTINGS.model_version
    assert plan.cost_model_version == COST_SETTINGS.model_version
    assert plan.fee_schedule_version == COST_SETTINGS.fee_schedule_version
    assert plan.execution_assumption_version == COST_SETTINGS.execution_assumption_version
    assert plan.strategy_version == "1.0.0"
    total = sum(component.awarded for component in plan.score_components)
    assert round(total) == plan.eligibility_score
    assert plan.data_timestamps["bbo_at"] != "n/a"


def test_summaries_carry_disclaimers_and_hide_levels_in_status() -> None:
    plan = evaluate(make_context()).plan
    assert plan is not None
    status_summary = summarize_plan(plan)
    assert "kein Trade-Signal" in status_summary["note"]
    assert "model_reference_levels" not in status_summary
    assert "assumption only" in status_summary["fee_schedule_version"]
    dashboard = dashboard_plan_details(plan)
    assert "Risk Research" in dashboard["disclaimer"]
    levels = dashboard["model_reference_levels"]
    assert "keine Handelsanweisung" in levels["label"]
    assert "keine echte Kontogroesse" in dashboard["reference_position"]["label"]
    assert "kein optimaler Hebel" in dashboard["leverage_suitability"]["label"]


import uuid as _uuid  # noqa: E402

_FIXED_ID = _uuid.UUID("00000000-0000-0000-0000-00000000abcd")
