"""Shared builders for the Phase A safety regression tests.

Keeping the fixtures in one place keeps every safety test focused on the
invariant it guards rather than on object construction details.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.ids import new_correlation_id, new_signal_id
from app.domain.enums import Direction, ReasonCode, Regime, Side
from app.domain.features import FeatureSnapshot
from app.domain.orders import Fill
from app.domain.regime import RegimeSnapshot
from app.domain.signals import StrategySignal
from app.risk.engine import RiskContext

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SYMBOL = "BTC/USD"

# A fully-formed feature set that would otherwise be a textbook long breakout.
READY_FEATURES: dict[str, float] = {
    "rsi": 65.0,
    "atr": 50.0,
    "vwap": 99.0,
    "range_high": 100.0,
    "range_low": 90.0,
    "ema_20": 105.0,
    "ema_50": 100.0,
}


def make_features(
    values: dict[str, float | None] | None = None,
    *,
    close: float = 110.0,
    spread_percent: float | None = 0.02,
    candle_count: int = 60,
    symbol: str = SYMBOL,
    timestamp: datetime = NOW,
    timeframe: str = "5m",
) -> FeatureSnapshot:
    merged: dict[str, float | None] = dict(READY_FEATURES)
    if values:
        merged.update(values)
    return FeatureSnapshot(
        symbol=symbol,
        timestamp=timestamp,
        timeframe=timeframe,
        close=close,
        high=close + 1.0,
        low=close - 1.0,
        spread_percent=spread_percent,
        candle_count=candle_count,
        values=merged,
    )


def make_regime(
    regime: Regime = Regime.TRENDING_BULLISH,
    *,
    reason: str = "test",
    symbol: str = SYMBOL,
    timestamp: datetime = NOW,
    timeframe: str = "5m",
) -> RegimeSnapshot:
    return RegimeSnapshot(
        symbol=symbol,
        timestamp=timestamp,
        timeframe=timeframe,
        regime=regime,
        reason=reason,
    )


def make_signal(
    *,
    price: float = 30000.0,
    stop_distance: float = 500.0,
    direction: Direction = Direction.LONG,
    regime: Regime = Regime.TRENDING_BULLISH,
    symbol: str = SYMBOL,
    timestamp: datetime = NOW,
    stop_reference: float | None = ...,
) -> StrategySignal:
    stop = (
        stop_reference
        if stop_reference is not ...
        else (price - stop_distance if direction is Direction.LONG else price + stop_distance)
    )
    take_profit = price + 2 * stop_distance if direction is Direction.LONG else price - 2 * stop_distance
    return StrategySignal(
        signal_id=new_signal_id(),
        timestamp=timestamp,
        symbol=symbol,
        strategy="breakout_momentum",
        direction=direction,
        reason_code=ReasonCode.BREAKOUT_ABOVE_RANGE,
        reason="test_signal",
        entry_reference=price,
        stop_reference=stop,
        take_profit_reference=take_profit,
        timeframe="5m",
        regime=regime,
        correlation_id=new_correlation_id(),
        session_id="ses_test",
    )


def make_fill(
    quantity: float,
    price: float,
    side: Side = Side.BUY,
    *,
    fill_id: str = "fil_test",
    order_id: str = "ord_test",
    broker_fill_id: str | None = None,
    timestamp: datetime = NOW,
    fee: float = 0.0,
    symbol: str = SYMBOL,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        timestamp=timestamp,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
        broker_fill_id=broker_fill_id,
    )


def make_risk_context(
    *,
    equity: float = 100000.0,
    symbol: str = SYMBOL,
    now: datetime = NOW,
    **overrides: object,
) -> RiskContext:
    base: dict[str, object] = dict(
        now=now,
        symbol=symbol,
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
        allowed_symbols={symbol},
    )
    base.update(overrides)
    return RiskContext(**base)
