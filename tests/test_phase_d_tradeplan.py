"""Phase D — TradePlan builder unit tests."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.config.models import TradePlanConfig
from app.decision.trade_plan import TradePlanBuilder, TradePlanBuildResult
from app.domain.enums import Direction, Regime
from app.domain.features import FeatureSnapshot
from app.domain.market import MarketSnapshot
from app.domain.regime import RegimeSnapshot
from app.domain.trade_plan import TradePlan, TradePlanStatus
from app.domain.signals import StrategySignal


def _signal(**overrides):
    defaults = dict(
        signal_id="sig_1",
        timestamp=datetime.utcnow(),
        symbol="BTC/USD",
        strategy="breakout_momentum",
        direction=Direction.LONG,
        reason_code="breakout_above_range",
        reason="breakout",
        entry_reference=100.0,
        timeframe="5m",
        regime=Regime.TRENDING_BULLISH,
        confidence=0.8,
        features={},
        correlation_id="corr_1",
        session_id="sess_1",
    )
    defaults.update(overrides)
    return StrategySignal(**defaults)


def _regime():
    return RegimeSnapshot(symbol="BTC/USD", timestamp=datetime.utcnow(), timeframe="15m", regime=Regime.TRENDING_BULLISH, reason="test")


def _features():
    values = {
        "rsi": 55.0, "atr": 1.0, "vwap": 99.5,
        "range_high": 105.0, "range_low": 95.0,
        "ema_20": 100.0, "ema_50": 99.0,
        "volatility": 0.015, "volume_ratio": 1.2,
    }
    return FeatureSnapshot(
        symbol="BTC/USD", timestamp=datetime.utcnow(), timeframe="5m",
        close=100.0, high=101.0, low=99.0, candle_count=50,
        spread_percent=0.05, volatility=0.015, volume_ratio=1.2,
        values=values, data_age_seconds=1.0, ready=True, missing=[],
    )


@pytest.fixture()
def builder():
    return TradePlanBuilder(TradePlanConfig())


def test_build_creates_ready_plan(builder):
    signal = _signal()
    result = builder.build_from_signal(signal, regime=_regime(), features=_features(), snapshot=None)
    plan = result.plan
    assert plan.status == TradePlanStatus.READY
    assert plan.direction == Direction.LONG
    assert plan.stop_price is not None
    assert plan.target_price is not None
    assert plan.stop_price < plan.entry_reference
    assert plan.target_price > plan.entry_reference


def test_build_emits_event(builder):
    signal = _signal()
    result = builder.build_from_signal(signal, regime=_regime(), features=_features(), snapshot=None)
    assert any(e["event"] == "TRADE_PLAN_CREATED" for e in result.events)


def test_wait_when_regime_unknown(builder):
    signal = _signal()
    result = builder.build_from_signal(signal, regime=RegimeSnapshot(symbol="BTC/USD", timestamp=datetime.utcnow(), timeframe="15m", regime=Regime.UNKNOWN, reason="test"), features=_features(), snapshot=None)
    assert result.plan.status == TradePlanStatus.WAIT


def test_invalidate_moves_to_invalidated(builder):
    signal = _signal()
    result = builder.build_from_signal(signal, regime=_regime(), features=_features(), snapshot=None)
    inv = builder.invalidate(result.plan, reason="regime_change")
    assert inv.plan.status == TradePlanStatus.INVALIDATED
    assert any("regime_change" in r for r in inv.plan.reasons)


def test_invalidate_emits_event(builder):
    signal = _signal()
    result = builder.build_from_signal(signal, regime=_regime(), features=_features(), snapshot=None)
    inv = builder.invalidate(result.plan, reason="regime_change")
    assert inv.events[0]["event"] == "TRADE_PLAN_INVALIDATED"
