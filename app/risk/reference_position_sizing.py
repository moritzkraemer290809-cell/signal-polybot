"""Hypothetical reference position sizing against a virtual account.

reference_cash_risk = virtual_account * risk_pct
risk_per_unit       = |entry_reference - invalidation|
reference_quantity  = reference_cash_risk / risk_per_unit  (rounded DOWN)
reference_notional  = reference_quantity * entry_reference

The virtual account is configuration - the engine never reads a real
balance, margin or position.  Rounding never increases risk; violated
minimums reject instead of inflating the reference risk.
"""

from __future__ import annotations

from app.config import RiskSettings
from app.risk.enums import PlanRejectionCode
from app.risk.models import (
    InstrumentRiskSnapshot,
    ReferencePosition,
    RiskDistance,
    RiskEngineError,
)
from app.risk.rounding import round_quantity_down


def size_reference_position(
    instrument: InstrumentRiskSnapshot,
    entry_reference_price: float,
    distance: RiskDistance,
    settings: RiskSettings,
) -> ReferencePosition:
    if entry_reference_price <= 0 or distance.absolute <= 0:
        raise RiskEngineError(PlanRejectionCode.REFERENCE_QUANTITY_INVALID, "invalid sizing inputs")
    cash_risk = (
        settings.virtual_reference_account_pusd * settings.reference_risk_per_plan_pct / 100.0
    )
    risk_per_unit = distance.absolute
    raw_quantity = cash_risk / risk_per_unit
    quantity = round_quantity_down(raw_quantity, instrument.quantity_decimals)
    if quantity <= 0:
        raise RiskEngineError(
            PlanRejectionCode.REFERENCE_QUANTITY_INVALID,
            f"reference quantity rounds to zero at {instrument.quantity_decimals} "
            "decimals - reference risk cannot be represented",
        )
    notional = quantity * entry_reference_price
    min_notional = instrument.min_notional
    if min_notional is None:
        raise RiskEngineError(
            PlanRejectionCode.INSTRUMENT_RISK_DATA_MISSING,
            "instrument min_notional unavailable - refusing to guess",
        )
    if notional < min_notional:
        # honouring the exchange minimum would force MORE risk than the
        # reference budget allows -> conservative rejection, never upsizing
        raise RiskEngineError(
            PlanRejectionCode.MIN_NOTIONAL_VIOLATION,
            f"reference notional {notional:.2f} pUSD is below the instrument "
            f"minimum {min_notional:.2f} pUSD; upsizing would exceed the "
            "reference risk budget",
        )
    if notional > settings.max_reference_notional_pusd:
        quantity = round_quantity_down(
            settings.max_reference_notional_pusd / entry_reference_price,
            instrument.quantity_decimals,
        )
        if quantity <= 0:
            raise RiskEngineError(
                PlanRejectionCode.REFERENCE_QUANTITY_INVALID,
                "notional cap collapses the reference quantity to zero",
            )
        notional = quantity * entry_reference_price
        if notional < min_notional:
            raise RiskEngineError(
                PlanRejectionCode.MIN_NOTIONAL_VIOLATION,
                "notional cap pushes the reference below the instrument minimum",
            )
    effective_cash_risk = quantity * risk_per_unit
    if effective_cash_risk <= 0 or effective_cash_risk > cash_risk * 1.0001:
        raise RiskEngineError(
            PlanRejectionCode.REFERENCE_QUANTITY_INVALID,
            "post-rounding risk check failed - rounding may never increase risk",
        )
    return ReferencePosition(
        virtual_account_pusd=settings.virtual_reference_account_pusd,
        risk_per_plan_pct=settings.reference_risk_per_plan_pct,
        reference_cash_risk=cash_risk,
        risk_per_unit=risk_per_unit,
        raw_quantity=raw_quantity,
        reference_quantity=quantity,
        reference_notional=notional,
        effective_cash_risk=effective_cash_risk,
        quantity_decimals=instrument.quantity_decimals,
    )
