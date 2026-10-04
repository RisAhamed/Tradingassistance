"""Smoke tests for the critical max_exposure fix.

Regression: RiskEngine._check_exposure used to compare the raw entry *price*
(e.g. BTC ~30_000) against the notional cap (equity * 20% ~= 20_000) and
rejected EVERY signal. The pre-sizing gate must defer the authoritative
notional check to PositionSizer (quantity * price), which knows the quantity.
"""
from datetime import timezone

from app.config.models import PositionSizingConfig, RiskConfig
from app.core.clock import utcnow
from app.core.ids import new_correlation_id, new_signal_id
from app.domain.enums import Direction, ReasonCode, Regime
from app.domain.signals import StrategySignal
from app.portfolio.position_sizing import PositionSizer
from app.risk.engine import RiskContext, RiskEngine


def _signal(price: float = 30000.0, stop_distance: float = 500.0) -> StrategySignal:
    now = utcnow()
    return StrategySignal(
        signal_id=new_signal_id(),
        timestamp=now,
        symbol="BTC/USD",
        strategy="breakout_momentum",
        direction=Direction.LONG,
        reason_code=ReasonCode.BREAKOUT_ABOVE_RANGE,
        reason="breakout_above_range",
        entry_reference=price,
        stop_reference=price - stop_distance,
        take_profit_reference=price + 2 * stop_distance,
        timeframe="5m",
        regime=Regime.TRENDING_BULLISH,
        correlation_id=new_correlation_id(),
        session_id="ses_test",
    )


def _ctx(equity: float = 100000.0) -> RiskContext:
    return RiskContext(
        now=utcnow(),
        symbol="BTC/USD",
        session_active=True,
        entries_allowed=True,
        market_data_fresh=True,
        system_healthy=True,
        reconciliation_ok=True,
        account_equity=equity,
        open_positions=0,
        orders_this_session=0,
        daily_realized_pnl=0.0,
        last_trade_time=None,
        allowed_symbols={"BTC/USD"},
    )


def test_exposure_gate_passes_for_btc_price():
    engine = RiskEngine(RiskConfig(), allow_short=True, cooldown_minutes=0.0)
    decision = engine.evaluate(_signal(price=30000.0), _ctx())
    failed = {c.name for c in decision.failed_checks}
    assert "max_exposure" not in failed, decision.model_dump(mode="json")


def test_sizer_caps_notional_to_max_value():
    sizer = PositionSizer(PositionSizingConfig())
    signal = _signal(price=30000.0, stop_distance=500.0)
    equity = 100000.0
    max_notional = equity * 0.20  # 20_000
    sizing = sizer.size(signal, equity, reference_price=30000.0, max_notional=max_notional)
    assert sizing.ok, sizing.rejected_reason
    assert sizing.final_quantity * 30000.0 <= max_notional + 1e-6


def test_sizer_rejects_when_cap_below_minimum_quantity():
    sizer = PositionSizer(PositionSizingConfig())
    signal = _signal(price=30000.0, stop_distance=500.0)
    sizing = sizer.size(signal, 100000.0, reference_price=30000.0, max_notional=0.01)
    assert not sizing.ok
    assert sizing.rejected_reason == "max_exposure"


def test_backtest_produces_trades():
    from app.backtesting.data import generate_history
    from app.backtesting.engine import BacktestEngine
    from app.config.loader import get_env, load_config

    config = load_config(env=get_env())
    candles = generate_history(config.trading.symbol, timeframe=config.timeframes.signal, length=600, seed=7)
    result = BacktestEngine(config).run(candles)
    assert result.entries > 0, f"expected trades, got signals={result.signals} rejections={result.rejections}"
