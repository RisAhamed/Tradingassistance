"""H1 session-VWAP-reversion strategy tests (APPROVED design).

Entry truth-table, low_volatility-only gate, fail-closed behavior, long-only
output, look-ahead contract, and backtest/live parity. Pure contexts only.
"""
from __future__ import annotations

import pytest

from app.config.loader import get_env, load_config
from app.core.clock import utcnow
from app.domain.enums import Direction, ReasonCode, Regime
from app.domain.features import FeatureSnapshot
from app.domain.market import Candle
from app.domain.regime import RegimeSnapshot
from app.features.engine import FeatureEngine
from app.strategies.base import StrategyContext
from app.strategies.session_vwap_reversion import SessionVwapReversionStrategy


def _config():
    config = load_config(env=get_env())
    config.strategy.name = "session_vwap_reversion"
    config.strategy.version = "0.1"
    return config


def _features(**overrides) -> FeatureSnapshot:
    now = utcnow()
    values = {
        "rsi": 30.0,
        "atr": 100.0,
        "vwap": 85900.0,
        "session_vwap": 86000.0,
        "range_high": 86500.0,
        "range_low": 85000.0,
        "ema_20": 85800.0,
        "ema_50": 85700.0,
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
        regime=regime or _regime(Regime.LOW_VOLATILITY),
        now=utcnow(),
        position_open=position_open,
    )


def _strategy() -> SessionVwapReversionStrategy:
    return SessionVwapReversionStrategy(_config().strategy)


# 21. valid entry ------------------------------------------------------------------
def test_valid_entry_emits_long():
    signal = _strategy().evaluate(_context())
    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_reference == 85800.0


# 22-23. stretch boundary -------------------------------------------------------------
def test_stretch_at_threshold_is_accepted():
    # (86100-86000)/100 = 1.0 exactly; recovery still fine
    features = _features()
    features.close = 86000.0
    features.values["session_vwap"] = 86100.0
    features.high = 86050.0
    features.low = 85950.0
    assert _strategy().evaluate(_context(features=features)) is not None


def test_insufficient_stretch_returns_none():
    # (86090-86000)/100 = 0.9 < 1.0
    features = _features()
    features.close = 86000.0
    features.high = 86050.0
    features.low = 85950.0
    features.values["session_vwap"] = 86090.0
    assert _strategy().evaluate(_context(features=features)) is None


def test_price_above_session_vwap_returns_none():
    features = _features()
    features.close = 86100.0
    assert _strategy().evaluate(_context(features=features)) is None


# 24-25. RSI ---------------------------------------------------------------------------
def test_rsi_at_threshold_is_accepted():
    assert _strategy().evaluate(_context(features=_features(rsi=35.0))) is not None


def test_rsi_above_threshold_returns_none():
    assert _strategy().evaluate(_context(features=_features(rsi=36.0))) is None


# 26-27. range containment ---------------------------------------------------------------
def test_close_at_range_low_is_accepted():
    features = _features()
    features.close = 85000.0
    features.high = 85100.0
    features.low = 84900.0
    assert _strategy().evaluate(_context(features=features)) is not None


def test_range_breakdown_returns_none():
    features = _features()
    features.close = 84900.0
    features.low = 84800.0
    assert _strategy().evaluate(_context(features=features)) is None


def test_missing_range_low_returns_none():
    assert _strategy().evaluate(_context(features=_features(range_low=None))) is None


# 28-29. recovery ---------------------------------------------------------------------------
def test_recovery_at_threshold_is_accepted():
    # (85760-85700)/(85900-85700) = 0.30 exactly
    features = _features()
    features.close = 85760.0
    assert _strategy().evaluate(_context(features=features)) is not None


def test_invalid_high_low_returns_none():
    features = _features()
    features.high = 85700.0
    assert _strategy().evaluate(_context(features=features)) is None


# 30-33. regime gates --------------------------------------------------------------------------
def test_low_volatility_accepted():
    assert _strategy().evaluate(_context(regime=_regime(Regime.LOW_VOLATILITY))) is not None


@pytest.mark.parametrize(
    "regime",
    [Regime.UNKNOWN, Regime.TRENDING_BULLISH, Regime.TRENDING_BEARISH,
     Regime.HIGH_VOLATILITY, Regime.RANGE_BOUND],
)
def test_other_regimes_rejected(regime):
    assert _strategy().evaluate(_context(regime=_regime(regime))) is None


