"""Phase D.7-R2 regression tests: backtest accounting remediation.

Covers BUG-D7-001 (short exit under in-kind fees left a residual position,
causing phantom trade records and repeated cumulative-PnL attribution) and
the hardened invariants: no phantom records, lifecycle trade PnL, flatness,
direction filtering.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.backtesting.data import generate_history
from app.backtesting.engine import (
    FLAT_QTY_TOLERANCE,
    BacktestEngine,
    BacktestStateViolation,
)
from app.config.loader import get_env, load_config
from app.core.clock import utcnow
from app.domain.enums import Side
from app.domain.market import Candle
from app.domain.orders import Fill
from app.portfolio.pnl_engine import PnlEngine
from app.portfolio.position_manager import PositionManager


def _test_config(*, directions: list[str]):
    config = load_config(env=get_env())
    config.strategy.allowed_directions = directions
    return config


def test_bug_d7_001_short_exit_in_kind_fee_does_not_leave_residual():
    """BUG-D7-001: a short exit must close the FULL quantity.

    Previously the exit BUY was multiplied by (1 - fee), leaving ~0.25%
    residual that kept the book non-flat forever.
    """
    config = _test_config(directions=["long", "short"])
    assert config.backtesting.execution.fee_model == "in_kind"
    engine = BacktestEngine(config)
    manager = PositionManager("BTC/USD")
    pnl = PnlEngine("BTC/USD", starting_equity=100000.0)

    now = utcnow()
    short_qty = 0.238861
    price = 83271.2922
    engine._apply_fill(
        Fill(fill_id="f-entry", order_id="o-entry", timestamp=now, symbol="BTC/USD",
             side=Side.SELL, quantity=short_qty, price=price, fee=0.0),
        manager, pnl, is_exit=False,
    )
    assert not manager.is_flat

    exit_fill = engine._exit_fill(manager, 83176.597, now, "forced_exit")
    # Full close: exit quantity equals the entire short position.
    assert exit_fill.quantity == short_qty
    assert exit_fill.side is Side.BUY
    # Fee settles in quote so the close can never leave a residual.
    expected_fee = short_qty * 83176.597 * (config.backtesting.execution.fee_percent / 100.0)
    assert exit_fill.fee == pytest.approx(expected_fee, rel=1e-9)

    engine._apply_fill(exit_fill, manager, pnl, is_exit=True)
    assert manager.is_flat
    assert abs(manager.position.quantity) <= FLAT_QTY_TOLERANCE
    assert abs(engine.fill_ledger.net_quantity("BTC/USD")) <= FLAT_QTY_TOLERANCE
    assert abs(engine.oms.net_quantity("BTC/USD")) <= FLAT_QTY_TOLERANCE


def test_long_exit_closes_full_quantity_with_quote_fee():
    config = _test_config(directions=["long"])
    engine = BacktestEngine(config)
    manager = PositionManager("BTC/USD")
    pnl = PnlEngine("BTC/USD", starting_equity=100000.0)
    now = utcnow()
    qty = 0.236966
    engine._apply_fill(
        Fill(fill_id="f-e2", order_id="o-e2", timestamp=now, symbol="BTC/USD",
             side=Side.BUY, quantity=qty, price=84450.7954, fee=0.0),
        manager, pnl, is_exit=False,
    )
    exit_fill = engine._exit_fill(manager, 82971.6175, now, "forced_exit")
    assert exit_fill.quantity == qty
    assert exit_fill.side is Side.SELL
    engine._apply_fill(exit_fill, manager, pnl, is_exit=True)
    assert manager.is_flat


def test_short_roundtrip_full_replay_has_no_phantoms():
    """Full replay on downward-drift data: every record valid, books flat."""
    config = _test_config(directions=["long", "short"])
    candles = generate_history("BTC/USD", timeframe="5m", length=600, seed=11, drift=-0.0015)
    result = BacktestEngine(config).run(candles)
    assert result.state_violations == []
    assert result.trades, "expected at least one round trip on downward drift"
    assert all(t.get("trade_id") for t in result.trades)
    assert len(result.trades) == result.entries
    final = result.accounting_checks[-1]
    assert final["consistent"]
    assert abs(final["pm"]) <= FLAT_QTY_TOLERANCE


def test_completed_trade_then_flat_bars_produce_no_phantoms():
    """Completed trade + N flat bars: records stay == entries, never N."""
    config = _test_config(directions=["long"])
    base = generate_history("BTC/USD", timeframe="5m", length=600, seed=7)
    px = base[-1].close
    tail = [
        Candle(timestamp=base[-1].timestamp + timedelta(seconds=(i + 1) * 300),
               symbol="BTC/USD", open=px, high=px, low=px, close=px,
               volume=1.0, timeframe="5m")
        for i in range(300)
    ]
    result = BacktestEngine(config).run(base + tail)
    assert result.state_violations == []
    assert all(t.get("trade_id") for t in result.trades)
    assert len(result.trades) == result.entries
    assert len(result.trades) < 20  # must not grow with the 300-bar tail


def test_trade_pnl_sums_to_realized_less_fees():
    """Per-trade lifecycle PnL reconciles with account realized PnL."""
    config = _test_config(directions=["long", "short"])
    candles = generate_history("BTC/USD", timeframe="5m", length=600, seed=11, drift=-0.0015)
    result = BacktestEngine(config).run(candles)
    assert result.trades
    trade_sum = sum(t["net_pnl"] for t in result.trades)
    assert trade_sum == pytest.approx(result.realized_pnl - result.fees_paid, abs=1e-4)
    # Engine headline net uses the same gross-minus-fees basis.
    assert result.metrics.net_pnl == pytest.approx(trade_sum, abs=1e-4)


def test_long_only_direction_filter_blocks_shorts():
    """BTC/USD spot validation is long-only; shorts are filtered, not entered."""
    candles = generate_history("BTC/USD", timeframe="5m", length=600, seed=11, drift=-0.0015)
    both = BacktestEngine(_test_config(directions=["long", "short"])).run(candles)
    assert any(t["side"] == "short" for t in both.trades)
    long_only = BacktestEngine(_test_config(directions=["long"])).run(candles)
    assert all(t["side"] == "long" for t in long_only.trades)
    assert long_only.direction_filtered >= 1
    assert long_only.state_violations == []


def test_close_trade_record_refuses_phantom():
    """The engine raises instead of emitting an empty phantom record."""
    config = _test_config(directions=["long"])
    engine = BacktestEngine(config)
    fill = Fill(fill_id="f-x", order_id="o-x", timestamp=utcnow(), symbol="BTC/USD",
                side=Side.SELL, quantity=0.1, price=80000.0, fee=1.0)
    with pytest.raises(BacktestStateViolation):
        engine._close_trade_record(None, fill, -5.0, "forced_exit", utcnow())
