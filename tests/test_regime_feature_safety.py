"""Phase A #1 (regime safety) & #2 (feature safety) regression tests.

Invariant: an UNKNOWN regime, or a missing/stale/insufficient feature set, can
never produce a signal — and in particular can never claim ``bullish_regime``,
``bearish_regime`` or ``trend_confirmed``.
"""
from app.config.models import RegimeConfig, StrategyConfig
from app.domain.enums import ReasonCode, Regime
from app.regime.engine import RegimeEngine
from app.strategies.base import StrategyContext
from app.strategies.breakout_momentum import BreakoutMomentumStrategy
from tests.support import NOW, SYMBOL, make_features, make_regime


def _strategy() -> BreakoutMomentumStrategy:
    return BreakoutMomentumStrategy(StrategyConfig())


def _ctx(features, regime) -> StrategyContext:
    return StrategyContext(
        symbol=SYMBOL,
        timeframe="5m",
        features=features,
        regime=regime,
        now=NOW,
        position_open=False,
    )


# --- #1 regime safety -------------------------------------------------------
def test_unknown_regime_blocks_breakout_signal():
    """A textbook long breakout must WAIT when the regime is UNKNOWN."""
    features = make_features()
    assert _strategy().evaluate(_ctx(features, make_regime(Regime.UNKNOWN))) is None


def test_unknown_regime_never_claims_confirmed_regime():
    """Regression: UNKNOWN previously still emitted bullish_regime/trend_confirmed."""
    strategy = _strategy()
    for features in (
        make_features(),
        make_features(values={"rsi": 70.0}),
        make_features(spread_percent=None),
    ):
        assert strategy.evaluate(_ctx(features, make_regime(Regime.UNKNOWN))) is None


def test_unknown_regime_blocks_short_signal():
    features = make_features(close=80.0, values={"rsi": 35.0, "ema_20": 95.0, "ema_50": 100.0, "vwap": 101.0})
    assert _strategy().evaluate(_ctx(features, make_regime(Regime.UNKNOWN))) is None


def test_range_bound_regime_does_not_claim_bullish_reason():
    """A non-trending regime may trade a breakout but must not fabricate a trend claim."""
    signal = _strategy().evaluate(_ctx(make_features(), make_regime(Regime.RANGE_BOUND)))
    assert signal is not None
    assert ReasonCode.BULLISH_REGIME.value not in signal.reason
    assert signal.regime is Regime.RANGE_BOUND


def test_insufficient_candles_classify_as_unknown():
    engine = RegimeEngine(RegimeConfig(), fast_period=20, slow_period=50)
    snapshot = engine.classify(make_features(candle_count=5), now=NOW)
    assert snapshot.is_unknown
    assert "insufficient_candles" in snapshot.reason


def test_regime_missing_indicators_classify_as_unknown():
    engine = RegimeEngine(RegimeConfig(), fast_period=20, slow_period=50)
    snapshot = engine.classify(make_features(values={"ema_50": None}), now=NOW)
    assert snapshot.is_unknown


# --- #2 feature safety ------------------------------------------------------
def test_ready_requires_every_required_feature():
    for name in ("rsi", "atr", "vwap", "ema_20", "ema_50", "range_high", "range_low"):
        features = make_features(values={name: None})
        assert not features.ready, f"{name} should deactivate readiness"
        assert name in features.missing
    assert make_features().ready


def test_strategy_waits_when_required_feature_missing():
    strategy = _strategy()
    for name in ("rsi", "atr", "vwap", "ema_20", "ema_50", "range_high", "range_low"):
        features = make_features(values={name: None})
        assert strategy.evaluate(_ctx(features, make_regime(Regime.TRENDING_BULLISH))) is None


def test_strategy_rejects_nonpositive_or_nonfinite_atr():
    strategy = _strategy()
    for atr in (0.0, -1.0, float("nan"), float("inf")):
        features = make_features(values={"atr": atr})
        assert not features.ready
        assert strategy.evaluate(_ctx(features, make_regime(Regime.TRENDING_BULLISH))) is None


def test_missing_spread_is_not_marked_acceptable():
    """A missing spread must not be fabricated into SPREAD_ACCEPTABLE."""
    signal = _strategy().evaluate(_ctx(make_features(spread_percent=None), make_regime(Regime.TRENDING_BULLISH)))
    assert signal is not None
    assert ReasonCode.SPREAD_ACCEPTABLE.value not in signal.reason
