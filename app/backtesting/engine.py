"""Backtesting engine: same features/regime/strategy/risk/sizing, different data.

Only two differences vs. live paper trading: the historical data source and
simulated execution. There is NO separate strategy implementation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.backtesting.metrics import BacktestMetrics, compute_metrics
from app.config.models import AppConfig
from app.core.ids import new_correlation_id, new_fill_id, new_order_id
from app.domain.enums import Direction, Side
from app.domain.market import Candle
from app.domain.orders import Fill
from app.features.engine import FeatureEngine
from app.portfolio.pnl_engine import PnlEngine
from app.portfolio.position_manager import PositionManager
from app.portfolio.position_sizing import PositionSizer
from app.regime.engine import RegimeEngine
from app.risk.engine import RiskContext, RiskEngine
from app.signals.generator import SignalGenerator
from app.strategies.base import StrategyContext
from app.strategies.breakout_momentum import BreakoutMomentumStrategy

logger = logging.getLogger("app.backtesting.engine")


@dataclass(slots=True)
class BacktestResult:
    metrics: BacktestMetrics
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    signals: int = 0
    rejections: int = 0
    entries: int = 0

    def to_dict(self) -> dict:
        return {
            "metrics": self.metrics.to_dict(),
            "trades": self.trades,
            "signals": self.signals,
            "rejections": self.rejections,
            "entries": self.entries,
        }


class BacktestEngine:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.symbol = config.trading.symbol
        self.timeframe = config.timeframes.signal
        self.feature_engine = FeatureEngine(config.features)
        self.regime_engine = RegimeEngine(
            config.regime,
            fast_period=config.strategy.trend.fast_ema,
            slow_period=config.strategy.trend.slow_ema,
        )
        strategy = BreakoutMomentumStrategy(config.strategy)
        self.generator = SignalGenerator(strategy, config.risk)
        self.risk_engine = RiskEngine(
            config.risk,
            allow_short=config.execution.allow_short,
            cooldown_minutes=config.strategy.cooldown.minutes if config.strategy.cooldown.enabled else 0.0,
        )
        self.sizer = PositionSizer(config.position_sizing)

    def run(self, candles: list[Candle]) -> BacktestResult:
        if not candles:
            return BacktestResult(metrics=BacktestMetrics())

        initial = self.config.backtesting.initial_capital
        position_manager = PositionManager(self.symbol)
        pnl = PnlEngine(self.symbol, starting_equity=initial)
        slippage = self.config.backtesting.slippage_percent / 100.0
        commission = self.config.backtesting.commission

        trades: list[dict] = []
        closed_pnls: list[float] = []
        holding_seconds: list[float] = []
        equity_curve: list[float] = []
        signals = rejections = entries = forced_exits = 0
        fees_total = 0.0
        slippage_total = 0.0
        last_trade_time: datetime | None = None
        min_candles = self.config.regime.min_candles

        for index, candle in enumerate(candles):
            if index + 1 < min_candles:
                equity_curve.append(pnl.equity)
                continue
            window = candles[: index + 1]
            features = self.feature_engine.compute(window, timeframe=self.timeframe, now=candle.timestamp)
            regime = self.regime_engine.classify(features, now=candle.timestamp)

            # --- manage an open position (intrabar stop/target) ------------
            if not position_manager.is_flat:
                position = position_manager.position
                exit_price, exit_reason = self._check_exit(position, candle)
                if exit_price is not None:
                    fill = self._fill(position, exit_price, commission, candle.timestamp, reduce=True)
                    update = position_manager.apply_fill(fill)
                    if update.realized_delta:
                        pnl.add_realized(update.realized_delta)
                        closed_pnls.append(update.realized_delta)
                    if fill.fee:
                        pnl.add_fee(fill.fee)
                        fees_total += fill.fee
                    holding = (
                        max(0.0, (candle.timestamp - position.opened_at).total_seconds())
                        if position.opened_at
                        else 0.0
                    )
                    holding_seconds.append(holding)
                    if exit_reason == "forced_exit":
                        forced_exits += 1
                    last_trade_time = candle.timestamp
                    trades.append(
                        {
                            "closed_at": candle.timestamp.isoformat(),
                            "direction": fill.side.value,
                            "entry": position.average_entry,
                            "exit": exit_price,
                            "reason": exit_reason,
                            "realized_pnl": round(update.realized_delta, 6),
                        }
                    )

            # --- evaluate a new entry when flat ----------------------------
            if position_manager.is_flat and self.config.strategy.enabled:
                context = StrategyContext(
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    features=features,
                    regime=regime,
                    now=candle.timestamp,
                    position_open=False,
                    correlation_id=new_correlation_id(),
                )
                try:
                    signal = self.generator.generate(context)
                except Exception:  # noqa: BLE001 - a broken signal is skipped
                    logger.exception("strategy error during backtest")
                    signal = None
                if signal is not None:
                    signals += 1
                    decision = self.risk_engine.evaluate(
                        signal,
                        self._risk_context(pnl, position_manager, last_trade_time, candle.timestamp),
                    )
                    if not decision.approved:
                        rejections += 1
                    else:
                        max_notional = pnl.equity * (self.config.risk.maximum_position_value_percent / 100.0)
                        sizing = self.sizer.size(
                            signal,
                            pnl.equity,
                            reference_price=candle.close,
                            max_notional=max_notional if max_notional > 0 else None,
                        )
                        if sizing.ok:
                            fill_price = candle.close * (
                                (1 + slippage) if signal.direction is Direction.LONG else (1 - slippage)
                            )
                            side = Side.BUY if signal.direction is Direction.LONG else Side.SELL
                            fill = Fill(
                                fill_id=new_fill_id(),
                                order_id=new_order_id(),
                                timestamp=candle.timestamp,
                                symbol=self.symbol,
                                side=side,
                                quantity=sizing.final_quantity,
                                price=fill_price,
                                fee=commission,
                            )
                            slippage_total += abs(fill_price - candle.close) * sizing.final_quantity
                            position_manager.apply_fill(
                                fill, stop=signal.stop_reference, target=signal.take_profit_reference
                            )
                            if fill.fee:
                                pnl.add_fee(fill.fee)
                                fees_total += fill.fee
                            entries += 1
                            last_trade_time = candle.timestamp

            position_manager.mark(candle.close)
            equity_curve.append(pnl.mark(position_manager.position.unrealized_pnl).equity)

        # Force-close anything left open at the end of the series.
        if not position_manager.is_flat:
            position = position_manager.position
            fill = self._fill(position, candles[-1].close, commission, candles[-1].timestamp, reduce=True)
            update = position_manager.apply_fill(fill)
            if update.realized_delta:
                pnl.add_realized(update.realized_delta)
                closed_pnls.append(update.realized_delta)
            if fill.fee:
                fees_total += fill.fee
            holding_seconds.append(
                max(0.0, (candles[-1].timestamp - position.opened_at).total_seconds())
                if position.opened_at
                else 0.0
            )
            forced_exits += 1
            trades.append(
                {
                    "closed_at": candles[-1].timestamp.isoformat(),
                    "direction": fill.side.value,
                    "entry": position.average_entry,
                    "exit": fill.price,
                    "reason": "end_of_data",
                    "realized_pnl": round(update.realized_delta, 6),
                }
            )
            equity_curve.append(pnl.equity)

        metrics = compute_metrics(
            closed_pnls=closed_pnls,
            holding_seconds=holding_seconds,
            equity_curve=equity_curve,
            initial_capital=initial,
            fees=fees_total,
            slippage=slippage_total,
            rejected_trades=rejections,
            forced_exits=forced_exits,
        )
        return BacktestResult(
            metrics=metrics,
            trades=trades,
            equity_curve=equity_curve,
            signals=signals,
            rejections=rejections,
            entries=entries,
        )

    # -- helpers ------------------------------------------------------------
    def _fill(self, position, price: float, commission: float, timestamp, *, reduce: bool) -> Fill:
        side = Side.SELL if position.direction is Direction.LONG else Side.BUY
        if not reduce:
            side = Side.BUY if position.direction is Direction.LONG else Side.SELL
        return Fill(
            fill_id=new_fill_id(),
            order_id=new_order_id(),
            timestamp=timestamp,
            symbol=self.symbol,
            side=side,
            quantity=abs(position.quantity),
            price=price,
            fee=commission,
        )

    def _check_exit(self, position, candle: Candle) -> tuple[float | None, str | None]:
        is_long = position.direction is Direction.LONG
        if position.stop is not None:
            if is_long and candle.low <= position.stop:
                return position.stop, "stop_loss"
            if not is_long and candle.high >= position.stop:
                return position.stop, "stop_loss"
        if position.target is not None:
            if is_long and candle.high >= position.target:
                return position.target, "take_profit"
            if not is_long and candle.low <= position.target:
                return position.target, "take_profit"
        if position.opened_at is not None:
            held_minutes = (candle.timestamp - position.opened_at).total_seconds() / 60.0
            if held_minutes > self.config.session.max_holding_minutes:
                return candle.close, "forced_exit"
        return None, None

    def _risk_context(self, pnl, position_manager, last_trade_time, now: datetime) -> RiskContext:
        position = position_manager.position
        return RiskContext(
            now=now,
            symbol=self.symbol,
            session_active=True,
            entries_allowed=True,
            market_data_fresh=True,
            system_healthy=True,
            reconciliation_ok=True,
            account_equity=pnl.equity,
            open_positions=0 if position_manager.is_flat else 1,
            orders_this_session=0,
            daily_realized_pnl=pnl.daily_realized,
            last_trade_time=last_trade_time,
            position=position.direction,
            position_holding_seconds=position.holding_seconds(now) if not position.is_flat else None,
            allowed_symbols={self.symbol},
        )
