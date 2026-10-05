"""Phase D.1.1 regression coverage for authoritative TradePlan risk."""
from __future__ import annotations

import pytest

from app.config.models import PositionSizingConfig, TradePlanConfig
from app.decision.trade_plan import TradePlanBuilder
from app.domain.trade_plan import TradePlanStatus
from app.portfolio.position_sizing import PositionSizer
from tests.support import make_features, make_regime, make_signal


def _build(*, equity: float = 100_000.0, risk_percent: float = 0.5, stop_distance: float = 500.0,
           max_notional: float | None = None, minimum_quantity: float = 0.000001,
           quantity_precision: int = 6, price: float = 30_000.0, stale: bool = False):
    signal = make_signal(price=price, stop_distance=stop_distance)
    config = PositionSizingConfig(
        risk_per_trade_percent=risk_percent,
        minimum_quantity=minimum_quantity,
        quantity_precision=quantity_precision,
    )
    sizing = PositionSizer(config).size(
        signal, equity, reference_price=price, max_notional=max_notional,
    )
    plan = TradePlanBuilder(TradePlanConfig()).build_from_signal(
        signal,
        regime=make_regime(),
        features=make_features(),
        sizing=sizing,
        risk_percent=risk_percent,
        maximum_notional=max_notional,
        freshness_stale=stale,
    ).plan
    return sizing, plan


@pytest.mark.parametrize("equity", [10_000.0, 100_000.0, 1_000_000.0])
@pytest.mark.parametrize("risk_percent", [0.25, 0.5, 1.0])
def test_tradeplan_risk_equals_position_sizer_for_equity_and_risk_percent(equity, risk_percent):
    sizing, plan = _build(equity=equity, risk_percent=risk_percent)
    assert sizing.ok
    assert plan.risk_amount == sizing.final_risk_amount
    assert plan.final_risk_amount == sizing.final_risk_amount
    assert plan.position_quantity == sizing.final_quantity
    assert plan.risk_budget == sizing.risk_budget
    assert plan.extra["risk_budget_source"] == "position_sizer"


@pytest.mark.parametrize("stop_distance", [1.0, 500.0, 5_000.0])
def test_tradeplan_uses_the_sizer_stop_distance(stop_distance):
    sizing, plan = _build(stop_distance=stop_distance)
    assert plan.stop_distance == sizing.stop_distance
    assert plan.risk_amount == sizing.final_quantity * sizing.stop_distance


def test_tradeplan_risk_reflects_maximum_notional_cap():
    sizing, plan = _build(max_notional=2_000.0)
    assert sizing.ok
    assert plan.final_notional <= 2_000.0
    assert plan.risk_amount == sizing.final_risk_amount
    assert plan.risk_amount < sizing.risk_budget


@pytest.mark.parametrize("equity", [0.0, -1.0])
def test_blocked_equity_does_not_manufacture_tradeplan_risk(equity):
    sizing, plan = _build(equity=equity)
    assert not sizing.ok
    assert plan.status == TradePlanStatus.WAIT
    assert plan.risk_amount == 0.0
    assert plan.position_quantity is None
    assert sizing.rejected_reason in plan.reasons[-1]


def test_malformed_stop_is_explicitly_blocked():
    signal = make_signal(stop_reference=None)
    sizing = PositionSizer(PositionSizingConfig()).size(signal, 100_000.0, reference_price=signal.entry_reference)
    plan = TradePlanBuilder(TradePlanConfig()).build_from_signal(
        signal, regime=make_regime(), features=make_features(), sizing=sizing,
    ).plan
    assert sizing.rejected_reason == "invalid_stop_distance"
    assert plan.status == TradePlanStatus.WAIT
    assert plan.risk_amount == 0.0


def test_quantity_precision_and_minimum_are_reflected_in_final_risk():
    sizing, plan = _build(stop_distance=1_000_000_000.0, minimum_quantity=0.0001, quantity_precision=4)
    assert not sizing.ok
    assert plan.position_quantity is None
    assert plan.risk_amount == 0.0


def test_missing_price_is_explicitly_blocked():
    signal = make_signal(price=0.0, stop_distance=500.0)
    sizing = PositionSizer(PositionSizingConfig()).size(signal, 100_000.0, reference_price=None)
    assert not sizing.ok
    assert sizing.rejected_reason == "invalid_reference_price"


def test_stale_market_data_blocks_plan_without_changing_risk():
    sizing, plan = _build(stale=True)
    assert sizing.ok
    assert plan.status == TradePlanStatus.WAIT
    assert plan.risk_amount == sizing.final_risk_amount
    assert any(condition["name"] == "market_data_fresh" and not condition["met"] for condition in plan.entry_conditions)