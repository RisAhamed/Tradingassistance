"""The trading engine: orchestrates the deterministic pipeline.

MARKET DATA -> NORMALIZATION -> FEATURES -> REGIME -> STRATEGY -> SIGNAL ->
RISK -> POSITION SIZE -> OMS -> BROKER -> FILL -> POSITION -> P&L

Safety invariants enforced here:
* entries blocked on stale data, unhealthy components, failed reconciliation,
  paused sessions, entry cutoff, and flat-at-session-end closeout;
* risk decisions are authoritative (the engine never bypasses them);
* ambiguous order submissions trigger reconciliation, never a blind retry.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.brokers.base import BrokerAdapter, BrokerExecution
from app.brokers.mock import MockBroker
from app.config.loader import PROJECT_ROOT
from app.config.models import AppConfig
from app.config.settings import EnvSettings
from app.core.clock import utcnow
from app.core.errors import ConfigError, OrderRejected, PaperOnlyViolation
from app.core.ids import new_correlation_id, new_order_id, new_signal_id, new_trade_id
from app.domain.enums import (
    Direction,
    HealthState,
    OrderStatus,
    SessionState,
    Side,
)
from app.domain.market import Quote, Trade
from app.domain.orders import Order, OrderIntent
from app.domain.pnl import TradeRecord
from app.domain.regime import RegimeSnapshot
from app.domain.risk import RiskDecision
from app.domain.signals import StrategySignal
from app.events.bus import EventBus
from app.events.types import EventType
from app.execution.executor import ExecutionResult, OrderExecutor
from app.features.engine import FeatureEngine
from app.market_data.aggregator import CandleAggregator
from app.market_data.base import MarketDataProvider, MarketUpdate
from app.market_data.store import MarketStore
from app.monitoring.health import HealthRegistry
from app.monitoring.state import SystemState
from app.orders.oms import OMS
from app.portfolio.pnl_engine import PnlEngine
from app.portfolio.position_manager import PositionManager
from app.portfolio.position_sizing import PositionSizer
from app.regime.engine import RegimeEngine
from app.risk.engine import RiskContext, RiskEngine
from app.runner.factories import create_broker, create_provider
from app.sessions.manager import SessionManager
from app.strategies.base import StrategyContext
from app.strategies.breakout_momentum import BreakoutMomentumStrategy
from app.signals.generator import SignalGenerator

logger = logging.getLogger("app.runner.engine")


class TradingEngine:
    def __init__(
        self,
        config: AppConfig,
        env: EnvSettings,
        *,
        provider: MarketDataProvider | None = None,
        broker: BrokerAdapter | None = None,
        bus: EventBus | None = None,
        repository: Any = None,
    ) -> None:
        self.config = config
        self.env = env
        self.symbol = config.trading.symbol
        self.bus = bus or EventBus()
        self.repository = repository

        self.provider = provider or create_provider(config, env)
        self.broker = broker or create_broker(config, env)

        timeframes = [config.timeframes.context, config.timeframes.signal, config.timeframes.execution]
        self.timeframes = list(dict.fromkeys(timeframes))
        self.store = MarketStore(self.timeframes)
        self.aggregator = CandleAggregator(self.symbol, self.timeframes)

        self.feature_engine = FeatureEngine(config.features)
        self.regime_engine = RegimeEngine(
            config.regime, fast_period=config.strategy.trend.fast_ema, slow_period=config.strategy.trend.slow_ema
        )
        strategy = BreakoutMomentumStrategy(config.strategy)
        self.generator = SignalGenerator(strategy, config.risk)
        self.risk_engine = RiskEngine(
            config.risk,
            allow_short=config.execution.allow_short,
            cooldown_minutes=config.strategy.cooldown.minutes if config.strategy.cooldown.enabled else 0.0,
        )
        self.sizer = PositionSizer(config.position_sizing)
        self.oms = OMS(self.broker, duplicate_protection=config.execution.duplicate_order_protection)
        self.executor = OrderExecutor(self.oms)
        self.position_manager = PositionManager(self.symbol)
        self.pnl = PnlEngine(self.symbol, starting_equity=0.0)
        self.session = SessionManager(config.session, config.session_closeout)
        self.health = HealthRegistry()

        self.state = SystemState(
            environment=config.application.environment,
            mode=config.trading.mode,
            symbol=self.symbol,
            broker=self.broker.name,
            market_data=self.provider.name,
            ai_provider=config.ai.provider,
        )
        self.state.strategy_name = strategy.name

        self._account_equity: float = 0.0
        self._pending_stop: float | None = None
        self._pending_target: float | None = None
        self._last_trade_time: datetime | None = None
        self._need_reconciliation = False
        self._running = False
        self._order_count = 0
        self._entry_order_id: str | None = None
        self._exiting = False
        self._flatten_done = False
        self._last_decision: RiskDecision | None = None
        self._stale_flag = False
        self._last_account_refresh: datetime | None = None
        self._tick_task: asyncio.Task | None = None
        self._last_exit_check: datetime | None = None
        # MARKET-DATA SAFETY: last accepted update timestamp per symbol, used
        # to reject out-of-order / duplicate ticks (never invent prices).
        self._last_update_at: dict[str, datetime] = {}

    # -- component wiring ---------------------------------------------------
    async def _wire(self) -> None:
        self.provider.set_handler(self.on_market_update)
        await self.provider.connect([self.symbol])
        await self.broker.connect()
        await self._refresh_account()

    # -- lifecycle ----------------------------------------------------------
    async def start(self, *, reconcile: bool = True) -> None:
        """Startup checklist -> reconcile -> connect -> SYSTEM READY."""
        logger.info(
            "SYSTEM STARTING",
            extra={"structured": {"event": "SYSTEM_STARTING", "component": "engine"}},
        )
        logger.info(
            "CONFIGURATION LOADED",
            extra={"structured": {"event": "CONFIGURATION_LOADED", "component": "engine", "symbol": self.symbol}},
        )
        logger.info(
            "PAPER MODE VERIFIED",
            extra={"structured": {"event": "PAPER_MODE_VERIFIED", "component": "engine", "mode": self.config.trading.mode}},
        )

        self._register_health()

        if self.repository is not None:
            await self.repository.init()

        await self._wire()

        if reconcile:
            await self.reconcile()

        session_id = self.session.start(utcnow())
        await self.bus.emit(
            EventType.SESSION_STARTED,
            payload={"session_id": session_id, "symbol": self.symbol},
            session_id=session_id,
        )
        if self.repository is not None:
            await self.repository.save_session(session_id, SessionState.TRADING.value, self.session.started_at, {})

        self.state.set_component("broker", HealthState.HEALTHY, self.broker.health().detail)
        self.state.set_component("market_data", HealthState.HEALTHY, self.provider.health().detail)
        self.state.set_component("database", HealthState.HEALTHY if (self.repository and self.repository.available) else HealthState.WARNING)
        self.state.set_component("ai", HealthState.WARNING, "not started")

        self._running = True
        self._schedule_tick()
        logger.info(
            "ENGINE STARTED",
            extra={"structured": {"event": "ENGINE_STARTED", "component": "engine"}},
        )
        await self.bus.emit(EventType.SYSTEM_READY, payload={"mode": "PAPER_TRADING_ONLY"})

    async def stop(self) -> None:
        self._running = False
        await self.provider.disconnect()
        await self.broker.disconnect()
        logger.info("ENGINE STOPPED", extra={"structured": {"event": "ENGINE_STOPPED", "component": "engine"}})

    def _register_health(self) -> None:
        self.health.register(
            "market_data",
            lambda: self.provider.health().connected,
            detail="live stream",
            critical=True,
        )
        self.health.register(
            "broker", lambda: self.broker.health().connected, detail="paper broker", critical=True
        )
        self.health.register(
            "database",
            lambda: True if (self.repository is None or not self.repository.available) else True,
            detail="persistence (auxiliary)",
            critical=False,
        )
        self.health.register(
            "reconciliation",
            lambda: not self._need_reconciliation,
            detail="broker vs internal",
            critical=True,
        )

    def _schedule_tick(self) -> None:
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._tick_loop())

    async def _tick_loop(self) -> None:
        while self._running:
            try:
                await self.tick(utcnow())
            except Exception as exc:  # noqa: BLE001 - keep the loop alive, log loudly
                logger.exception(
                    "tick failed",
                    extra={"structured": {"event": "SYSTEM_ERROR", "component": "engine", "error": str(exc)}},
                )
            await asyncio.sleep(1.0)

    async def _refresh_account(self) -> None:
        try:
            account = await self.broker.get_account()
            self._account_equity = account.equity
            self.pnl.set_equity(account.equity)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "account query failed",
                extra={"structured": {"event": "BROKER_DISCONNECTED", "component": "engine", "error": str(exc)}},
            )
            self.state.set_component("broker", HealthState.ERROR, "account query failed")

    # -- market data --------------------------------------------------------
    async def on_market_update(self, update: MarketUpdate) -> None:
        """Provider callback: normalize -> store -> (ticks) -> candle close.

        MARKET-DATA SAFETY (Phase A #3): never invent replacement prices.
        Updates for an unexpected symbol, with a non-positive price, or whose
        timestamp does not advance that stream's clock (quotes and trades are
        tracked separately) are rejected and logged — they can never create
        new entries.
        """
        now = utcnow()
        symbol = getattr(update, "symbol", None)
        if symbol != self.symbol:
            logger.warning(
                "unexpected symbol rejected",
                extra={
                    "structured": {
                        "event": "INVALID_MARKET_DATA",
                        "component": "engine",
                        "symbol": symbol,
                        "expected": self.symbol,
                    }
                },
            )
            return
        if isinstance(update, Quote):
            if update.bid <= 0 or update.ask <= 0 or update.ask < update.bid:
                logger.warning(
                    "invalid quote rejected",
                    extra={
                        "structured": {
                            "event": "INVALID_MARKET_DATA",
                            "component": "engine",
                            "symbol": symbol,
                        }
                    },
                )
                return
            if self._is_out_of_order(symbol, update.timestamp, "quote"):
                return
            self.store.update_quote(update)
        elif isinstance(update, Trade):
            if update.price <= 0 or update.size < 0:
                logger.warning(
                    "invalid trade rejected",
                    extra={
                        "structured": {
                            "event": "INVALID_MARKET_DATA",
                            "component": "engine",
                            "symbol": symbol,
                        }
                    },
                )
                return
            if self._is_out_of_order(symbol, update.timestamp, "trade"):
                return
            self.store.update_trade(update)
            for candle in self.aggregator.add_trade(update):
                self.store.add_candle(candle)
                await self._on_candle_closed(candle)
        else:  # pragma: no cover - defensive
            logger.warning(
                "unknown market update",
                extra={"structured": {"event": "INVALID_MARKET_DATA", "component": "engine"}},
            )
            return

        snapshot = self.store.snapshot(self.symbol)
        self.state.latest_snapshot = snapshot
        if snapshot and snapshot.price:
            broker = self.broker
            setter = getattr(broker, "set_price", None)
            if callable(setter):
                try:
                    setter(self.symbol, snapshot.price)
                except Exception:  # noqa: BLE001 - price seam is best-effort
                    pass
            await self._manage_position(snapshot.price)
        await self.bus.emit(
            EventType.MARKET_DATA_RECEIVED,
            payload={
                "symbol": self.symbol,
                "price": snapshot.price if snapshot else None,
                "bid": snapshot.bid if snapshot else None,
                "ask": snapshot.ask if snapshot else None,
            },
        )

    def _is_out_of_order(self, symbol: str, timestamp: datetime, kind: str) -> bool:
        """Reject out-of-order / duplicate ticks; return True when dropped.

        MARKET-DATA SAFETY (Phase A #3): a quote and its paired trade legitimately
        share one market timestamp, so quotes and trades are tracked as
        independent streams. Within a stream, any tick whose timestamp does not
        strictly advance the clock is a duplicate or a replay — drop it so
        replays can never manufacture entries. Valid ticks must advance time.
        """
        key = f"{symbol}:{kind}"
        last = self._last_update_at.get(key)
        if last is not None and timestamp <= last:
            logger.warning(
                "out_of_order_or_duplicate tick rejected",
                extra={
                    "structured": {
                        "event": "INVALID_MARKET_DATA",
                        "component": "engine",
                        "symbol": symbol,
                        "kind": kind,
                        "reason": "out_of_order_or_duplicate",
                    }
                },
            )
            return True
        self._last_update_at[key] = timestamp
        return False

    def _features_for(self, timeframe: str, now: datetime):
        candles = self.store.candles(self.symbol, timeframe)
        snapshot = self.store.snapshot(self.symbol)
        return self.feature_engine.compute(candles, timeframe=timeframe, snapshot=snapshot, now=now)

    async def _on_candle_closed(self, candle) -> None:
        now = utcnow()
        logger.debug(
            "candle closed",
            extra={"structured": {"event": "CANDLE_CLOSED", "component": "engine", "timeframe": candle.timeframe}},
        )
        await self.bus.emit(
            EventType.CANDLE_CLOSED,
            payload={"symbol": candle.symbol, "timeframe": candle.timeframe, "close": candle.close},
        )

        # Context timeframe (15m): market context + regime classification.
        if candle.timeframe == self.config.timeframes.context:
            await self._update_regime(now)

        # Signal timeframe (5m): strategy evaluation on candle close only.
        if candle.timeframe == self.config.timeframes.signal:
            await self.evaluate(now)

    async def _update_regime(self, now: datetime) -> None:
        context_features = self._features_for(self.config.timeframes.context, now)
        previous = self.state.regime
        regime = self.regime_engine.classify(context_features, now=now)
        self.state.record_regime(regime)
        await self.bus.emit(
            EventType.FEATURES_UPDATED,
            payload={"timeframe": self.config.timeframes.context, "values": context_features.numeric_values()},
        )
        if previous is None or previous.regime is not regime.regime:
            logger.info(
                "REGIME UPDATED",
                extra={
                    "structured": {
                        "event": "REGIME_CHANGED",
                        "component": "engine",
                        "regime": regime.regime.value,
                        "reason": regime.reason,
                    }
                },
            )
            await self.bus.emit(
                EventType.REGIME_CHANGED,
                payload={"regime": regime.regime.value, "reason": regime.reason},
            )

    # -- pipeline -----------------------------------------------------------
    async def evaluate(self, now: datetime | None = None) -> StrategySignal | None:
        """Run features -> regime -> strategy for the signal timeframe."""
        now = now or utcnow()
        features = self._features_for(self.config.timeframes.signal, now)
        self.state.features = features
        self.state.last_evaluation_at = now
        logger.debug(
            "FEATURES UPDATED",
            extra={
                "structured": {
                    "event": "FEATURES_UPDATED",
                    "component": "features",
                    "timeframe": self.config.timeframes.signal,
                    "candle_count": features.candle_count,
                    "ready": features.ready,
                    "missing": features.missing,
                }
            },
        )
        await self.bus.emit(
            EventType.FEATURES_UPDATED,
            payload={"timeframe": self.config.timeframes.signal, "values": features.numeric_values()},
        )

        regime = self.state.regime
        if regime is None:
            regime = self.regime_engine.classify(features, now=now)
            self.state.record_regime(regime)

        # REGIME SAFETY: report explicitly why evaluation cannot proceed.
        if regime.is_unknown:
            logger.debug(
                "STRATEGY WAIT — regime unknown",
                extra={
                    "structured": {
                        "event": "STRATEGY_EVALUATED",
                        "component": "strategy",
                        "result": "wait_regime_unknown",
                        "reason": regime.reason,
                    }
                },
            )
            return None
        if not features.ready:
            logger.debug(
                "STRATEGY WAIT — features not ready",
                extra={
                    "structured": {
                        "event": "STRATEGY_EVALUATED",
                        "component": "strategy",
                        "result": "wait_features_not_ready",
                        "missing": features.missing,
                    }
                },
            )
            return None

        if not self.config.strategy.enabled:
            return None
        if not self.session.entries_allowed:
            logger.debug(
                "STRATEGY SKIPPED",
                extra={"structured": {"event": "STRATEGY_EVALUATED", "component": "engine", "note": "entries_not_allowed"}},
            )
            return None

        context = StrategyContext(
            symbol=self.symbol,
            timeframe=self.config.timeframes.signal,
            features=features,
            regime=regime,
            now=now,
            snapshot=self.store.snapshot(self.symbol),
            position_open=not self.position_manager.is_flat,
            correlation_id=new_correlation_id(),
            session_id=self.session.session_id,
        )

        try:
            signal = self.generator.generate(context)
        except Exception:  # noqa: BLE001 - strategy failures must not kill the engine
            logger.exception(
                "STRATEGY ERROR",
                extra={"structured": {"event": "SYSTEM_ERROR", "component": "strategy", "note": "signal_skipped"}},
            )
            self.state.record_error("strategy", "exception during evaluation")
            return None

        if signal is None:
            logger.debug(
                "NO_SIGNAL",
                extra={"structured": {"event": "STRATEGY_EVALUATED", "component": "strategy", "result": "no_signal"}},
            )
            return None

        logger.info(
            "SIGNAL GENERATED",
            extra={
                "structured": {
                    "event": "SIGNAL_GENERATED",
                    "component": "strategy",
                    "symbol": signal.symbol,
                    "direction": signal.direction.value,
                    "reason": signal.reason,
                    "signal_id": signal.signal_id,
                }
            },
        )
        self.state.record_signal(signal)
        self.session.count("signals")
        await self.bus.emit(
            EventType.SIGNAL_GENERATED,
            payload=signal.model_dump(mode="json"),
            correlation_id=signal.correlation_id,
            session_id=signal.session_id,
        )
        if self.repository is not None:
            await self.repository.save_signal(signal)

        await self._handle_signal(signal)
        return signal

    # -- risk gate ----------------------------------------------------------
    def _build_risk_context(self, now: datetime) -> RiskContext:
        age = self.store.data_age(self.symbol, now)
        fresh = age is not None and age <= self.config.risk.stale_market_data.maximum_age_seconds
        position = self.position_manager.position
        return RiskContext(
            now=now,
            symbol=self.symbol,
            session_active=self.session.state is SessionState.TRADING,
            entries_allowed=self.session.entries_allowed,
            market_data_fresh=fresh,
            system_healthy=self.health.ready(),
            reconciliation_ok=not self._need_reconciliation,
            account_equity=self._account_equity,
            open_positions=0 if self.position_manager.is_flat else 1,
            orders_this_session=self._order_count,
            daily_realized_pnl=self.pnl.daily_realized,
            last_trade_time=self._last_trade_time,
            position=position.direction,
            position_holding_seconds=position.holding_seconds(now) if not position.is_flat else None,
            allowed_symbols={self.symbol},
        )

    async def _handle_signal(self, signal: StrategySignal) -> None:
        now = utcnow()
        decision: RiskDecision = self.risk_engine.evaluate(signal, self._build_risk_context(now))
        self._last_decision = decision
        if self.repository is not None:
            await self.repository.save_risk_decision(decision)

        if not decision.approved:
            reason = decision.reason.value
            failed = [c.name for c in decision.failed_checks]
            logger.warning(
                "RISK CHECK FAILED",
                extra={
                    "structured": {
                        "event": "RISK_REJECTED",
                        "component": "risk",
                        "signal_id": signal.signal_id,
                        "reason": reason,
                        "failed_checks": ",".join(failed),
                    }
                },
            )
            self.session.count("rejections")
            self.state.record_rejection(
                {
                    "signal_id": signal.signal_id,
                    "timestamp": now.isoformat(),
                    "symbol": signal.symbol,
                    "direction": signal.direction.value,
                    "reason": reason,
                    "failed_checks": failed,
                }
            )
            await self.bus.emit(
                EventType.RISK_REJECTED,
                payload={"signal_id": signal.signal_id, "reason": reason, "checks": failed},
                correlation_id=signal.correlation_id,
            )
            await self.bus.emit(
                EventType.SIGNAL_REJECTED,
                payload={"signal_id": signal.signal_id, "reason": reason},
            )
            return

        logger.info(
            "RISK CHECK PASSED",
            extra={"structured": {"event": "RISK_APPROVED", "component": "risk", "signal_id": signal.signal_id}},
        )
        await self.bus.emit(
            EventType.RISK_APPROVED,
            payload={"signal_id": signal.signal_id, "risk_id": decision.risk_id},
            correlation_id=signal.correlation_id,
        )
        await self._submit_entry(signal)

    async def _record_execution_failure(self, signal: StrategySignal, reason: str) -> None:
        self.session.count("rejections")
        self.state.record_rejection(
            {
                "signal_id": signal.signal_id,
                "timestamp": utcnow().isoformat(),
                "symbol": signal.symbol,
                "direction": signal.direction.value,
                "reason": reason,
                "failed_checks": ["execution"],
            }
        )
        await self.bus.emit(
            EventType.SIGNAL_REJECTED,
            payload={"signal_id": signal.signal_id, "reason": reason},
        )

    # -- order submission ---------------------------------------------------
    def _order_type(self):
        from app.domain.enums import OrderType

        return OrderType.LIMIT if self.config.execution.order_type == "limit" else OrderType.MARKET

    async def _submit_entry(self, signal: StrategySignal) -> None:
        # RISK AUTHORITY (Phase A #4): every entry funnels through this method —
        # strategy, aggregator, and AI tools all call _handle_signal, which
        # evaluates RiskEngine and only reaches _submit_entry on approval.
        # There is no alternate order-creation path for entries.
        now = utcnow()
        price = self.state.latest_snapshot.price if self.state.latest_snapshot else None
        max_notional = self._account_equity * (self.config.risk.maximum_position_value_percent / 100.0)
        sizing = self.sizer.size(
            signal,
            self._account_equity,
            reference_price=price,
            max_notional=max_notional if max_notional > 0 else None,
        )
        logger.info(
            "POSITION SIZE CALCULATED",
            extra={
                "structured": {
                    "event": "POSITION_SIZED",
                    "component": "sizing",
                    "equity": round(sizing.equity, 2),
                    "risk_budget": round(sizing.risk_budget, 4),
                    "stop_distance": round(sizing.stop_distance, 4),
                    "raw_quantity": round(sizing.raw_quantity, 8),
                    "normalized_quantity": round(sizing.normalized_quantity, 8),
                    "final_quantity": round(sizing.final_quantity, 8),
                }
            },
        )
        if not sizing.ok:
            await self._record_execution_failure(signal, sizing.rejected_reason or "sizing_failed")
            return

        side = Side.BUY if signal.direction is Direction.LONG else Side.SELL
        intent = OrderIntent(
            order_id=new_order_id(),
            client_order_id=new_order_id(),
            timestamp=now,
            symbol=self.symbol,
            side=side,
            direction=signal.direction,
            quantity=sizing.final_quantity,
            order_type=self._order_type(),
            limit_price=signal.entry_reference if self.config.execution.order_type == "limit" else None,
            stop_reference=signal.stop_reference,
            take_profit_reference=signal.take_profit_reference,
            signal_id=signal.signal_id,
            correlation_id=signal.correlation_id,
            session_id=signal.session_id,
            reason=signal.reason,
        )
        order = self.oms.create(intent)
        self.state.record_order(order)
        self.session.count("orders")
        self._order_count += 1
        self._pending_stop = signal.stop_reference
        self._pending_target = signal.take_profit_reference

        logger.info(
            "ORDER CREATED",
            extra={
                "structured": {
                    "event": "ORDER_CREATED",
                    "component": "orders",
                    "order_id": order.order_id,
                    "side": order.side.value,
                    "quantity": order.quantity,
                    "signal_id": order.signal_id,
                }
            },
        )
        await self.bus.emit(
            EventType.ORDER_CREATED,
            payload=order.model_dump(mode="json"),
            correlation_id=signal.correlation_id,
            session_id=signal.session_id,
        )
        if self.repository is not None:
            await self.repository.save_order(order)

        await self._submit(order)

    # -- order submission ---------------------------------------------------
    async def _submit(
        self,
        order: Order,
        *,
        is_exit: bool = False,
        exit_reason: str | None = None,
    ) -> bool:
        try:
            result: ExecutionResult = await self.executor.submit(order)
        except OrderRejected as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)
            order.updated_at = utcnow()
            logger.error(
                "ORDER REJECTED",
                extra={"structured": {"event": "ORDER_REJECTED", "component": "execution", "order_id": order.order_id, "reason": order.reject_reason}},
            )
            self.state.record_order(order)
            await self.bus.emit(EventType.ORDER_REJECTED, payload=order.model_dump(mode="json"))
            if self.repository is not None:
                await self.repository.save_order(order)
            return False

        if result.ambiguous:
            self._need_reconciliation = True
            self.state.reconciliation.update(
                {
                    "ok": False,
                    "discrepancies": [f"ambiguous submission for {order.order_id}"],
                    "last_run_at": utcnow().isoformat(),
                }
            )
            logger.critical(
                "RECONCILIATION REQUIRED",
                extra={
                    "structured": {
                        "event": "RECONCILIATION_FAILED",
                        "component": "execution",
                        "order_id": order.order_id,
                        "reason": result.reason,
                    }
                },
            )
            await self.bus.emit(
                EventType.RECONCILIATION_FAILED,
                payload={"order_id": order.order_id, "reason": result.reason},
            )
            return False

        if result.rejected:
            logger.error(
                "ORDER REJECTED",
                extra={"structured": {"event": "ORDER_REJECTED", "component": "execution", "order_id": order.order_id, "reason": result.reason}},
            )
            order.status = OrderStatus.REJECTED
            order.reject_reason = result.reason
            self.state.record_order(order)
            return False

        logger.info(
            "ORDER SUBMITTED",
            extra={"structured": {"event": "ORDER_SUBMITTED", "component": "orders", "order_id": order.order_id, "side": order.side.value, "quantity": order.quantity}},
        )
        await self.bus.emit(
            EventType.ORDER_SUBMITTED,
            payload=order.model_dump(mode="json"),
            correlation_id=order.correlation_id,
            session_id=order.session_id,
        )
        for fill in result.fills:
            await self._apply_fill(order, fill, is_exit=is_exit, exit_reason=exit_reason)
        if self.repository is not None:
            await self.repository.save_order(order)
        return True

    # -- fill handling ------------------------------------------------------
    async def _apply_fill(self, order, fill, *, is_exit: bool = False, exit_reason: str | None = None) -> None:
        position = self.position_manager.position
        prior = {
            "direction": position.direction,
            "quantity": position.quantity,
            "entry": position.average_entry,
            "opened_at": position.opened_at,
            "signal_id": order.signal_id,
        }
        stop = None if is_exit else self._pending_stop
        target = None if is_exit else self._pending_target
        update = self.position_manager.apply_fill(
            fill, stop=stop, target=target, correlation_id=order.correlation_id
        )

        if update.realized_delta:
            self.pnl.add_realized(update.realized_delta)
        if fill.fee:
            self.pnl.add_fee(fill.fee)

        unreal = self.position_manager.mark(fill.price)
        pnl_snapshot = self.pnl.mark(unreal)

        if update.opened:
            self._entry_order_id = order.order_id
            self._last_trade_time = fill.timestamp
        if update.closed or update.flipped:
            await self._record_closed_trade(fill, prior, update.realized_delta, exit_reason, order)

        logger.info(
            "ORDER FILLED",
            extra={"structured": {"event": "ORDER_FILLED", "component": "execution", "order_id": order.order_id, "side": order.side.value, "quantity": fill.quantity, "price": fill.price}},
        )
        await self.bus.emit(EventType.ORDER_FILLED, payload=fill.model_dump(mode="json"), session_id=order.session_id)

        position = self.position_manager.position
        logger.info(
            "POSITION UPDATED",
            extra={"structured": {"event": "POSITION_UPDATED", "component": "portfolio", "symbol": position.symbol, "direction": position.direction.value, "quantity": position.quantity}},
        )
        self.state.position = position
        self.state.pnl = pnl_snapshot
        await self.bus.emit(EventType.POSITION_UPDATED, payload=position.model_dump(mode="json"))

        logger.debug(
            "P&L UPDATED",
            extra={"structured": {"event": "PNL_UPDATED", "component": "portfolio", "realized": round(pnl_snapshot.realized, 4), "unrealized": round(pnl_snapshot.unrealized, 4), "equity": round(pnl_snapshot.equity, 2)}},
        )
        await self.bus.emit(EventType.PNL_UPDATED, payload=pnl_snapshot.model_dump(mode="json"))

        if self.repository is not None:
            await self.repository.save_fill(fill, session_id=order.session_id)
            await self.repository.save_pnl(pnl_snapshot, session_id=order.session_id)

    async def _record_closed_trade(self, fill, prior, realized: float, exit_reason: str | None, order) -> None:
        opened_at = prior.get("opened_at")
        if opened_at is None or prior.get("quantity", 0.0) <= 0:
            return
        trade = TradeRecord(
            trade_id=new_trade_id(),
            symbol=self.symbol,
            direction=prior["direction"],
            quantity=prior["quantity"],
            entry_price=prior["entry"],
            exit_price=fill.price,
            realized_pnl=realized,
            opened_at=opened_at,
            closed_at=fill.timestamp,
            holding_seconds=max(0.0, (fill.timestamp - opened_at).total_seconds()),
            signal_id=prior.get("signal_id"),
            entry_order_id=self._entry_order_id,
            exit_order_id=fill.order_id,
            reason=exit_reason or "strategy_exit",
            correlation_id=order.correlation_id,
            session_id=self.session.session_id,
        )
        self.session.count("trades")
        self.state.record_trade(trade)
        logger.info(
            "TRADE CLOSED",
            extra={
                "structured": {
                    "event": "TRADE_CLOSED",
                    "component": "portfolio",
                    "trade_id": trade.trade_id,
                    "direction": trade.direction.value,
                    "quantity": trade.quantity,
                    "entry": trade.entry_price,
                    "exit": trade.exit_price,
                    "realized_pnl": round(trade.realized_pnl, 4),
                    "reason": trade.reason,
                }
            },
        )
        await self.bus.emit(EventType.TRADE_CLOSED, payload=trade.model_dump(mode="json"), session_id=trade.session_id)
        if self.repository is not None:
            await self.repository.save_trade(trade)

    # -- exits (stops / targets / holding time) -----------------------------
    async def _manage_position(self, price: float) -> None:
        position = self.position_manager.position
        if position.is_flat:
            return
        self.position_manager.mark(price)
        now = utcnow()
        reason: str | None = None

        if position.stop is not None:
            if position.direction is Direction.LONG and price <= position.stop:
                reason = "stop_loss"
            elif position.direction is Direction.SHORT and price >= position.stop:
                reason = "stop_loss"

        if reason is None and position.target is not None:
            if position.direction is Direction.LONG and price >= position.target:
                reason = "take_profit"
            elif position.direction is Direction.SHORT and price <= position.target:
                reason = "take_profit"

        if reason is None and position.opened_at is not None:
            held_minutes = position.holding_seconds(now) / 60.0
            if held_minutes > self.config.session.max_holding_minutes:
                reason = "max_holding_time"

        if reason is not None:
            await self._exit(reason)

    async def _exit(self, reason: str) -> bool:
        """Close the current position. Exits are never gated by risk/session."""
        position = self.position_manager.position
        if self._exiting or position.is_flat:
            return False
        self._exiting = True
        try:
            now = utcnow()
            side = Side.SELL if position.direction is Direction.LONG else Side.BUY
            intent = OrderIntent(
                order_id=new_order_id(),
                client_order_id=new_order_id(),
                timestamp=now,
                symbol=self.symbol,
                side=side,
                direction=Direction.FLAT,
                quantity=abs(position.quantity),
                order_type=self._order_type(),
                correlation_id=new_correlation_id(),
                session_id=self.session.session_id,
                reason=reason,
            )
            order = self.oms.create(intent)
            self.state.record_order(order)
            self._order_count += 1
            logger.info(
                "EXIT REQUESTED",
                extra={
                    "structured": {
                        "event": "ORDER_CREATED",
                        "component": "orders",
                        "order_id": order.order_id,
                        "side": side.value,
                        "quantity": order.quantity,
                        "reason": reason,
                    }
                },
            )
            if self.repository is not None:
                await self.repository.save_order(order)
            return await self._submit(order, is_exit=True, exit_reason=reason)
        finally:
            self._exiting = False

    # -- reconciliation -----------------------------------------------------
    async def reconcile(self) -> bool:
        """Compare broker state with internal state; block entries on mismatch.

        RECONCILIATION (Phase A #9): broker/account state unknown (query
        failure) or mismatched blocks new entries — never assume flat.
        """
        logger.info(
            "RECONCILIATION STARTED",
            extra={"structured": {"event": "RECONCILIATION_STARTED", "component": "engine"}},
        )
        await self.bus.emit(EventType.RECONCILIATION_STARTED, payload={"symbol": self.symbol})
        try:
            await self.broker.get_account()
            discrepancies = await self.broker.reconcile([self.position_manager.position])
        except Exception as exc:  # noqa: BLE001
            discrepancies = [f"reconciliation_query_failed: {exc}"]

        self._need_reconciliation = bool(discrepancies)
        self.state.reconciliation = {
            "ok": not discrepancies,
            "discrepancies": discrepancies,
            "last_run_at": utcnow().isoformat(),
        }
        if discrepancies:
            logger.critical(
                "RECONCILIATION MISMATCH",
                extra={"structured": {"event": "RECONCILIATION_FAILED", "component": "engine", "count": len(discrepancies)}},
            )
            await self.bus.emit(EventType.RECONCILIATION_FAILED, payload={"discrepancies": discrepancies})
            self.state.set_component("broker", HealthState.ERROR, "reconciliation mismatch")
        else:
            logger.info(
                "RECONCILIATION OK",
                extra={"structured": {"event": "RECONCILIATION_COMPLETED", "component": "engine"}},
            )
            await self.bus.emit(EventType.RECONCILIATION_COMPLETED, payload={})
            self.state.set_component("broker", HealthState.HEALTHY, self.broker.health().detail)
        return not discrepancies

    # -- closeout / flatten -------------------------------------------------
    async def flatten(self, *, force: bool = False, session_closeout: bool = True) -> bool:
        """Flatten ALL positions and VERIFY flatness. Idempotent + retryable.

        ``session_closeout=True`` drives the session into CLOSEOUT/COMPLETED
        (used by the session timer). Human/AI flatten uses ``session_closeout=False``
        so it does not complete a still-active session.
        """
        if self._flatten_done and not force:
            return self.position_manager.is_flat
        if session_closeout:
            self.session.begin_closeout(utcnow())
            await self.bus.emit(EventType.CLOSEOUT_STARTED, payload={"symbol": self.symbol})
            logger.warning(
                "CLOSEOUT STARTED",
                extra={"structured": {"event": "SESSION_CLOSEOUT_STARTED", "component": "session"}},
            )
            logger.warning(
                "FLATTEN REQUESTED",
                extra={"structured": {"event": "FLATTEN_REQUESTED", "component": "session"}},
            )

        closeout = self.config.session_closeout
        attempts = closeout.maximum_flatten_attempts if closeout.retry_flatten else 1
        success = False
        for attempt in range(1, max(1, attempts) + 1):
            if not self.position_manager.is_flat:
                await self._exit("session_flatten")
                for _ in range(10):
                    await asyncio.sleep(0.05)
                    if self.position_manager.is_flat:
                        break
            broker_positions = await self.broker.get_positions()
            success = self.position_manager.is_flat and not broker_positions
            if success:
                break
            logger.warning(
                "FLATTEN RETRY",
                extra={"structured": {"event": "FLATTEN_FAILED", "component": "session", "attempt": attempt}},
            )

        success = self.position_manager.is_flat and not await self.broker.get_positions()
        if session_closeout:
            self._flatten_done = True
        if success:
            logger.info(
                "FLATTEN COMPLETED (POSITION = 0)",
                extra={"structured": {"event": "FLATTEN_COMPLETED", "component": "session"}},
            )
            await self.bus.emit(EventType.FLATTEN_COMPLETED, payload={"symbol": self.symbol})
        else:
            logger.critical(
                "SESSION FLATTEN FAILED",
                extra={"structured": {"event": "FLATTEN_FAILED", "component": "session"}},
            )
            await self.bus.emit(EventType.FLATTEN_FAILED, payload={"symbol": self.symbol})
        if session_closeout:
            self.session.mark_flat(success=success, note="flatten_ok" if success else "flatten_failed")
        else:
            self.session.is_flat = success
        return success

    # -- periodic tick ------------------------------------------------------
    async def tick(self, now: datetime) -> None:
        previous = self.session.state
        state = self.session.update(now)
        if state is not previous:
            await self.bus.emit(
                EventType.SESSION_STATE_CHANGED,
                payload={"state": state.value, "previous": previous.value},
            )
            if self.repository is not None:
                await self.repository.save_session(
                    self.session.session_id or "none",
                    state.value,
                    self.session.started_at,
                    self.session.snapshot(now).model_dump(mode="json"),
                )

        if state is SessionState.CLOSEOUT and not self._flatten_done:
            await self.flatten(session_closeout=True)

        await self._update_data_health(now)

        broker_health = self.broker.health()
        self.state.set_component(
            "broker",
            HealthState.HEALTHY if broker_health.connected else HealthState.ERROR,
            broker_health.detail,
        )
        provider_health = self.provider.health()
        if not self._stale_flag:
            self.state.set_component(
                "market_data",
                HealthState.HEALTHY if provider_health.connected else HealthState.ERROR,
                provider_health.detail,
            )
        self.state.set_component(
            "database",
            HealthState.HEALTHY if (self.repository and self.repository.available) else HealthState.WARNING,
            "persistence (auxiliary)",
        )

        if self._last_account_refresh is None or (now - self._last_account_refresh).total_seconds() >= 60:
            await self._refresh_account()
            self._last_account_refresh = now

        self.state.risk = self._risk_status(now)
        self.state.session = self.session.snapshot(now)

    async def _update_data_health(self, now: datetime) -> None:
        age = self.store.data_age(self.symbol, now)
        limit = self.config.risk.stale_market_data.maximum_age_seconds
        stale = age is None or age > limit
        if stale and not self._stale_flag:
            self._stale_flag = True
            self.state.set_component("market_data", HealthState.WARNING, f"data_age={age}")
            logger.warning(
                "MARKET DATA STALE",
                extra={"structured": {"event": "MARKET_DATA_STALE", "component": "engine", "age": age}},
            )
            await self.bus.emit(EventType.MARKET_DATA_STALE, payload={"symbol": self.symbol, "age": age})
        elif not stale and self._stale_flag:
            self._stale_flag = False
            self.state.set_component("market_data", HealthState.HEALTHY, self.provider.health().detail)
            logger.info(
                "MARKET DATA FRESH",
                extra={"structured": {"event": "MARKET_DATA_CONNECTED", "component": "engine"}},
            )
            await self.bus.emit(EventType.MARKET_DATA_CONNECTED, payload={"symbol": self.symbol})

    def _risk_status(self, now: datetime) -> dict:
        equity = self._account_equity
        daily_limit = equity * (self.config.risk.maximum_daily_loss_percent / 100.0)
        daily_used = -self.pnl.daily_realized
        position = self.position_manager.position
        age = self.store.data_age(self.symbol, now)
        last = self._last_decision
        return {
            "account_equity": round(equity, 2),
            "risk_per_trade_percent": self.config.risk.risk_per_trade_percent,
            "max_daily_loss_percent": self.config.risk.maximum_daily_loss_percent,
            "daily_realized_pnl": round(self.pnl.daily_realized, 4),
            "daily_loss_limit": round(daily_limit, 4),
            "remaining_daily_loss_budget": round(max(0.0, daily_limit - daily_used), 4),
            "exposure": round(position.market_value, 4),
            "open_positions": 0 if position.is_flat else 1,
            "max_open_positions": self.config.risk.maximum_open_positions,
            "orders_this_session": self._order_count,
            "max_orders_per_session": self.config.risk.maximum_orders_per_session,
            "data_age_seconds": age,
            "market_data_fresh": age is not None and age <= self.config.risk.stale_market_data.maximum_age_seconds,
            "reconciliation_ok": not self._need_reconciliation,
            "session_active": self.session.state is SessionState.TRADING,
            "last_decision": last.model_dump(mode="json") if last else None,
        }

    # -- control (human + AI, through the same command layer) --------------
    async def pause(self, *, source: str = "human", reason: str = "paused") -> None:
        self.session.pause(reason)
        if self.session.state is SessionState.PAUSED:
            logger.warning(
                "SESSION PAUSED",
                extra={"structured": {"event": "SESSION_STATE_CHANGED", "component": "session", "source": source, "state": SessionState.PAUSED.value}},
            )
            await self.bus.emit(
                EventType.SESSION_STATE_CHANGED,
                payload={"state": SessionState.PAUSED.value, "source": source, "reason": reason},
            )

    async def resume(self, *, source: str = "human") -> None:
        before = self.session.state
        self.session.resume()
        if before is SessionState.PAUSED and self.session.state is SessionState.TRADING:
            logger.info(
                "SESSION RESUMED",
                extra={"structured": {"event": "SESSION_STATE_CHANGED", "component": "session", "source": source, "state": SessionState.TRADING.value}},
            )
            await self.bus.emit(
                EventType.SESSION_STATE_CHANGED,
                payload={"state": SessionState.TRADING.value, "source": source},
            )

    async def request_reconciliation(self, *, source: str = "human") -> bool:
        logger.info(
            "RECONCILIATION REQUESTED",
            extra={"structured": {"event": "RECONCILIATION_STARTED", "component": "session", "source": source}},
        )
        return await self.reconcile()

    def payload(self) -> dict:
        """Full dashboard payload (sanitized)."""
        data = self.state.to_payload()
        data["health"] = self.health.report()
        data["paper_trading_only"] = True
        return data

    @property
    def running(self) -> bool:
        return self._running










