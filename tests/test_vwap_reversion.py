"""VWAP-reversion strategy unit tests (APPROVED design v0.1).

Deterministic truth-table over entry rules, regime gates, fail-closed
behavior, long-only output, and registry resolution. No broker, no risk
engine, no network — pure StrategyContext in, signal-or-None out.
"""
from __future__ import annotations

import pytest

from app.config.loader import get_env, load_config
from app.core.clock import utcnow
from app.domain.enums import Direction, ReasonCode, Regime
from app.domain.features import FeatureSnapshot
from app.domain.regime import RegimeSnapshot
from app.strategies.base import StrategyContext
from app.strategies.registry import build_strategy, known_strategies
from app.strategies.vwap_reversion import VwapReversionStrategy


def _config():
    config = load_config(env=get_env())
    config.strategy.name = "vwap_reversion"
    config.strategy.version = "0.1"
    return config


def _features(**overrides) -> FeatureSnapshot:
    now = utcnow()
    values = {
        "rsi": 30.0,
        "atr": 100.0,
        "vwap": 86000.0,
        "range_high": 86500.0,
        "range_low": 85000.0,
        "ema_20": 85800.0,
        "ema_50": 85700.0,
        "volume_ratio": 1.2,
    }
    values.update(overrides)
    return FeatureSnapshot(
        symbol="BTC/USD",
        timestamp=now,
        timeframe="5m",
        close=85800.0,
        high=85900.0,
        low=85700.0,
        values=values,
    )


def _regime(regime: Regime) -> RegimeSnapshot:
    return RegimeSnapshot(
        symbol="BTC/USD", timestamp=utcnow(), timeframe="5m", regime=regime
    )


def _context(features=None, regime=None, position_open=False) -> StrategyContext:
    return StrategyContext(
        symbol="BTC/USD",
        timeframe="5m",
        features=features or _features(),
        regime=regime or _regime(Regime.RANGE_BOUND),
        now=utcnow(),
        position_open=position_open,
    )


def _strategy() -> VwapReversionStrategy:
    return VwapReversionStrategy(_config().strategy)


# 1. valid long setup ---------------------------------------------------------
def test_valid_long_setup_emits_long():
    signal = _strategy().evaluate(_context())
    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_reference == 85800.0


# 2. flat-book requirement ----------------------------------------------------
def test_position_open_returns_none():
    assert _strategy().evaluate(_context(position_open=True)) is None


# 3. feature readiness ---------------------------------------------------------
def test_features_not_ready_returns_none():
    assert _strategy().evaluate(_context(features=_features(ema_50=None))) is None


# 4-8. missing required features ------------------------------------------------
@pytest.mark.parametrize("field", ["vwap", "atr", "rsi", "range_low"])
def test_missing_required_value_returns_none(field):
    assert _strategy().evaluate(_context(features=_features(**{field: None}))) is None


def test_missing_high_returns_none():
    features = _features()
    features.high = None
    assert _strategy().evaluate(_context(features=features)) is None


def test_missing_low_returns_none():
    features = _features()
    features.low = None
    assert _strategy().evaluate(_context(features=features)) is None


# 9. zero/invalid ATR ------------------------------------------------------------
@pytest.mark.parametrize("atr", [0.0, -5.0])
def test_non_positive_atr_returns_none(atr):
    assert _strategy().evaluate(_context(features=_features(atr=atr))) is None


# 10. high <= low recovery guard ---------------------------------------------------
@pytest.mark.parametrize("high,low", [(85800.0, 85800.0), (85700.0, 85800.0)])
def test_degenerate_bar_returns_none(high, low):
    features = _features()
    features.high = high
    features.low = low
    assert _strategy().evaluate(_context(features=features)) is None


# 11. insufficient VWAP stretch ------------------------------------------------------
def test_stretch_below_threshold_returns_none():
    # stretch = (86000-85950)/100 = 0.5 < 1.0; recovery still fine
    features = _features()
    features.close = 85950.0
    features.high = 86000.0
    features.low = 85900.0
    assert _strategy().evaluate(_context(features=features)) is None


def test_price_above_vwap_returns_none():
    features = _features()
    features.close = 86100.0
    assert _strategy().evaluate(_context(features=features)) is None


# 12. RSI above threshold ------------------------------------------------------------
def test_rsi_above_threshold_returns_none():
    assert _strategy().evaluate(_context(features=_features(rsi=40.0))) is None


def test_rsi_at_threshold_is_accepted():
    assert _strategy().evaluate(_context(features=_features(rsi=35.0))) is not None


