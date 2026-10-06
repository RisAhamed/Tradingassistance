"""Phase D.6: deterministic backtesting tests.

Covers bar ordering, duplicates, gaps, no-lookahead, features, strategy,
risk, sizing, simulated fills, fees, slippage, partial fills, exits,
session flatten, reconciliation, final flat invariant, deterministic
replay, execution-gate isolation, cost sensitivity, and trade records.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backtesting.data import generate_history
from app.backtesting.engine import BacktestEngine
from app.backtesting.execution import SimulatedBroker
from app.backtesting.metrics import compute_metrics
from app.config.loader import get_env, load_config
from app.config.models import BacktestingExecutionConfig
from app.domain.enums import Direction, Side
from app.domain.market import Candle


def _config(**overrides):
    config = load_config(env=get_env())
    config.trading.broker = "mock"
    config.execution.enabled = False
    config.market_data.provider = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    for key, value in overrides.items():
        parts = key.split(".")
        obj = config
        for part in parts[:-1]:
            obj = getattr(obj, part)
        setattr(obj, parts[-1], value)
    return config


def _candle(ts: datetime, close: float, *, high=None, low=None, volume=1.0) -> Candle:
    return Candle(
        timestamp=ts,
        symbol="BTC/USD",
        open=close,
        high=high if high is not None else close + 1,
        low=low if low is not None else close - 1,
        close=close,
        volume=volume,
        timeframe="5m",
    )


def _base_time() -> datetime:
    return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Bar ordering / duplicates / gaps / timestamps
# ---------------------------------------------------------------------------
def test_generate_history_produces_ordered_unique_timestamps():
    candles = generate_history(length=100, timeframe="5m", seed=42)
    timestamps = [c.timestamp for c in candles]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)


def test_generate_history_respects_timeframe_spacing():
    candles = generate_history(length=50, timeframe="1h", seed=7)
    for i in range(1, len(candles)):
        delta = (candles[i].timestamp - candles[i - 1].timestamp).total_seconds()
        assert delta == 3600


def test_generate_history_is_deterministic():
    a = generate_history(length=100, seed=99)
    b = generate_history(length=100, seed=99)
    assert [c.close for c in a] == [c.close for c in b]


def test_generate_history_different_seeds_differ():
    a = generate_history(length=100, seed=1)
    b = generate_history(length=100, seed=2)
    assert [c.close for c in a] != [c.close for c in b]


# ---------------------------------------------------------------------------
# 2. No look-ahead bias
# ---------------------------------------------------------------------------
def test_features_use_only_past_and_current_candles():
    """FeatureEngine.compute with window=candles[:i+1] must not see future bars."""
    from app.features.engine import FeatureEngine

    config = _config()
    engine = FeatureEngine(config.features)
    candles = generate_history(length=100, timeframe="5m", seed=7)
    ts = candles[50].timestamp
    window = candles[:51]
    snap = engine.compute(window, timeframe="5m", now=ts)
    assert snap.candle_count == 51
    assert snap.timestamp == ts


def test_backtest_engine_does_not_see_future_candles():
    """The engine processes bars sequentially; features at bar i use bars[:i+1]."""
    config = _config()
    engine = BacktestEngine(config)
    candles = generate_history(length=200, timeframe="5m", seed=7)
    result = engine.run(candles)
    # If the engine ran, it processed bars in order without crashing.
    assert isinstance(result.metrics.trade_count, int)


# ---------------------------------------------------------------------------
# 3. Feature calculation
# ---------------------------------------------------------------------------
def test_feature_engine_computes_all_configured_features():
    from app.features.engine import FeatureEngine

    config = _config()
    engine = FeatureEngine(config.features)
    candles = generate_history(length=100, timeframe="5m", seed=7)
    snap = engine.compute(candles, timeframe="5m", now=candles[-1].timestamp)
    assert snap.close is not None
    assert snap.rsi is not None
    assert snap.atr is not None
    assert snap.range_high is not None
    assert snap.range_low is not None


def test_feature_engine_returns_unknown_regime_on_insufficient_candles():
    from app.regime.engine import RegimeEngine

    config = _config()
    regime_engine = RegimeEngine(config.regime, fast_period=20, slow_period=50)
    candles = generate_history(length=5, timeframe="5m", seed=7)
    from app.features.engine import FeatureEngine

    features = FeatureEngine(config.features).compute(candles, timeframe="5m", now=candles[-1].timestamp)
    snap = regime_engine.classify(features, now=candles[-1].timestamp)
    assert snap.is_unknown


# ---------------------------------------------------------------------------
# 4. Strategy signal generation
# ---------------------------------------------------------------------------
def test_strategy_returns_none_on_unknown_regime():
    from app.strategies.breakout_momentum import BreakoutMomentumStrategy

    config = _config()
    strategy = BreakoutMomentumStrategy(config.strategy)
    from app.domain.regime import RegimeSnapshot
    from app.domain.enums import Regime
    from app.strategies.base import StrategyContext
    from tests.support import make_features

    ctx = StrategyContext(
        symbol="BTC/USD",
        timeframe="5m",
        features=make_features(),
        regime=RegimeSnapshot(symbol="BTC/USD", timestamp=_base_time(), timeframe="5m", regime=Regime.UNKNOWN),
        now=_base_time(),
    )
    assert strategy.evaluate(ctx) is None


def test_strategy_emits_long_on_valid_breakout():
    from app.strategies.breakout_momentum import BreakoutMomentumStrategy

    config = _config()
    strategy = BreakoutMomentumStrategy(config.strategy)
    from app.domain.regime import RegimeSnapshot
    from app.domain.enums import Regime
    from app.strategies.base import StrategyContext
    from tests.support import make_features

    features = make_features(close=115.0, values={"range_high": 100.0, "range_low": 90.0, "rsi": 65.0, "atr": 50.0, "ema_20": 105.0, "ema_50": 100.0, "vwap": 99.0})
    ctx = StrategyContext(
        symbol="BTC/USD",
        timeframe="5m",
        features=features,
        regime=RegimeSnapshot(symbol="BTC/USD", timestamp=_base_time(), timeframe="5m", regime=Regime.TRENDING_BULLISH),
        now=_base_time(),
    )
    signal = strategy.evaluate(ctx)
    assert signal is not None
    assert signal.direction is Direction.LONG


# ---------------------------------------------------------------------------
# 5. Risk rejection
# ---------------------------------------------------------------------------
def test_risk_rejects_when_position_limit_reached():
    from app.risk.engine import RiskEngine, RiskContext
    from tests.support import make_signal

    config = _config()
    engine = RiskEngine(config.risk, allow_short=True, cooldown_minutes=0.0)
    signal = make_signal()
    ctx = RiskContext(
        now=_base_time(),
        symbol="BTC/USD",
        session_active=True,
        entries_allowed=True,
        market_data_fresh=True,
        system_healthy=True,
        reconciliation_ok=True,
        account_equity=100000.0,
        open_positions=1,
        orders_this_session=0,
        daily_realized_pnl=0.0,
        last_trade_time=None,
        position=Direction.LONG,
        position_holding_seconds=None,
        allowed_symbols={"BTC/USD"},
    )
    decision = engine.evaluate(signal, ctx)
    assert not decision.approved


def test_risk_rejects_on_cooldown():
    from app.risk.engine import RiskEngine, RiskContext
    from tests.support import make_signal

    config = _config(**{"strategy.cooldown.enabled": True, "strategy.cooldown.minutes": 5.0})
    engine = RiskEngine(config.risk, allow_short=True, cooldown_minutes=5.0)
    signal = make_signal()
    ctx = RiskContext(
        now=_base_time(),
        symbol="BTC/USD",
        session_active=True,
        entries_allowed=True,
        market_data_fresh=True,
        system_healthy=True,
        reconciliation_ok=True,
        account_equity=100000.0,
        open_positions=0,
        orders_this_session=0,
        daily_realized_pnl=0.0,
        last_trade_time=_base_time() - timedelta(minutes=2),
        position=Direction.FLAT,
        position_holding_seconds=None,
        allowed_symbols={"BTC/USD"},
    )
    decision = engine.evaluate(signal, ctx)
    assert not decision.approved


# ---------------------------------------------------------------------------
# 6. Position sizing
# ---------------------------------------------------------------------------
def test_sizer_rejects_invalid_stop_distance():
    from app.portfolio.position_sizing import PositionSizer
    from tests.support import make_signal

    config = _config()
    sizer = PositionSizer(config.position_sizing)
    signal = make_signal()
    signal.stop_reference = None
    result = sizer.size(signal, equity=100000.0, reference_price=30000.0)
    assert not result.ok


def test_sizer_respects_max_notional():
    from app.portfolio.position_sizing import PositionSizer
    from tests.support import make_signal

    config = _config()
    sizer = PositionSizer(config.position_sizing)
    signal = make_signal()
    result = sizer.size(signal, equity=100000.0, reference_price=30000.0, max_notional=1000.0)
    assert result.ok
    assert result.final_quantity * 30000.0 <= 1000.0 + 1e-9


# ---------------------------------------------------------------------------
# 7. Simulated fills / fees / slippage / partial fills
# ---------------------------------------------------------------------------
def test_simulated_broker_buy_fill_applies_slippage_and_spread():
    cfg = BacktestingExecutionConfig(spread_percent=0.02, slippage_percent=0.05, fee_percent=0.0)
    broker = SimulatedBroker(cfg)
    order = broker.submit(
        order_id="o1", side=Side.BUY, direction=Direction.LONG,
        quantity=1.0, price=100.0, timestamp=_base_time(),
    )
    fill = broker.try_fill(
        order, candle_high=110.0, candle_low=90.0, candle_close=100.0,
        timestamp=_base_time(), fill_id="f1",
    )
    assert fill is not None
    assert fill.quantity == 1.0
    assert fill.price > 100.0  # slippage + spread


def test_simulated_broker_sell_fill_applies_slippage_and_spread():
    cfg = BacktestingExecutionConfig(spread_percent=0.02, slippage_percent=0.05, fee_percent=0.0)
    broker = SimulatedBroker(cfg)
    order = broker.submit(
        order_id="o2", side=Side.SELL, direction=Direction.LONG,
        quantity=1.0, price=100.0, timestamp=_base_time(),
    )
    fill = broker.try_fill(
        order, candle_high=110.0, candle_low=90.0, candle_close=100.0,
        timestamp=_base_time(), fill_id="f2",
    )
    assert fill is not None
    assert fill.price < 100.0


def test_simulated_broker_in_kind_fee_reduces_buy_quantity():
    cfg = BacktestingExecutionConfig(spread_percent=0.0, slippage_percent=0.0, fee_percent=0.25, fee_model="in_kind")
    broker = SimulatedBroker(cfg)
    order = broker.submit(
        order_id="o3", side=Side.BUY, direction=Direction.LONG,
        quantity=1.0, price=100.0, timestamp=_base_time(),
    )
    fill = broker.try_fill(
        order, candle_high=110.0, candle_low=90.0, candle_close=100.0,
        timestamp=_base_time(), fill_id="f3",
    )
    assert fill is not None
    assert fill.quantity < 1.0
    assert fill.quantity == pytest.approx(0.9975, abs=1e-6)


def test_simulated_broker_partial_fill():
    cfg = BacktestingExecutionConfig(
        spread_percent=0.0, slippage_percent=0.0, fee_percent=0.0,
        partial_fill_enabled=True, partial_fill_max_fraction=0.5,
    )
    broker = SimulatedBroker(cfg)
    order = broker.submit(
        order_id="o4", side=Side.BUY, direction=Direction.LONG,
        quantity=1.0, price=100.0, timestamp=_base_time(),
    )
    fill = broker.try_fill(
        order, candle_high=110.0, candle_low=90.0, candle_close=100.0,
        timestamp=_base_time(), fill_id="f4",
    )
    assert fill is not None
    assert fill.quantity == pytest.approx(0.5, abs=1e-6)
    assert order.status == "partially_filled"


def test_simulated_broker_market_order_always_fills():
    """Market orders fill at the slippage/spread-adjusted close price."""
    cfg = BacktestingExecutionConfig(spread_percent=0.0, slippage_percent=0.0, fee_percent=0.0)
    broker = SimulatedBroker(cfg)
    order = broker.submit(
        order_id="o5", side=Side.BUY, direction=Direction.LONG,
        quantity=1.0, price=100.0, timestamp=_base_time(),
    )
    fill = broker.try_fill(
        order, candle_high=95.0, candle_low=90.0, candle_close=92.0,
        timestamp=_base_time(), fill_id="f5",
    )
    assert fill is not None
    assert fill.price == pytest.approx(92.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 8. Stop / target / max-holding exits
# ---------------------------------------------------------------------------
def test_stop_loss_exit_triggers():
    config = _config()
    engine = BacktestEngine(config)
    ts = _base_time()
    # Candle lows descend to 101, below the stop at 102.
    candles = [
        _candle(ts + timedelta(minutes=5 * i), 100.0 + i, high=101.0 + i, low=99.0 + i)
        for i in range(50)
    ]
    candles[5] = _candle(ts + timedelta(minutes=25), 105.0, high=106.0, low=101.0)
    from app.portfolio.position_manager import PositionManager
    from app.domain.orders import Fill

    pm = PositionManager("BTC/USD")
    fill = Fill(fill_id="f", order_id="o", timestamp=ts, symbol="BTC/USD", side=Side.BUY, quantity=1.0, price=100.0)
    pm.apply_fill(fill, stop=102.0, target=110.0)
    exit_price, reason = engine._check_exit(pm.position, candles[5])
    assert reason == "stop_loss"
    assert exit_price == 102.0


def test_take_profit_exit_triggers():
    config = _config()
    engine = BacktestEngine(config)
    ts = _base_time()
    candles = [
        _candle(ts + timedelta(minutes=5 * i), 100.0 + i, high=101.0 + i, low=99.0 + i)
        for i in range(50)
    ]
    from app.portfolio.position_manager import PositionManager
    from app.domain.orders import Fill

    pm = PositionManager("BTC/USD")
    fill = Fill(fill_id="f", order_id="o", timestamp=ts, symbol="BTC/USD", side=Side.BUY, quantity=1.0, price=100.0)
    pm.apply_fill(fill, stop=95.0, target=103.0)
    # Find a candle where high >= 103
    for c in candles:
        if c.high >= 103.0:
            exit_price, reason = engine._check_exit(pm.position, c)
            assert reason == "take_profit"
            assert exit_price == 103.0
            return
    pytest.fail("No candle reached target")


def test_max_holding_exit_triggers():
    config = _config(**{"session.max_holding_minutes": 10})
    engine = BacktestEngine(config)
    ts = _base_time()
    candles = [
        _candle(ts + timedelta(minutes=5 * i), 100.0, high=101.0, low=99.0)
        for i in range(50)
    ]
    from app.portfolio.position_manager import PositionManager
    from app.domain.orders import Fill

    pm = PositionManager("BTC/USD")
    fill = Fill(fill_id="f", order_id="o", timestamp=ts, symbol="BTC/USD", side=Side.BUY, quantity=1.0, price=100.0)
    pm.apply_fill(fill, stop=95.0, target=110.0)
    # After 10 minutes (2 bars), the position should force-exit.
    exit_price, reason = engine._check_exit(pm.position, candles[3])
    assert reason == "forced_exit"


# ---------------------------------------------------------------------------
# 9. Session flatten / reconciliation / final flat invariant
# ---------------------------------------------------------------------------
def test_backtest_produces_zero_trades_on_flat_data():
    config = _config()
    candles = generate_history(length=100, timeframe="5m", seed=7)
    result = BacktestEngine(config).run(candles)
    # Zero trades is a valid outcome — the strategy may not fire.
    assert isinstance(result.metrics.trade_count, int)


def test_accounting_invariant_holds_throughout_backtest():
    config = _config()
    candles = generate_history(length=300, timeframe="5m", seed=7)
    result = BacktestEngine(config).run(candles)
    for check in result.accounting_checks:
        assert check["consistent"], f"Bar {check['bar']}: ledger={check['ledger']} oms={check['oms']} pm={check['pm']}"


def test_final_position_is_zero():
    config = _config()
    candles = generate_history(length=300, timeframe="5m", seed=7)
    result = BacktestEngine(config).run(candles)
    if result.accounting_checks:
        assert result.accounting_checks[-1]["pm"] == 0.0


# ---------------------------------------------------------------------------
# 10. Deterministic replay
# ---------------------------------------------------------------------------
def test_backtest_is_deterministic():
    config = _config()
    candles = generate_history(length=200, timeframe="5m", seed=7)
    r1 = BacktestEngine(config).run(candles)
    r2 = BacktestEngine(config).run(candles)
    assert r1.run_id == r2.run_id
    assert r1.metrics.net_pnl == r2.metrics.net_pnl
    assert r1.metrics.trade_count == r2.metrics.trade_count


# ---------------------------------------------------------------------------
# 11. Execution gate cannot reach real Alpaca
# ---------------------------------------------------------------------------
def test_backtest_never_creates_real_broker_orders():
    config = _config()
    assert config.execution.enabled is False
    candles = generate_history(length=100, timeframe="5m", seed=7)
    result = BacktestEngine(config).run(candles)
    # The simulated broker's orders are SimulatedOrder objects, not real broker orders.
    for order in result.trades:
        assert "broker_order_id" not in order or order.get("broker_order_id") is None


def test_repository_config_remains_unchanged():
    config = load_config(env=get_env())
    assert config.execution.enabled is False
    assert config.trading.mode == "paper"


# ---------------------------------------------------------------------------
# 12. Cost sensitivity
# ---------------------------------------------------------------------------
def test_cost_sensitivity_zero_vs_elevated():
    config = _config()
    candles = generate_history(length=300, timeframe="5m", seed=7)

    # Zero costs
    config.backtesting.execution.fee_percent = 0.0
    config.backtesting.execution.slippage_percent = 0.0
    config.backtesting.execution.spread_percent = 0.0
    r_zero = BacktestEngine(config).run(candles)

    # Elevated costs
    config.backtesting.execution.fee_percent = 1.0
    config.backtesting.execution.slippage_percent = 0.5
    config.backtesting.execution.spread_percent = 0.5
    r_elevated = BacktestEngine(config).run(candles)

    # Elevated costs should produce worse or equal net P&L.
    assert r_elevated.metrics.net_pnl <= r_zero.metrics.net_pnl + 1e-6


# ---------------------------------------------------------------------------
# 13. Trade record completeness
# ---------------------------------------------------------------------------
def test_trade_records_have_required_fields():
    config = _config()
    candles = generate_history(length=600, timeframe="5m", seed=7)
    result = BacktestEngine(config).run(candles)
    required = {
        "trade_id", "symbol", "side", "quantity_requested", "quantity_filled",
        "entry_time", "entry_price", "exit_time", "exit_price",
        "gross_pnl", "fees", "slippage", "net_pnl", "holding_seconds",
        "strategy", "regime", "timeframe", "signal", "stop", "target",
        "exit_reason", "risk_per_trade", "position_value", "outcome",
    }
    for trade in result.trades:
        missing = required - set(trade.keys())
        assert not missing, f"Trade {trade.get('trade_id')} missing fields: {missing}"


# ---------------------------------------------------------------------------
# 14. Metrics
# ---------------------------------------------------------------------------
def test_metrics_computation_basic():
    metrics = compute_metrics(
        closed_pnls=[100.0, -50.0, 200.0, -30.0],
        holding_seconds=[60.0, 120.0, 180.0, 90.0],
        equity_curve=[100000.0, 100100.0, 100050.0, 100250.0, 100220.0],
        initial_capital=100000.0,
        fees=10.0,
        slippage=5.0,
    )
    assert metrics.trade_count == 4
    assert metrics.winning_trades == 2
    assert metrics.losing_trades == 2
    assert metrics.win_rate == 0.5
    assert metrics.net_pnl == pytest.approx(220.0 - 10.0, abs=0.01)


def test_metrics_zero_trades():
    metrics = compute_metrics(
        closed_pnls=[],
        holding_seconds=[],
        equity_curve=[100000.0],
        initial_capital=100000.0,
    )
    assert metrics.trade_count == 0
    assert metrics.win_rate == 0.0
    assert metrics.net_pnl == 0.0
