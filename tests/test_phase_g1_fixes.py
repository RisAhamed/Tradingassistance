"""G.1: config-sourced trend threshold + reproducible synthetic history."""
from __future__ import annotations

from datetime import datetime, timezone

from app.backtesting.data import generate_history
from app.config.loader import get_env, load_config
from app.domain.enums import Regime
from app.strategies.base import StrategyContext
from app.strategies.breakout_momentum import BreakoutMomentumStrategy
from tests.support import SYMBOL, make_features, make_regime


def _ctx(**kw):
    from app.core.clock import utcnow

    base = dict(symbol=SYMBOL, timeframe="5m",
               features=make_features(), regime=make_regime(),
               now=utcnow(), position_open=False)
    base.update(kw)
    return StrategyContext(**base)


def test_trend_margin_uses_configured_threshold_050():
    cfg = load_config(env=get_env()).strategy
    strat = BreakoutMomentumStrategy(cfg)
    ctx = _ctx(ema_distance_atr_min=0.50)
    margin_050 = strat._confidence(ctx, __import__("app.domain.enums", fromlist=["Direction"]).Direction.LONG, 101.0)[0]
    ctx2 = _ctx(ema_distance_atr_min=0.25)
    margin_025 = strat._confidence(ctx2, __import__("app.domain.enums", fromlist=["Direction"]).Direction.LONG, 101.0)[0]
    # Larger reference threshold must shrink the trend contribution.
    assert margin_050 < margin_025


def test_trend_margin_scales_with_010():
    from app.domain.enums import Direction

    cfg = load_config(env=get_env()).strategy
    strat = BreakoutMomentumStrategy(cfg)
    lo = strat._confidence(_ctx(ema_distance_atr_min=0.10), Direction.LONG, 101.0)[1]["trend_margin"]
    hi = strat._confidence(_ctx(ema_distance_atr_min=0.50), Direction.LONG, 101.0)[1]["trend_margin"]
    assert lo > hi
    assert lo == 1.0  # |110-100|/50/0.10 saturates


def test_reference_start_is_byte_identical():
    a = generate_history("BTC/USD", timeframe="1m", length=200, seed=7,
                         start=datetime(2024, 1, 1, tzinfo=timezone.utc))
    b = generate_history("BTC/USD", timeframe="1m", length=200, seed=7,
                         start=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert len(a) == len(b) > 0
    for x, y in zip(a, b):
        assert x.timestamp == y.timestamp
        assert (x.open, x.high, x.low, x.close, x.volume) == (y.open, y.high, y.low, y.close, y.volume)


def test_reference_start_null_keeps_utcnow_anchor():
    from app.backtesting import data as data_mod

    real_utcnow = data_mod.utcnow
    data_mod.utcnow = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    try:
        early = generate_history("BTC/USD", timeframe="1m", length=5, seed=7)
    finally:
        data_mod.utcnow = real_utcnow
    data_mod.utcnow = lambda: datetime(2026, 6, 1, tzinfo=timezone.utc)
    try:
        late = generate_history("BTC/USD", timeframe="1m", length=5, seed=7)
    finally:
        data_mod.utcnow = real_utcnow
    assert early[0].timestamp != late[0].timestamp
    assert early[0].timestamp < late[0].timestamp