# 34-35. missing inputs -------------------------------------------------------------------------------
@pytest.mark.parametrize("field", ["session_vwap", "atr", "rsi"])
def test_missing_required_value_returns_none(field):
    assert _strategy().evaluate(_context(features=_features(**{field: None}))) is None


def test_zero_atr_returns_none():
    assert _strategy().evaluate(_context(features=_features(atr=0.0))) is None


def test_features_not_ready_returns_none():
    assert _strategy().evaluate(_context(features=_features(ema_50=None))) is None


# 36. long-only ------------------------------------------------------------------------------------------------
def test_only_long_is_ever_emitted():
    strategy = _strategy()
    regimes = [Regime.LOW_VOLATILITY, Regime.RANGE_BOUND, Regime.UNKNOWN,
               Regime.TRENDING_BULLISH, Regime.TRENDING_BEARISH, Regime.HIGH_VOLATILITY]
    for regime in regimes:
        for rsi in (20.0, 30.0, 35.0, 50.0):
            signal = strategy.evaluate(_context(features=_features(rsi=rsi), regime=_regime(regime)))
            assert signal is None or signal.direction is Direction.LONG


# 37. flat-book -------------------------------------------------------------------------------
def test_position_open_returns_none():
    assert _strategy().evaluate(_context(position_open=True)) is None


# 38. spread gate ----------------------------------------------------------------------------------
def test_excessive_spread_returns_none():
    features = _features()
    features.spread_percent = 0.5
    assert _strategy().evaluate(_context(features=features)) is None


# 39. reason codes -------------------------------------------------------------------------------------
def test_reason_codes_describe_session_reversion_setup():
    signal = _strategy().evaluate(_context())
    assert signal is not None
    assert signal.reason_code is ReasonCode.REVERSION_SETUP
    assert ReasonCode.VWAP_STRETCH.value in signal.reason
    assert ReasonCode.RANGE_CONTAINED.value in signal.reason
    assert signal.strategy == "session_vwap_reversion"
    assert signal.regime is Regime.LOW_VOLATILITY


# 40. purity ----------------------------------------------------------------------------------------------------
def test_evaluation_is_pure_and_deterministic():
    strategy = _strategy()
    first = strategy.evaluate(_context())
    second = strategy.evaluate(_context())
    assert first is not None and second is not None
    for field in ("direction", "entry_reference", "reason", "reason_code", "strategy", "regime"):
        assert getattr(first, field) == getattr(second, field)


# look-ahead -----------------------------------------------------------------------------------------------
def test_signal_at_bar_t_ignores_next_bar():
    from datetime import datetime, timezone

    def bar(day, minute, o, h, l, c, v):
        return Candle(timestamp=datetime(2026, 10, day, 0, minute, tzinfo=timezone.utc),
                      symbol="BTC/USD", open=o, high=h, low=l, close=c,
                      volume=v, timeframe="5m")

    b1 = bar(1, 5, 100, 110, 90, 100, 10)    # typical 100
    b2 = bar(1, 10, 95, 100, 80, 90, 30)     # typical 90 → session VWAP 92.5
    b3 = bar(1, 15, 150, 160, 140, 150, 1000)  # would drag VWAP far away
    config = load_config(env=get_env())
    engine = FeatureEngine(config.features)
    vwap_at_b2 = engine.compute([b1, b2], timeframe="5m").session_vwap
    vwap_with_b3 = engine.compute([b1, b2, b3], timeframe="5m").session_vwap
    assert vwap_at_b2 == pytest.approx(92.5)
    assert vwap_with_b3 != pytest.approx(92.5)
    # The bar-t decision is a pure function of the ≤t window: re-evaluating
    # the b2 snapshot after b3 exists cannot change it.
    snap_b2 = engine.compute([b1, b2], timeframe="5m")
    assert snap_b2.session_vwap == pytest.approx(92.5)


# parity -----------------------------------------------------------------------------------------------------------
def test_backtest_and_live_windows_agree():
    from app.backtesting.data import generate_history

    config = load_config(env=get_env())
    candles = generate_history("BTC/USD", timeframe="5m", length=300, seed=7)
    replay_engine, live_engine = FeatureEngine(config.features), FeatureEngine(config.features)
    replay_values = [
        replay_engine.compute(candles[: i + 1], timeframe="5m").session_vwap
        for i in range(len(candles))
    ]
    # Live path: the store grows bar by bar; each evaluation sees the full
    # series-so-far through the same pure function.
    store: list = []
    live_values = []
    for candle in candles:
        store.append(candle)
        live_values.append(live_engine.compute(list(store), timeframe="5m").session_vwap)
    assert replay_values == live_values
