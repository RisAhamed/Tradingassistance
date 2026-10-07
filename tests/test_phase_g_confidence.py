"""Phase G: deterministic signal confidence + conviction sizing.

Conviction only ever DOWN-scales the risk_percent ceiling. No entry gating
on confidence exists in this phase (explicitly deferred).
"""
from __future__ import annotations

import asyncio
import logging
import math

import pytest

from app.ai_supervisor.tools import build_gateway
from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.config.models import MarketDataConfig, PositionSizingConfig
from app.core.clock import utcnow
from app.core.logging import configure_logging, reset_logging_state
from app.decision.trade_plan import TradePlanBuilder
from app.domain.enums import Direction, Regime
from app.domain.regime import RegimeSnapshot
from app.domain.signals import StrategySignal
from app.portfolio.position_sizing import PositionSizer
from app.runtime import build_runtime
from app.strategies.breakout_momentum import BreakoutMomentumStrategy
from app.strategies.range_edge_rejection import RangeEdgeRejectionStrategy
from app.strategies.session_vwap_reversion import SessionVwapReversionStrategy
from app.strategies.vwap_reversion import VwapReversionStrategy
from tests.support import SYMBOL, make_features, make_regime, make_signal

CONF = {"enabled": True, "min_multiplier": 0.25, "max_multiplier": 1.0}


def _sizer(**overrides):
    cfg = PositionSizingConfig.model_validate(
        {"method": "risk_based", "confidence": {**CONF, **overrides}}
    )
    return PositionSizer(cfg, risk_percent=0.5)


def _ctx(strategy, features, regime):
    from app.strategies.base import StrategyContext

    return StrategyContext(
        symbol=SYMBOL, timeframe="5m", features=features, regime=regime,
        now=utcnow(), position_open=False,
    )


