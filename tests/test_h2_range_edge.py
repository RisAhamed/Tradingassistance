"""H2 range-edge-rejection strategy tests (APPROVED design v0.1).

Boundary truth-table, close/recovery/RSI/regime gates, fail-closed purity,
range causality (current bar excluded), look-ahead contract, and
backtest/live parity. Pure StrategyContext in, signal-or-None out.
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
from app.strategies.range_edge_rejection import RangeEdgeRejectionStrategy


def _config():
    config = load_config(env=get_env())
    config.strategy.name = "range_edge_rejection"
    config.strategy.version = "0.1"
    return config


def _features(**overrides) -> FeatureSnapshot:
    now = utcnow()
    values = {
        "rsi": 30.0,
        "atr": 100.0,
        "vwap": 85900.0,
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
        close=85100.0,
        high=85200.0,
        low=84900.0,
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


def _strategy() -> RangeEdgeRejectionStrategy:
    return RangeEdgeRejectionStrategy(_config().strategy)


# --- valid setup: low 84900 <= range_low 85000, close 85100 >= 85000,
# --- recovery (85100-84900)/(85200-84900) = 0.667, rsi 30, low_vol ------
def test_valid_rejection_emits_long():
    signal = _strategy().evaluate(_context())
    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_reference == 85100.0


# --- boundary ---------------------------------------------------------------
def test_exact_touch_is_accepted():
    features = _features()
    features.low = 85000.0
    features.close = 85050.0
    features.high = 85100.0
    assert _strategy().evaluate(_context(features=features)) is not None


def test_penetration_is_accepted():
    assert _strategy().evaluate(_context()) is not None  # low 84900 < 85000


def test_no_touch_returns_none():
    features = _features()
    features.low = 85001.0
    assert _strategy().evaluate(_context(features=features)) is None


# --- close -------------------------------------------------------------------
def test_exact_close_back_inside_is_accepted():
    features = _features()
    features.close = 85000.0
    features.high = 85200.0
    features.low = 84900.0
    # recovery (85000-84900)/(85200-84900) = 0.333 >= 0.30
    assert _strategy().evaluate(_context(features=features)) is not None


def test_close_above_boundary_is_accepted():
    assert _strategy().evaluate(_context()) is not None


def test_close_below_boundary_returns_none():
    features = _features()
    features.close = 84950.0
    features.high = 85050.0
    assert _strategy().evaluate(_context(features=features)) is None


# --- recovery -----------------------------------------------------------------
def test_recovery_at_threshold_is_accepted():
    # (85090-85000)/(85300-85000) = 0.30 exactly; low touches boundary
    features = _features()
    features.close = 85090.0
    features.high = 85300.0
    features.low = 85000.0
    assert _strategy().evaluate(_context(features=features)) is not None


def test_recovery_below_threshold_returns_none():
    # (85050-85000)/(85300-85000) = 0.167 < 0.30
    features = _features()
    features.close = 85050.0
    features.high = 85300.0
    features.low = 85000.0
    assert _strategy().evaluate(_context(features=features)) is None


def test_recovery_above_threshold_is_accepted():
    assert _strategy().evaluate(_context()) is not None  # 0.667


def test_flat_bar_returns_none():
    features = _features()
    features.high = 85000.0
    features.low = 85000.0
    assert _strategy().evaluate(_context(features=features)) is None


# --- RSI -----------------------------------------------------------------------
def test_rsi_at_threshold_is_accepted():
    assert _strategy().evaluate(_context(features=_features(rsi=35.0))) is not None


def test_rsi_above_threshold_returns_none():
    assert _strategy().evaluate(_context(features=_features(rsi=36.0))) is None


# --- regime ----------------------------------------------------------------------
def test_low_volatility_accepted():
    assert _strategy().evaluate(_context(regime=_regime(Regime.LOW_VOLATILITY))) is not None


@pytest.mark.parametrize(
    "regime",
    [Regime.RANGE_BOUND, Regime.TRENDING_BULLISH, Regime.TRENDING_BEARISH,
     Regime.HIGH_VOLATILITY, Regime.UNKNOWN],
)
def test_other_regimes_rejected(regime):
    assert _strategy().evaluate(_context(regime=_regime(regime))) is None


# --- gates --------------------------------------------------------------------------
def test_not_ready_returns_none():
    assert _strategy().evaluate(_context(features=_features(ema_50=None))) is None


def test_position_open_returns_none():
    assert _strategy().evaluate(_context(position_open=True)) is None


def test_excessive_spread_returns_none():
    features = _features()
    features.spread_percent = 0.5
    assert _strategy().evaluate(_context(features=features)) is None


@pytest.mark.parametrize("field", ["range_low", "atr", "rsi", "high", "low", "close"])
def test_missing_required_value_returns_none(field):
    features = _features(**{field: None}) if field not in ("high", "low", "close") else None
    if features is None:
        features = _features()
        setattr(features, field, None)
    assert _strategy().evaluate(_context(features=features)) is None


def test_zero_atr_returns_none():
    assert _strategy().evaluate(_context(features=_features(atr=0.0))) is None


# --- long-only ----------------------------------------------------------------------------
def test_only_long_is_ever_emitted():
    strategy = _strategy()
    regimes = [Regime.LOW_VOLATILITY, Regime.RANGE_BOUND, Regime.UNKNOWN,
               Regime.TRENDING_BULLISH, Regime.TRENDING_BEARISH, Regime.HIGH_VOLATILITY]
    for regime in regimes:
        for rsi in (20.0, 30.0, 35.0, 50.0):
            signal = strategy.evaluate(_context(features=_features(rsi=rsi), regime=_regime(regime)))
            assert signal is None or signal.direction is Direction.LONG


# --- reason codes ----------------------------------------------------------------------------------
def test_reason_codes_describe_edge_rejection():
    signal = _strategy().evaluate(_context())
    assert signal is not None
    assert signal.reason_code is ReasonCode.EDGE_REJECTION
    assert ReasonCode.RANGE_EDGE_TOUCH.value in signal.reason
    assert ReasonCode.RANGE_CONTAINED.value in signal.reason
    assert signal.strategy == "range_edge_rejection"
    assert signal.regime is Regime.LOW_VOLATILITY


# --- purity -------------------------------------------------------------------------------------------------
def test_evaluation_is_pure_and_deterministic():
    strategy = _strategy()
    first = strategy.evaluate(_context())
    second = strategy.evaluate(_context())
    assert first is not None and second is not None
    for field in ("direction", "entry_reference", "reason", "reason_code", "strategy", "regime"):
        assert getattr(first, field) == getattr(second, field)


# --- range causality: current bar excluded ----------------------------------------------------------------------
def test_range_boundary_excludes_current_bar():
    from datetime import datetime, timedelta, timezone

    def bar(day, minute, h, l, c, v=5.0):
        base = datetime(2026, 10, day, tzinfo=timezone.utc)
        return Candle(timestamp=base + timedelta(minutes=minute),
                      symbol="BTC/USD", open=c, high=h, low=l, close=c,
                      volume=v, timeframe="5m")

    history = [bar(1, 5 * i, 100, 90, 95) for i in range(1, 21)]
    config = load_config(env=get_env())
    engine = FeatureEngine(config.features)
    base_low = engine.compute(history, timeframe="5m").range_low
    assert base_low == pytest.approx(90.0)
    # Appending a bar whose low undercuts everything must NOT move the
    # boundary used for its own signal: range_low stays 90.0.
    with_dip = history + [bar(1, 105, 96, 80, 94)]
    assert engine.compute(with_dip, timeframe="5m").range_low == pytest.approx(90.0)


# --- look-ahead: t+1 cannot change the bar-t decision ------------------------------------------------------------------
def test_next_bar_cannot_change_current_decision():
    from datetime import datetime, timedelta, timezone

    def bar(day, minute, h, l, c, v=5.0):
        base = datetime(2026, 10, day, tzinfo=timezone.utc)
        return Candle(timestamp=base + timedelta(minutes=minute),
                      symbol="BTC/USD", open=c, high=h, low=l, close=c,
                      volume=v, timeframe="5m")

    history = [bar(1, 5 * i, 100, 90, 95) for i in range(1, 21)]
    # Bar t: tests the 90.0 boundary, closes back inside with recovery.
    bar_t = bar(1, 105, 92, 89, 91.5)
    config = load_config(env=get_env())
    engine = FeatureEngine(config.features)
    snap_t = engine.compute(history + [bar_t], timeframe="5m")
    assert snap_t.range_low == pytest.approx(90.0)
    # A violent t+1 (range collapse) is unavailable at t and must not
    # retroactively alter the recorded bar-t snapshot values.
    bar_next = bar(1, 110, 91, 50, 55, v=500.0)
    snap_t2 = engine.compute(history + [bar_t, bar_next], timeframe="5m")
    # t+1's range correctly includes bar_t as a PRIOR bar (89.0) while
    # excluding t+1 itself — causality in both directions.
    assert snap_t2.range_low == pytest.approx(89.0)
    assert snap_t.range_low == pytest.approx(90.0)
    assert snap_t.close == pytest.approx(91.5)


# --- parity: prefix replay vs growing live-style windows ------------------------------------------------------------------
def test_backtest_and_live_windows_agree():
    from app.backtesting.data import generate_history

    config = load_config(env=get_env())
    candles = generate_history("BTC/USD", timeframe="5m", length=300, seed=7)
    replay_engine, live_engine = FeatureEngine(config.features), FeatureEngine(config.features)
    strategy = RangeEdgeRejectionStrategy(config.strategy)
    for i in range(20, len(candles)):
        replay_snap = replay_engine.compute(candles[: i + 1], timeframe="5m")
        live_snap = live_engine.compute(list(candles[: i + 1]), timeframe="5m")
        assert replay_snap.range_low == live_snap.range_low
        assert replay_snap.close == live_snap.close
        ctx_kwargs = dict(symbol="BTC/USD", timeframe="5m", now=candles[i].timestamp)
        r1 = strategy.evaluate(StrategyContext(features=replay_snap, regime=_regime(Regime.LOW_VOLATILITY), **ctx_kwargs))
        r2 = strategy.evaluate(StrategyContext(features=live_snap, regime=_regime(Regime.LOW_VOLATILITY), **ctx_kwargs))
        assert (r1 is None) == (r2 is None)
        if r1 is not None:
            assert r1.direction == r2.direction == Direction.LONG
            assert r1.entry_reference == r2.entry_reference