# 13. range breakdown ------------------------------------------------------------------
def test_close_below_range_low_returns_none():
    features = _features()
    features.close = 84900.0
    features.low = 84800.0
    assert _strategy().evaluate(_context(features=features)) is None


# 14. insufficient recovery ---------------------------------------------------------------
def test_weak_intrabar_recovery_returns_none():
    # recovery = (85720-85700)/(85900-85700) = 0.1 < 0.30
    features = _features()
    features.close = 85720.0
    assert _strategy().evaluate(_context(features=features)) is None


# 15. volume ratio ----------------------------------------------------------------------------
def test_insufficient_volume_ratio_returns_none():
    assert _strategy().evaluate(_context(features=_features(volume_ratio=0.5))) is None


def test_unmeasurable_volume_ratio_is_accepted():
    assert _strategy().evaluate(_context(features=_features(volume_ratio=None))) is not None


# 16-17. accepted regimes -----------------------------------------------------------------------
@pytest.mark.parametrize("regime", [Regime.RANGE_BOUND, Regime.LOW_VOLATILITY])
def test_tradeable_regimes_accepted(regime):
    assert _strategy().evaluate(_context(regime=_regime(regime))) is not None


# 18-21. refused regimes ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "regime",
    [Regime.UNKNOWN, Regime.TRENDING_BULLISH, Regime.TRENDING_BEARISH, Regime.HIGH_VOLATILITY],
)
def test_non_tradeable_regimes_rejected(regime):
    assert _strategy().evaluate(_context(regime=_regime(regime))) is None


# 22-23. long-only behavior ------------------------------------------------------------------------------
def test_only_long_is_ever_emitted():
    strategy = _strategy()
    regimes = [Regime.RANGE_BOUND, Regime.LOW_VOLATILITY, Regime.UNKNOWN,
               Regime.TRENDING_BULLISH, Regime.TRENDING_BEARISH, Regime.HIGH_VOLATILITY]
    for regime in regimes:
        for rsi in (20.0, 30.0, 35.0, 50.0, 70.0):
            signal = strategy.evaluate(_context(features=_features(rsi=rsi), regime=_regime(regime)))
            assert signal is None or signal.direction is Direction.LONG


# 24. spread rejection ----------------------------------------------------------------------
def test_excessive_spread_returns_none():
    features = _features()
    features.spread_percent = 0.5
    assert _strategy().evaluate(_context(features=features)) is None


def test_unmeasurable_spread_is_accepted():
    assert _strategy().evaluate(_context()) is not None


# 25. reason-code correctness ----------------------------------------------------------------------
def test_reason_codes_describe_reversion_setup():
    signal = _strategy().evaluate(_context())
    assert signal is not None
    assert signal.reason_code is ReasonCode.REVERSION_SETUP
    assert ReasonCode.VWAP_STRETCH.value in signal.reason
    assert ReasonCode.RANGE_CONTAINED.value in signal.reason
    assert signal.strategy == "vwap_reversion"
    assert signal.regime is Regime.RANGE_BOUND


# 26. no broker/risk side effects ------------------------------------------------------------------
def test_evaluation_is_pure_and_deterministic():
    strategy = _strategy()
    first = strategy.evaluate(_context())
    second = strategy.evaluate(_context())
    assert first is not None and second is not None
    for field in ("direction", "entry_reference", "reason", "reason_code", "strategy", "regime"):
        assert getattr(first, field) == getattr(second, field)


# registry ----------------------------------------------------------------------------------
def test_registry_resolves_breakout_momentum():
    from app.strategies.breakout_momentum import BreakoutMomentumStrategy

    config = load_config(env=get_env())
    config.strategy.name = "breakout_momentum"
    strategy = build_strategy(config.strategy)
    assert isinstance(strategy, BreakoutMomentumStrategy)
    assert strategy.name == "breakout_momentum"


def test_registry_resolves_vwap_reversion():
    config = load_config(env=get_env())
    config.strategy.name = "vwap_reversion"
    strategy = build_strategy(config.strategy)
    assert isinstance(strategy, VwapReversionStrategy)


def test_registry_lists_both_strategies():
    assert {"breakout_momentum", "vwap_reversion"} <= set(known_strategies())


def test_registry_unknown_name_fails_clearly():
    config = load_config(env=get_env())
    config.strategy.name = "no_such_strategy"
    with pytest.raises(ValueError, match="unknown strategy"):
        build_strategy(config.strategy)