# --- 1. validator ------------------------------------------------------------
def test_confidence_out_of_bounds_rejected():
    base = dict(signal_id="s", timestamp=utcnow(), symbol=SYMBOL, strategy="x",
                direction=Direction.LONG, reason_code="breakout_above_range",
                reason="t", entry_reference=100.0)
    for bad in (1.1, -0.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            StrategySignal(**{**base, "confidence": bad})
    with pytest.raises(ValueError):
        StrategySignal(**{**base, "confidence_components": {"m": float("nan")}})
    # empty components are allowed
    s = StrategySignal(**base)
    assert s.confidence == 0.0 and s.confidence_components == {}


# --- 2/3. per-strategy confidence --------------------------------------------
def _breakout_values(**kw):
    vals = {
        "rsi": 65.0, "atr": 1.0, "vwap": 99.0, "range_high": 100.0,
        "range_low": 90.0, "ema_20": 110.0, "ema_50": 100.0,
    }
    vals.update(kw)
    return vals


def test_breakout_strong_is_high_and_pure():
    cfg = load_config(env=get_env()).strategy
    strat = BreakoutMomentumStrategy(cfg)
    ctx = lambda **kw: _ctx(strat, make_features(_breakout_values(**kw), close=kw.pop("close", 101.0), spread_percent=0.02), make_regime(Regime.TRENDING_BULLISH))
    s1 = strat.evaluate(ctx())
    s2 = strat.evaluate(ctx())
    assert s1 is not None and s1.confidence > 0.6
    assert s1.confidence == s2.confidence
    assert s1.confidence_components == s2.confidence_components
    assert set(s1.confidence_components) >= {
        "breakout_margin", "momentum_margin", "trend_margin", "spread_margin"}


def test_breakout_barely_qualifying_is_low():
    cfg = load_config(env=get_env()).strategy
    strat = BreakoutMomentumStrategy(cfg)
    feats = make_features(
        _breakout_values(rsi=55.5, ema_20=100.1, ema_50=100.0),
        close=100.06, spread_percent=0.09,
    )
    sig = strat.evaluate(_ctx(strat, feats, make_regime(Regime.TRENDING_BULLISH)))
    assert sig is not None
    assert sig.confidence < 0.3


def _reversion_values(**kw):
    vals = {"rsi": 20.0, "atr": 2.0, "vwap": 100.0, "range_high": 101.0,
            "range_low": 93.0, "ema_20": 95.0, "ema_50": 96.0,
            "volume_ratio": 1.0}
    vals.update(kw)
    return vals


def _snap(*, close, high, low, values, spread_percent=0.02):
    from app.domain.features import FeatureSnapshot

    return FeatureSnapshot(
        symbol=SYMBOL, timestamp=utcnow(), timeframe="5m",
        close=close, high=high, low=low,
        spread_percent=spread_percent, candle_count=60, values=values,
    )


def test_vwap_reversion_strong_and_weak():
    cfg = load_config(env=get_env()).strategy
    strat = VwapReversionStrategy(cfg)
    strong = _snap(close=94.0, high=96.0, low=90.0, values=_reversion_values())
    sig = strat.evaluate(_ctx(strat, strong, make_regime(Regime.RANGE_BOUND)))
    assert sig is not None and sig.confidence > 0.6
    weak = _snap(close=97.9, high=99.0, low=90.0,
                 values=_reversion_values(rsi=34.8))
    sig2 = strat.evaluate(_ctx(strat, weak, make_regime(Regime.RANGE_BOUND)))
    assert sig2 is not None and sig2.confidence < 0.3
    assert {"stretch_margin", "rsi_margin", "recovery_margin"} <= set(sig.confidence_components)


def test_session_vwap_reversion_confidence():
    cfg = load_config(env=get_env()).strategy
    strat = SessionVwapReversionStrategy(cfg)
    feats = _snap(close=94.0, high=96.0, low=90.0,
                  values=_reversion_values(session_vwap=100.0))
    sig = strat.evaluate(_ctx(strat, feats, make_regime(Regime.LOW_VOLATILITY)))
    assert sig is not None and sig.confidence > 0.6


def test_range_edge_reversion_confidence():
    cfg = load_config(env=get_env()).strategy
    strat = RangeEdgeRejectionStrategy(cfg)
    vals = {"rsi": 20.0, "atr": 2.0, "vwap": 96.0, "range_high": 101.0,
            "range_low": 95.0, "ema_20": 95.0, "ema_50": 96.0}
    feats = _snap(close=98.0, high=99.0, low=93.0, values=vals)
    sig = strat.evaluate(_ctx(strat, feats, make_regime(Regime.LOW_VOLATILITY)))
    assert sig is not None and sig.confidence > 0.6
    weak = _snap(close=96.0, high=98.0, low=94.9, values={**vals, "rsi": 34.0})
    sig2 = strat.evaluate(_ctx(strat, weak, make_regime(Regime.LOW_VOLATILITY)))
    assert sig2 is not None and sig2.confidence < 0.3


# --- 4. sizer math -------------------------------------------------------------
def test_sizer_multiplier_endpoints_and_linearity():
    sizer = _sizer()
    eq = 100000.0
    for conf, mult in ((0.0, 0.25), (1.0, 1.0), (0.5, 0.625)):
        sig = make_signal()
        sig = sig.model_copy(update={"confidence": conf})
        r = sizer.size(sig, eq, reference_price=30000.0, max_notional=20000.0)
        assert r.ok
        assert r.confidence_multiplier == pytest.approx(mult, abs=1e-9)
        assert r.effective_risk_percent == pytest.approx(0.5 * mult, abs=1e-12)


def test_sizer_never_exceeds_ceiling_and_never_negative():
    for eq in (1.0, 100.0, 100000.0, 1e9):
        for conf in (0.0, 0.13, 0.5, 0.77, 1.0):
            sizer = _sizer()
            sig = make_signal()
            sig = sig.model_copy(update={"confidence": conf})
            r = sizer.size(sig, eq, reference_price=30000.0, max_notional=eq * 0.2)
            assert r.risk_budget <= eq * 0.5 / 100.0 + 1e-9
            assert r.risk_budget >= 0.0


def test_sizer_disabled_reproduces_pre_g_result():
    sig = make_signal()
    sig = sig.model_copy(update={"confidence": 0.0})
    on = _sizer().size(sig, 100000.0, reference_price=30000.0, max_notional=20000.0)
    off_cfg = PositionSizingConfig.model_validate(
        {"method": "risk_based", "confidence": {"enabled": False}})
    off = PositionSizer(off_cfg, risk_percent=0.5).size(
        sig, 100000.0, reference_price=30000.0, max_notional=20000.0)
    legacy_cfg = PositionSizingConfig()
    legacy = PositionSizer(legacy_cfg, risk_percent=0.5).size(
        sig, 100000.0, reference_price=30000.0, max_notional=20000.0)
    # default model has confidence enabled → reduced budget
    assert on.risk_budget == pytest.approx(125.0)           # 500 * 0.25
    # disabled reproduces the old full-budget result exactly
    assert off.final_quantity == pytest.approx(0.666666)
    assert off.risk_budget == pytest.approx(500.0)
    # default model has confidence enabled, so it must differ (reduced budget)
    assert legacy.final_quantity == pytest.approx(0.25)
    assert off.effective_risk_percent == 0.5


# --- 5. TradePlan persistence --------------------------------------------------
@pytest.mark.asyncio
async def test_trade_plan_confidence_persists_and_reloads(tmp_path):
    from app.config.loader import PROJECT_ROOT
    from app.storage.db import Database
    from app.storage.repository import Repository

    env = get_env()
    config = load_config(env=env)
    config.storage.enabled = True
    config.storage.sqlite_path = str(tmp_path / "g.sqlite3")
    db = Database(config.storage, project_root=PROJECT_ROOT)
    repo = Repository(db.session_factory, database=db)
    await repo.init()
    builder = TradePlanBuilder(config.trade_plan)
    sig = make_signal()
    sig = sig.model_copy(update={"confidence": 0.75,
                                  "confidence_components": {"m": 0.5}})
    sizing = PositionSizer(config.position_sizing, risk_percent=0.5).size(
        sig, 100000.0, reference_price=30000.0, max_notional=20000.0)
    res = builder.build_from_signal(
        sig, regime=make_regime(), features=make_features(),
        sizing=sizing, risk_percent=0.5, maximum_notional=20000.0)
    await repo.save_trade_plan(res.plan.model_dump(mode="json"), session_id="s")
    latest = await repo.load_latest_trade_plan()
    assert latest["confidence"] == 0.75
    assert latest["confidence_components"] == {"m": 0.5}
    await db.dispose()


# --- 6. backtest parity ----------------------------------------------------------
def test_backtest_and_live_sizing_agree():
    from app.backtesting.engine import BacktestEngine  # noqa: F401  (shape parity)

    config = load_config(env=get_env())
    live_sizer = PositionSizer(config.position_sizing, risk_percent=config.risk.risk_per_trade_percent)
    bt_sizer = PositionSizer(config.position_sizing)
    sig = make_signal()
    sig = sig.model_copy(update={"confidence": 0.62})
    a = live_sizer.size(sig, 100000.0, reference_price=30000.0, max_notional=20000.0)
    b = bt_sizer.size(sig, 100000.0, reference_price=30000.0, max_notional=20000.0)
    assert a.final_quantity == b.final_quantity


# --- 7/8. end-to-end --------------------------------------------------------------
@pytest.mark.asyncio
async def test_mock_pipeline_signals_carry_confidence_and_zero_orders():
    reset_logging_state()
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.execution.enabled = False
    config.market_data.max_future_skew_seconds = 86400 * 30
    configure_logging(config, env, project_root=PROJECT_ROOT)
    runtime = build_runtime(config, env)
    await runtime.engine.start()
    try:
        provider = runtime.engine.provider
        n = 0
        while not provider.exhausted and n < 700:
            await provider.pump_one()
            n += 1
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        signals = list(runtime.engine.state.recent_signals)
        assert signals, "mock pipeline must emit at least one signal"
        assert any(s.confidence > 0 for s in signals)
        # Trace + sizing-log path: drive the pipeline's last signal through
        # the entry funnel (execution stays disabled) and verify conviction
        # fields appear in both the trace and the structured log line.
        # NOTE: configure_logging() clears root handlers, so caplog cannot be
        # used here — attach a dedicated handler to the engine logger instead.
        sized_records: list = []

        class _Capture(logging.Handler):
            def emit(self, record):
                sized_records.append(record)

        capture = _Capture()
        logging.getLogger("app.runner.engine").addHandler(capture)
        try:
            await runtime.engine._submit_entry(signals[-1])
        finally:
            logging.getLogger("app.runner.engine").removeHandler(capture)
        sized_logs = [r for r in sized_records
                      if getattr(r, "structured", {}).get("event") == "POSITION_SIZED"]
        assert sized_logs, "sizing must have run at least once across the run"
        assert "confidence" in sized_logs[-1].structured
        assert "confidence_multiplier" in sized_logs[-1].structured
        assert "effective_risk_percent" in sized_logs[-1].structured
        # Trace path: drive the last signal through the entry funnel and
        # verify the POSITION_SIZE trace carries conviction fields.
        sized_trace = [e for e in runtime.engine.decision_trace()
                       if e["stage"] == "POSITION_SIZE"]
        assert sized_trace, "POSITION_SIZE trace must be emitted"
        out = sized_trace[-1]["output"]
        assert {"confidence", "confidence_multiplier", "effective_risk_percent"} <= set(out)
        assert runtime.engine.oms.all_orders() == []
        assert runtime.engine.position_manager.is_flat
    finally:
        await runtime.engine.stop()
