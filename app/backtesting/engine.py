"""Backtesting engine: full pipeline replay with realistic simulated execution.

Reuses the REAL Trading GOAT components — FeatureEngine, RegimeEngine,
BreakoutMomentumStrategy, SignalGenerator, RiskEngine, PositionSizer,
FillLedger, OMS, PositionManager, PnlEngine, SessionManager. Only the data
source and execution are simulated.

Pipeline per bar:
    features → regime → exit check → entry check → session update → accounting

No look-ahead: features/regime use only candles[:i+1]. Exits use the current
bar's high/low (intrabar, standard backtest practice). Entries fill at the
current bar's close (or next open, configurable).

Accounting invariant after every fill:
    simulator == fill ledger == OMS == position manager
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

from app.accounting.ledger import FillLedger
from app.backtesting.execution import SimulatedBroker
from app.backtesting.metrics import BacktestMetrics, compute_metrics
from app.config.models import AppConfig
from app.core.ids import new_correlation_id, new_fill_id, new_order_id, new_trade_id
from app.domain.enums import Direction, OrderType, Side
from app.domain.market import Candle
from app.domain.orders import Fill, OrderIntent
from app.domain.pnl import TradeRecord
from app.features.engine import FeatureEngine
from app.orders.oms import OMS
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
    run_id: str = ""
    data_fingerprint: str = ""
    git_commit: str = ""
    config_hash: str = ""
    session_summaries: list[dict] = field(default_factory=list)
    accounting_checks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "metrics": self.metrics.to_dict(),
            "trades": self.trades,
            "signals": self.signals,
            "rejections": self.rejections,
            "entries": self.entries,
            "run_id": self.run_id,
            "data_fingerprint": self.data_fingerprint,
            "git_commit": self.git_commit,
            "config_hash": self.config_hash,
            "session_summaries": self.session_summaries,
            "accounting_checks": self.accounting_checks,
        }


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _config_hash(config: AppConfig) -> str:
    payload = config.model_dump_json(exclude={"ai": {"endpoint": {"base_url"}}})
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _data_fingerprint(candles: list[Candle]) -> str:
    if not candles:
        return "empty"
    first = candles[0]
    last = candles[-1]
    payload = f"{first.timestamp.isoformat()}:{last.timestamp.isoformat()}:{len(candles)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


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
        self.broker = SimulatedBroker(config.backtesting.execution)
        self.fill_ledger = FillLedger()
        self.oms = OMS(self.broker, duplicate_protection=True)
        self.oms.broker = self.broker  # OMS uses broker only for submit; we drive fills directly

    def run(self, candles: list[Candle]) -> BacktestResult:
        if not candles:
            return BacktestResult(metrics=BacktestMetrics())

        initial = self.config.backtesting.initial_capital
        position_manager = PositionManager(self.symbol)
        pnl = PnlEngine(self.symbol, starting_equity=initial)
        session = self._build_session()
        session.start(candles[0].timestamp)

        trades: list[dict] = []
        closed_pnls: list[float] = []
        holding_seconds: list[float] = []
        equity_curve: list[float] = []
        signals = rejections = entries = forced_exits = 0
        fees_total = 0.0
        slippage_total = 0.0
        last_trade_time: datetime | None = None
        min_candles = self.config.regime.min_candles
        accounting_checks: list[dict] = []
        session_summaries: list[dict] = []
        open_trade: dict | None = None
        position_sizes: list[float] = []
        position_values: list[float] = []
        exposure_flags: list[bool] = []

        session_day = candles[0].timestamp.date()
        for index, candle in enumerate(candles):
            if index + 1 < min_candles:
                equity_curve.append(pnl.equity)
                continue

            # --- new session day? Flatten-and-rotate -----------------------
            bar_day = candle.timestamp.date()
            if bar_day != session_day:
                if not position_manager.is_flat:
                    fill = self._exit_fill(position_manager, candle.close, candle.timestamp, "session_closeout")
                    if fill is not None:
                        self._apply_fill(fill, position_manager, pnl, is_exit=True)
                        fees_total += fill.fee
                        slippage_total += abs(fill.price - candle.close) * fill.quantity
                        realized = position_manager.position.realized_pnl - (open_trade["entry_realized"] if open_trade else 0.0)
                        closed_pnls.append(realized)
                        holding_seconds.append(max(0.0, (candle.timestamp - (open_trade["entry_time"] if open_trade else candle.timestamp)).total_seconds()))
                        forced_exits += 1
                        trades.append(self._close_trade_record(open_trade, fill, realized, "session_closeout", candle.timestamp))
                        open_trade = None
                if session.state.value != "completed":
                    session.mark_flat(success=True, note="session_rotate")
                session_summaries.append(session.build_summary(candle.timestamp, realized=sum(closed_pnls), fees=fees_total).model_dump())
                session = self._build_session()
                session.start(candle.timestamp)
                session_day = bar_day

            # --- replay clock: advance session -----------------------------
            prev_state = session.state
            session.update(candle.timestamp)
            if session.state is not prev_state:
                logger.info(
                    "SESSION_STATE_CHANGED",
                    extra={"structured": {"event": "SESSION_STATE_CHANGED", "previous": prev_state.value, "new": session.state.value}},
                )
            if session.state.value == "closeout" and open_trade is not None:
                # Session closeout: force-flatten any open position.
                exit_price = candle.close
                fill = self._exit_fill(position_manager, exit_price, candle.timestamp, "session_closeout")
                if fill is not None:
                    self._apply_fill(fill, position_manager, pnl, is_exit=True)
                    fees_total += fill.fee
                    slippage_total += abs(fill.price - candle.close) * fill.quantity
                    realized = position_manager.position.realized_pnl - (open_trade["entry_realized"])
                    closed_pnls.append(realized)
                    holding = (candle.timestamp - open_trade["entry_time"]).total_seconds()
                    holding_seconds.append(holding)
                    forced_exits += 1
                    trades.append(self._close_trade_record(open_trade, fill, realized, "session_closeout", candle.timestamp))
                    open_trade = None
                    last_trade_time = candle.timestamp
                    session_summaries.append(session.build_summary(candle.timestamp, realized=sum(closed_pnls), fees=fees_total).model_dump())
                    session.mark_flat(success=True, note="session_closeout_flatten")

            # --- features + regime (no look-ahead) ---------------------------
            window = candles[: index + 1]
            features = self.feature_engine.compute(window, timeframe=self.timeframe, now=candle.timestamp)
            regime = self.regime_engine.classify(features, now=candle.timestamp)

            # --- exit check (intrabar stop/target) --------------------------
            if not position_manager.is_flat:
                position = position_manager.position
                exit_price, exit_reason = self._check_exit(position, candle)
                if exit_price is not None:
                    fill = self._exit_fill(position_manager, exit_price, candle.timestamp, exit_reason)
                    if fill is not None:
                        self._apply_fill(fill, position_manager, pnl, is_exit=True)
                        fees_total += fill.fee
                        slippage_total += abs(fill.price - candle.close) * fill.quantity
                        realized = position_manager.position.realized_pnl - (open_trade["entry_realized"] if open_trade else 0.0)
                        closed_pnls.append(realized)
                        holding = (candle.timestamp - (open_trade["entry_time"] if open_trade else candle.timestamp)).total_seconds()
                        holding_seconds.append(max(0.0, holding))
                        if exit_reason == "forced_exit":
                            forced_exits += 1
                        trades.append(self._close_trade_record(open_trade, fill, realized, exit_reason, candle.timestamp))
                        open_trade = None
                        last_trade_time = candle.timestamp

            # --- entry check (only when flat and session allows) ------------
            if position_manager.is_flat and self.config.strategy.enabled and session.entries_allowed and session.state.value == "trading":
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
                except Exception:  # noqa: BLE001
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
                            fill_price = self._entry_price(candle)
                            side = Side.BUY if signal.direction is Direction.LONG else Side.SELL
                            order = self.broker.submit(
                                order_id=new_order_id(),
                                side=side,
                                direction=signal.direction,
                                quantity=sizing.final_quantity,
                                price=fill_price,
                                timestamp=candle.timestamp,
                                symbol=self.symbol,
                            )
                            fill = self.broker.try_fill(
                                order,
                                candle_high=candle.high,
                                candle_low=candle.low,
                                candle_close=candle.close,
                                timestamp=candle.timestamp,
                                fill_id=new_fill_id(),
                            )
                            if fill is not None:
                                self._apply_fill(fill, position_manager, pnl, is_exit=False)
                                fees_total += fill.fee
                                slippage_total += order.slippage
                                entries += 1
                                last_trade_time = candle.timestamp
                                open_trade = {
                                    "trade_id": new_trade_id(),
                                    "signal": signal,
                                    "sizing": sizing,
                                    "entry_time": candle.timestamp,
                                    "entry_price": fill.price,
                                    "entry_realized": pnl.realized,
                                    "regime": regime,
                                }

            # --- accounting invariant check --------------------------------
            ledger_net = self.fill_ledger.net_quantity(self.symbol)
            oms_net = self.oms.net_quantity(self.symbol)
            pm_net = 0.0 if position_manager.is_flat else (
                position_manager.position.quantity if position_manager.direction is Direction.LONG
                else -position_manager.position.quantity
            )
            check = {
                "bar": index,
                "timestamp": candle.timestamp.isoformat(),
                "ledger": ledger_net,
                "oms": oms_net,
                "pm": pm_net,
                "consistent": abs(ledger_net - oms_net) < 1e-9 and abs(oms_net - pm_net) < 1e-9,
            }
            accounting_checks.append(check)
            if not check["consistent"]:
                logger.critical(
                    "ACCOUNTING DIVERGENCE",
                    extra={"structured": {"event": "ACCOUNTING_DIVERGENCE", "bar": index, "ledger": ledger_net, "oms": oms_net, "pm": pm_net}},
                )

            position_manager.mark(candle.close)
            equity_curve.append(pnl.mark(position_manager.position.unrealized_pnl).equity)

            # --- exposure tracking -----------------------------------------
            if not position_manager.is_flat:
                pos = position_manager.position
                position_sizes.append(pos.quantity)
                position_values.append(pos.quantity * candle.close)
                exposure_flags.append(True)
            else:
                exposure_flags.append(False)

        # Force-close anything left open at the end of the series.
        if not position_manager.is_flat:
            position = position_manager.position
            fill = self._exit_fill(position_manager, candles[-1].close, candles[-1].timestamp, "end_of_data")
            if fill is not None:
                self._apply_fill(fill, position_manager, pnl, is_exit=True)
                fees_total += fill.fee
                slippage_total += abs(fill.price - candles[-1].close) * fill.quantity
                realized = position_manager.position.realized_pnl - (open_trade["entry_realized"] if open_trade else 0.0)
                closed_pnls.append(realized)
                holding_seconds.append(
                    max(0.0, (candles[-1].timestamp - (open_trade["entry_time"] if open_trade else candles[-1].timestamp)).total_seconds())
                )
                forced_exits += 1
                trades.append(self._close_trade_record(open_trade, fill, realized, "end_of_data", candles[-1].timestamp))
                open_trade = None

        # Final accounting invariant at the closing state.
        if accounting_checks:
            ledger_net = self.fill_ledger.net_quantity(self.symbol)
            oms_net = self.oms.net_quantity(self.symbol)
            pm_net = 0.0 if position_manager.is_flat else (
                position_manager.position.quantity if position_manager.direction is Direction.LONG
                else -position_manager.position.quantity
            )
            accounting_checks.append({
                "bar": len(candles) - 1,
                "timestamp": candles[-1].timestamp.isoformat(),
                "ledger": ledger_net,
                "oms": oms_net,
                "pm": pm_net,
                "consistent": abs(ledger_net - oms_net) < 1e-9 and abs(oms_net - pm_net) < 1e-9,
                "label": "final",
            })

        metrics = compute_metrics(
            closed_pnls=closed_pnls,
            holding_seconds=holding_seconds,
            equity_curve=equity_curve,
            initial_capital=initial,
            fees=fees_total,
            slippage=slippage_total,
            rejected_trades=rejections,
            forced_exits=forced_exits,
            position_sizes=position_sizes,
            position_values=position_values,
            exposure_flags=exposure_flags,
        )
        run_id = hashlib.sha256(
            f"{_git_commit()}:{_config_hash(self.config)}:{_data_fingerprint(candles)}".encode()
        ).hexdigest()[:16]
        return BacktestResult(
            metrics=metrics,
            trades=trades,
            equity_curve=equity_curve,
            signals=signals,
            rejections=rejections,
            entries=entries,
            run_id=run_id,
            data_fingerprint=_data_fingerprint(candles),
            git_commit=_git_commit(),
            config_hash=_config_hash(self.config),
            session_summaries=session_summaries,
            accounting_checks=accounting_checks,
        )

    # -- helpers ------------------------------------------------------------
    def _build_session(self):
        from app.sessions.manager import SessionManager
        return SessionManager(self.config.session, self.config.session_closeout)

    def _entry_price(self, candle: Candle) -> float:
        mode = self.config.backtesting.execution.entry_price
        if mode == "next_open":
            return candle.open
        return candle.close

    def _exit_fill(self, position_manager, price: float, timestamp, reason: str) -> Fill | None:
        position = position_manager.position
        side = Side.SELL if position.direction is Direction.LONG else Side.BUY
        fee_rate = self.config.backtesting.execution.fee_percent / 100.0
        qty = abs(position.quantity)
        if self.config.backtesting.execution.fee_model == "in_kind" and side is Side.BUY:
            qty = qty * (1.0 - fee_rate)
        fee = qty * price * fee_rate if (self.config.backtesting.execution.fee_model == "quote" or side is Side.SELL) else 0.0
        return Fill(
            fill_id=new_fill_id(),
            order_id=new_order_id(),
            timestamp=timestamp,
            symbol=self.symbol,
            side=side,
            quantity=round(qty, 8),
            price=price,
            fee=round(fee, 8),
        )

    def _apply_fill(self, fill: Fill, position_manager, pnl, *, is_exit: bool) -> None:
        self.fill_ledger.add_fill(fill)
        intent = OrderIntent(
            order_id=fill.order_id,
            client_order_id=fill.order_id,
            timestamp=fill.timestamp,
            symbol=self.symbol,
            side=fill.side,
            direction=Direction.LONG if fill.side is Side.BUY else Direction.SHORT,
            quantity=fill.quantity,
            order_type=OrderType.MARKET,
            signal_id=None,
            correlation_id=None,
            session_id=None,
            reason="backtest",
        )
        order = self.oms.create(intent)
        self.oms.apply_fill(order, fill)
        update = position_manager.apply_fill(fill)
        if update.realized_delta:
            pnl.add_realized(update.realized_delta)
        if fill.fee:
            pnl.add_fee(fill.fee)

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

    def _close_trade_record(self, open_trade: dict | None, fill: Fill, realized: float, reason: str, exit_time: datetime) -> dict:
        if open_trade is None:
            return {}
        signal = open_trade["signal"]
        sizing = open_trade["sizing"]
        entry_price = open_trade["entry_price"]
        direction = signal.direction
        gross = realized + fill.fee
        position_value = sizing.final_quantity * entry_price
        return {
            "trade_id": open_trade["trade_id"],
            "symbol": self.symbol,
            "side": direction.value,
            "quantity_requested": sizing.final_quantity,
            "quantity_filled": sizing.final_quantity,
            "entry_time": open_trade["entry_time"].isoformat(),
            "entry_price": round(entry_price, 4),
            "exit_time": exit_time.isoformat(),
            "exit_price": round(fill.price, 4),
            "gross_pnl": round(gross, 6),
            "fees": round(fill.fee, 6),
            "slippage": round(abs(fill.price - entry_price) * sizing.final_quantity, 6),
            "net_pnl": round(realized, 6),
            "holding_seconds": round((exit_time - open_trade["entry_time"]).total_seconds(), 2),
            "strategy": signal.strategy,
            "regime": open_trade["regime"].regime.value,
            "timeframe": self.timeframe,
            "signal": signal.reason_code.value,
            "stop": signal.stop_reference,
            "target": signal.take_profit_reference,
            "exit_reason": reason,
            "risk_per_trade": self.config.risk.risk_per_trade_percent,
            "position_value": round(position_value, 2),
            "outcome": "win" if realized > 0 else ("loss" if realized < 0 else "breakeven"),
        }
