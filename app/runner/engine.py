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
from app.market_data.history import (
    AlpacaHistoricalDataClient,
    compute_warmup_requirement,
    timeframe_minutes,
)
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
        self.store = MarketStore(self.timeframes, max_candles=2000)
        # Phase C: candles whose bucket just opened (drained per tick).
        self._candle_started: list = []
        self.aggregator = CandleAggregator(
            self.symbol,
            self.timeframes,
            on_candle_started=self._candle_started.append,
        )
        # Phase C: historical warm-up state and the live handoff boundary.
        self.history_client = AlpacaHistoricalDataClient(config, env)
        self.warmup: dict = self._warmup_state(status="pending", reason=None)
        self._last_historical_at: datetime | None = None
        self._live_handoff_done = False
        self._data_gap_ok = True

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
    @property
    def execution_enabled(self) -> bool:
        """Fail-closed master gate for order creation and submission.

        PHASE B: when False, market data / features / regime / strategy /
        signals / risk all keep running, but nothing may reach a broker. A
        missing configuration attribute defaults to disabled.
        """
        return bool(getattr(self.config.execution, "enabled", False))

    def execution_status(self) -> dict:
        enabled = self.execution_enabled
        return {
            "enabled": enabled,
            "status": "ENABLED" if enabled else "DISABLED",
            "gate": "execution.enabled",
            "reason": None if enabled else "execution.enabled=false",
            "market_data": "ENABLED",
            "features": "ENABLED",
            "regime": "ENABLED",
            "strategy": "ENABLED",
            "signal_generation": "ENABLED",
            "risk_evaluation": "ENABLED",
            "order_creation": "ENABLED" if enabled else "DISABLED",
            "broker_contact": "ENABLED" if enabled else "DISABLED",
        }

    async def _wire(self) -> None:
        self.provider.set_handler(self.on_market_update)
        await self.provider.connect([self.symbol])
        if self.execution_enabled:
            await self.broker.connect()
        else:
            # EXECUTION SAFETY (Phase B): with execution disabled we never touch a
            # broker endpoint at all — not even a read — so "no order can be
            # submitted" is provable rather than merely intended.
            logger.warning(
                "EXECUTION DISABLED — broker not connected",
                extra={
                    "structured": {
                        "event": "EXECUTION_DISABLED",
                        "component": "engine",
                        "reason": "execution.enabled=false",
                    }
                },
            )
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
        if not self.execution_enabled:
            # PHASE B: the whole pipeline runs, only order submission is gated.
            logger.warning(
                "EXECUTION DISABLED",
                extra={
                    "structured": {
                        "event": "EXECUTION_DISABLED",
                        "component": "engine",
                        "reason": "execution.enabled=false",
                        "orders": "disabled",
                    }
                },
            )
            await self.bus.emit(
                EventType.EXECUTION_DISABLED, payload={"reason": "execution.enabled=false", "orders": "disabled"}
            )

        self._register_health()

        if self.repository is not None:
            await self.repository.init()

        # Phase C: warm up from history BEFORE the live stream starts, so the
        # pipeline is strategy-ready immediately and the live feed only appends.
        warm = await self.warm_up()
        history = self.config.market_data.history
        if not warm and history.required and history.provider != "none":
            logger.critical(
                "WARMUP REQUIRED BUT FAILED — system stays not-ready",
                extra={
                    "structured": {
                        "event": "WARMUP_FAILED",
                        "component": "engine",
                        "reason": self.warmup.get("reason"),
                    }
                },
            )

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
            "broker",
            # EXECUTION SAFETY: with execution disabled the broker is never
            # connected, so it must not count as a critical failure — the whole
            # point is that trading data flow continues without execution.
            lambda: True if not self.execution_enabled else self.broker.health().connected,
            detail="paper broker" if self.execution_enabled else "not connected (execution disabled)",
            critical=True,
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
        self.health.register(
            # Phase C: a history->live gap larger than the configured tolerance
            # makes the data untrustworthy for trading until it is re-warmed.
            "data_integrity",
            lambda: self._data_gap_ok,
            detail="history/live continuity",
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
        if not self.execution_enabled:
            # EXECUTION SAFETY: never query the broker while execution is off.
            # Use the configured baseline so risk/sizing diagnostics stay
            # meaningful without touching a broker endpoint.
            equity = float(self.config.backtesting.initial_capital)
            self._account_equity = equity
            self.pnl.set_equity(equity)
            return
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
                await self.bus.emit(
                    EventType.CANDLE_REJECTED,
                    payload={"symbol": symbol, "reason": "invalid_trade"},
                )
                return
            if self._is_out_of_order(symbol, update.timestamp, "trade"):
                await self.bus.emit(
                    EventType.CANDLE_REJECTED,
                    payload={"symbol": symbol, "reason": "out_of_order_or_duplicate"},
                )
                return
            self.store.update_trade(update)
            await self._on_live_handoff(update.timestamp)
            closed = self.aggregator.add_trade(update)
            for started in self._candle_started:
                await self._emit_candle_started(started)
            self._candle_started.clear()
            for candle in closed:
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
        logger.debug(
            "raw market data received",
            extra={
                "structured": {
                    "event": "MARKET_DATA_TICK",
                    "component": "market_data",
                    "symbol": symbol,
                    "kind": type(update).__name__,
                    "price": snapshot.price if snapshot else None,
                }
            },
        )
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

    async def _emit_candle_started(self, candle) -> None:
        """Phase B #5: a new candle bucket opened (symbol + timeframe)."""
        logger.info(
            "CANDLE STARTED",
            extra={
                "structured": {
                    "event": "CANDLE_STARTED",
                    "component": "market_data",
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "bucket": candle.timestamp.isoformat(),
                }
            },
        )
        await self.bus.emit(
            EventType.CANDLE_STARTED,
            payload={"symbol": candle.symbol, "timeframe": candle.timeframe},
        )

    async def _on_candle_closed(self, candle) -> None:
        now = utcnow()
        logger.info(
            "CANDLE COMPLETED",
            extra={
                "structured": {
                    "event": "CANDLE_COMPLETED",
                    "component": "market_data",
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "close": candle.close,
                    "volume": candle.volume,
                }
            },
        )
        await self.bus.emit(
            EventType.CANDLE_COMPLETED,
            payload={
                "symbol": candle.symbol,
                "timeframe": candle.timeframe,
                "close": candle.close,
            },
        )
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
        logger.info(
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
            logger.info(
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
            logger.info(
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
            logger.info(
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

    # -- historical warm-up (Phase C) -----------------------------------------
    async def warm_up(self) -> bool:
        """Seed the pipeline from historical bars so it starts strategy-ready.

        Architecture: HISTORICAL -> NORMALIZE -> CANDLE HISTORY -> FEATURES ->
        REGIME -> (ready) -> LIVE STREAM. Historical 1-minute bars are folded
        through the *existing* candle aggregator, so derived timeframes are built
        by exactly the same code path as live data.
        """
        history = self.config.market_data.history
        if not history.enabled or history.provider == "none":
            self.warmup = self._warmup_state(status="skipped", reason="history_disabled")
            return False

        requirement = compute_warmup_requirement(self.config)
        bars = history.lookback_bars or requirement.history_bars_needed
        bars = min(bars, history.maximum_history_bars)
        self.warmup = self._warmup_state(
            status="running",
            reason=None,
            requirement=requirement.as_dict(),
            requested_bars=bars,
            required_bars=requirement.history_bars_needed,
        )
        await self._warmup_event(EventType.WARMUP_STARTED, "WARMUP STARTED", symbol=self.symbol)
        await self._warmup_event(
            EventType.WARMUP_REQUESTED,
            "WARMUP REQUESTED",
            symbol=self.symbol,
            provider=history.provider,
            timeframe=requirement.history_timeframe,
            bars=bars,
            derived_from=requirement.reasons,
        )

        try:
            candles = await self.history_client.fetch_candles(
                symbol=self.symbol,
                bars=bars,
                timeframe=requirement.history_timeframe,
                timeout_seconds=history.startup_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - warm-up failure must stay safe
            await self._warmup_failed(f"history_fetch_failed: {exc}")
            return False

        if not candles:
            await self._warmup_failed("empty_history")
            return False

        await self._warmup_event(
            EventType.WARMUP_RECEIVED,
            "WARMUP RECEIVED",
            symbol=self.symbol,
            received=len(candles),
            first=candles[0].timestamp.isoformat(),
            last=candles[-1].timestamp.isoformat(),
        )

        built = self._build_history_candles(candles)
        await self._warmup_event(
            EventType.WARMUP_CANDLES_BUILT,
            "WARMUP CANDLES BUILT",
            symbol=self.symbol,
            candles={tf: len(built.get(tf, [])) for tf in self.timeframes},
        )

        features = self._features_for(self.config.timeframes.signal, utcnow())
        self.state.features = features
        features_ready = bool(features.ready)
        await self._warmup_event(
            EventType.WARMUP_FEATURES_READY,
            "WARMUP FEATURES READY" if features_ready else "WARMUP FEATURES NOT READY",
            ready=features_ready,
            missing=features.missing,
        )

        context_features = self._features_for(self.config.timeframes.context, utcnow())
        regime = self.regime_engine.classify(context_features, now=utcnow())
        self.state.record_regime(regime)
        await self._warmup_event(
            EventType.WARMUP_REGIME_READY,
            "WARMUP REGIME READY" if not regime.is_unknown else "WARMUP REGIME NOT READY",
            regime=regime.regime.value,
            reason=regime.reason,
        )

        if not features_ready or regime.is_unknown:
            self.warmup.update(
                {
                    "features_ready": features_ready,
                    "regime_ready": not regime.is_unknown,
                    "historical_candles": len(candles),
                    "last_historical_at": candles[-1].timestamp.isoformat(),
                }
            )
            await self._warmup_failed("insufficient_history")
            return False

        await self._complete_warmup(candles, built)
        return True

    async def _complete_warmup(self, candles: list, built: dict) -> None:
        """Record the warm-up result and arm the live handoff watermark."""
        # Seed the trade watermark so the first live update cannot duplicate a
        # historical bar, and record the boundary for gap detection.
        self._last_historical_at = candles[-1].timestamp
        self._live_handoff_done = False
        self._last_update_at[f"{self.symbol}:trade"] = self._last_historical_at

        self.warmup.update(
            {
                "status": "completed",
                "features_ready": True,
                "regime_ready": True,
                "historical_candles": len(candles),
                "historical_candles_built": {tf: len(built.get(tf, [])) for tf in self.timeframes},
                "last_historical_at": self._last_historical_at.isoformat(),
                "live_handoff": "pending",
            }
        )

        await self._warmup_event(
            EventType.WARMUP_COMPLETED,
            "WARMUP COMPLETED",
            symbol=self.symbol,
            historical_candles=len(candles),
            last_historical_at=self._last_historical_at.isoformat(),
        )
        await self._warmup_event(
            EventType.LIVE_HANDOFF_STARTED,
            "LIVE HANDOFF STARTED",
            last_historical_at=self._last_historical_at.isoformat(),
        )

    def _build_history_candles(self, history: list) -> dict[str, list]:
        """Fold historical bars into every configured timeframe.

        Each base bar is replayed through the live candle aggregator as a single
        print (timestamp = bar close), so 1m/5m/15m are produced by the same
        aggregation code used for live data — no second pipeline.
        """
        aggregator = CandleAggregator(self.symbol, self.timeframes)
        for bar in history:
            aggregator.add_trade(
                Trade(timestamp=bar.timestamp, symbol=self.symbol, price=bar.close, size=bar.volume)
            )
            for timeframe in self.timeframes:
                current = aggregator.current(timeframe)
                if current is not None:
                    self.store.upsert_candle(current)
        return {timeframe: self.store.candles(self.symbol, timeframe) for timeframe in self.timeframes}

    async def _on_live_handoff(self, timestamp: datetime) -> None:
        """First accepted live update after warm-up: measure the gap, then hand off."""
        if self._live_handoff_done or self._last_historical_at is None:
            return
        self._live_handoff_done = True
        gap_seconds = (timestamp - self._last_historical_at).total_seconds()
        base_minutes = timeframe_minutes(self.config.market_data.history.bar_timeframe)
        gap_candles = max(0.0, gap_seconds / (base_minutes * 60.0))
        allowed = self.config.market_data.history.max_gap_candles
        within = gap_candles <= allowed
        self.warmup.update(
            {
                "first_live_at": timestamp.isoformat(),
                "live_handoff": "completed",
                "gap": {
                    "detected": gap_seconds > 0,
                    "seconds": gap_seconds,
                    "candles": gap_candles,
                    "within_tolerance": within,
                },
            }
        )
        if not within:
            # Fail-closed: a history->live gap we cannot trust keeps the system
            # unhealthy (and therefore not trading) until it is re-warmed.
            self._data_gap_ok = False
            logger.error(
                "DATA GAP after warm-up",
                extra={
                    "structured": {
                        "event": "DATA_GAP",
                        "component": "engine",
                        "gap_candles": gap_candles,
                        "allowed": allowed,
                    }
                },
            )
            await self.bus.emit(
                EventType.DATA_GAP, payload={"gap_candles": gap_candles, "allowed": allowed}
            )
        await self._warmup_event(
            EventType.LIVE_HANDOFF_COMPLETED,
            "LIVE HANDOFF COMPLETED",
            last_historical_at=self._last_historical_at.isoformat(),
            first_live_at=timestamp.isoformat(),
            gap_candles=gap_candles,
            gap_within_tolerance=within,
        )

    async def _warmup_failed(self, reason: str) -> None:
        self.warmup.update({"status": "failed", "reason": reason})
        logger.error(
            "WARMUP FAILED",
            extra={
                "structured": {
                    "event": "WARMUP_FAILED",
                    "component": "engine",
                    "reason": reason,
                    "symbol": self.symbol,
                }
            },
        )
        await self.bus.emit(EventType.WARMUP_FAILED, payload={"reason": reason, "symbol": self.symbol})

    async def _warmup_event(self, event_type: EventType, message: str, **fields) -> None:
        logger.info(message, extra={"structured": {"event": event_type.value, "component": "engine", **fields}})
        await self.bus.emit(event_type, payload=fields)

    def _warmup_state(self, *, status: str, reason: str | None, **extra) -> dict:
        base = {
            "status": status,
            "reason": reason,
            "features_ready": False,
            "regime_ready": False,
            "historical_candles": 0,
            "historical_candles_built": {},
            "required_bars": None,
            "requested_bars": None,
            "last_historical_at": None,
            "first_live_at": None,
            "live_handoff": "pending",
            "gap": {"detected": False, "seconds": None, "candles": None, "within_tolerance": True},
        }
        base.update(extra)
        return base

    def warmup_status(self) -> dict:
        """Warm-up + handoff state for the dashboard/API (sanitized)."""
        status = dict(self.warmup)
        status["execution"] = self.execution_status()["status"]
        return status

    # -- risk gate ----------------------------------------------------------
    def _build_risk_context(self, now: datetime) -> RiskContext:
        age = self.store.data_age(self.symbol, now)
        # Risk POLICY (fail-closed): the blocking threshold is a risk setting, and
        # `market_data.freshness.stale_action` decides whether losing freshness
        # actually blocks entries or only warns.
        freshness = self.config.market_data.freshness
        measured = age is not None and age <= self.config.risk.stale_market_data.maximum_age_seconds
        fresh = measured if freshness.stale_action == "block_entries" else True
        if not self._data_gap_ok:
            fresh = False
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
        logger.info(
            "RISK EVALUATED",
            extra={
                "structured": {
                    "event": "RISK_EVALUATED",
                    "component": "risk",
                    "signal_id": signal.signal_id,
                    "approved": decision.approved,
                    "reason": decision.reason.value,
                    "execution_enabled": self.execution_enabled,
                }
            },
        )
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

        if not self.execution_enabled:
            # EXECUTION SAFETY (Phase B, fail-closed): risk approved and sizing
            # computed for diagnostics, but execution is disabled — so we stop
            # HERE. No OrderIntent is created, nothing enters the OMS, and no
            # broker endpoint is called.
            logger.warning(
                "EXECUTION BLOCKED",
                extra={
                    "structured": {
                        "event": "EXECUTION_BLOCKED",
                        "component": "engine",
                        "reason": "execution_disabled",
                        "signal_id": signal.signal_id,
                        "direction": signal.direction.value,
                        "quantity": sizing.final_quantity,
                    }
                },
            )
            await self.bus.emit(
                EventType.EXECUTION_BLOCKED,
                payload={
                    "signal_id": signal.signal_id,
                    "reason": "execution_disabled",
                    "quantity": sizing.final_quantity,
                },
                correlation_id=signal.correlation_id,
                session_id=signal.session_id,
            )
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
        # EXECUTION SAFETY (Phase B): the single funnel to the broker. Even if a
        # future code path produced an Order directly, submission is refused here.
        if not self.execution_enabled:
            logger.critical(
                "EXECUTION BLOCKED — order submission refused",
                extra={
                    "structured": {
                        "event": "EXECUTION_BLOCKED",
                        "component": "engine",
                        "reason": "execution_disabled",
                        "order_id": order.order_id,
                        "side": order.side.value,
                        "quantity": order.quantity,
                    }
                },
            )
            await self.bus.emit(
                EventType.EXECUTION_BLOCKED,
                payload={"order_id": order.order_id, "reason": "execution_disabled"},
            )
            return False
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
        if not self.execution_enabled:
            # EXECUTION SAFETY: while execution is disabled no order can exist,
            # so internal state is trivially flat and there is nothing to compare
            # against a broker. Crucially we do NOT contact the broker here, and
            # we do NOT assume anything about real positions — we simply record
            # that reconciliation is not applicable in this mode.
            self._need_reconciliation = False
            self.state.reconciliation = {
                "ok": True,
                "discrepancies": [],
                "last_run_at": utcnow().isoformat(),
                "mode": "execution_disabled",
                "note": "no orders possible while execution is disabled",
            }
            logger.info(
                "RECONCILIATION SKIPPED (execution disabled)",
                extra={
                    "structured": {
                        "event": "RECONCILIATION_COMPLETED",
                        "component": "engine",
                        "mode": "execution_disabled",
                    }
                },
            )
            await self.bus.emit(
                EventType.RECONCILIATION_COMPLETED, payload={"mode": "execution_disabled"}
            )
            return True
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
        # Freshness MEASUREMENT (Phase C): what the dashboard/health report use.
        # The entry-BLOCKING decision stays in risk.stale_market_data and is
        # applied in _build_risk_context — measurement and policy are separate.
        limit = self.config.market_data.freshness.threshold_seconds
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
        data["execution"] = self.execution_status()
        data["warmup"] = self.warmup_status()
        return data

    @property
    def running(self) -> bool:
        return self._running










