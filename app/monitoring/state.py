"""Central, sanitized snapshot of everything the dashboard/API needs.

All mutation happens on the asyncio event loop, so a plain object is sufficient
(no locking). ``to_payload`` returns a JSON-safe dict and never includes secrets.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any

from app.core.clock import utcnow
from app.domain.enums import HealthState
from app.domain.features import FeatureSnapshot
from app.domain.market import MarketSnapshot
from app.domain.orders import Order
from app.domain.pnl import PnlSnapshot, TradeRecord
from app.domain.positions import Position
from app.domain.regime import RegimeSnapshot
from app.domain.session import SessionSnapshot
from app.domain.signals import StrategySignal


class SystemState:
    def __init__(
        self,
        *,
        environment: str,
        mode: str,
        symbol: str,
        broker: str,
        market_data: str,
        ai_provider: str,
    ) -> None:
        self.started_at = utcnow()
        self.environment = environment
        self.mode = mode
        self.symbol = symbol
        self.broker = broker
        self.market_data_provider = market_data
        self.ai_provider = ai_provider
        self.components: dict[str, dict[str, Any]] = {}
        self.latest_snapshot: MarketSnapshot | None = None
        self.features: FeatureSnapshot | None = None
        self.regime: RegimeSnapshot | None = None
        self.regime_history: deque[dict[str, Any]] = deque(maxlen=50)
        self.strategy_name: str = ""
        self.last_evaluation_at: datetime | None = None
        self.last_signal: StrategySignal | None = None
        self.recent_signals: deque[StrategySignal] = deque(maxlen=25)
        self.risk: dict[str, Any] = {}
        self.rejections: deque[dict[str, Any]] = deque(maxlen=25)
        self.orders: dict[str, Order] = {}
        self.position: Position | None = None
        self.pnl: PnlSnapshot | None = None
        self.trades: deque[TradeRecord] = deque(maxlen=50)
        self.session: SessionSnapshot | None = None
        self.ai: dict[str, Any] = {
            "status": "unknown",
            "last_event": None,
            "tool_calls": 0,
            "investigations": 0,
            "rejected_actions": 0,
        }
        self.reconciliation: dict[str, Any] = {"ok": True, "discrepancies": [], "last_run_at": None}
        self.errors: deque[dict[str, Any]] = deque(maxlen=50)
        self.notes: dict[str, Any] = {}

    # -- updates ------------------------------------------------------------
    def set_component(self, name: str, state: HealthState, detail: str = "") -> None:
        self.components[name] = {
            "name": name,
            "state": state.value,
            "detail": detail,
            "updated_at": utcnow().isoformat(),
        }

    def record_regime(self, regime: RegimeSnapshot) -> None:
        if self.regime is None or self.regime.regime is not regime.regime:
            self.regime_history.append(
                {
                    "timestamp": regime.timestamp.isoformat(),
                    "regime": regime.regime.value,
                    "reason": regime.reason,
                }
            )
        self.regime = regime

    def record_signal(self, signal: StrategySignal) -> None:
        self.last_signal = signal
        self.recent_signals.append(signal)

    def record_rejection(self, payload: dict[str, Any]) -> None:
        self.rejections.append(payload)

    def record_order(self, order: Order) -> None:
        self.orders[order.order_id] = order

    def record_trade(self, trade: TradeRecord) -> None:
        self.trades.append(trade)

    def record_error(self, component: str, message: str) -> None:
        self.errors.append({"timestamp": utcnow().isoformat(), "component": component, "message": message})

    def uptime_seconds(self) -> float:
        return (utcnow() - self.started_at).total_seconds()

    # -- serialization ------------------------------------------------------
    def to_payload(self) -> dict[str, Any]:
        return {
            "system": self._system_payload(),
            "market": self.latest_snapshot.model_dump(mode="json") if self.latest_snapshot else None,
            "features": self.features.model_dump(mode="json") if self.features else None,
            "regime": self.regime.model_dump(mode="json") if self.regime else None,
            "regime_history": list(self.regime_history),
            "strategy": {
                "name": self.strategy_name,
                "last_evaluation_at": self.last_evaluation_at.isoformat() if self.last_evaluation_at else None,
                "last_signal": self.last_signal.model_dump(mode="json") if self.last_signal else None,
                "recent_signals": [s.model_dump(mode="json") for s in list(self.recent_signals)[-10:]],
            },
            "risk": self.risk,
            "rejections": list(self.rejections),
            "orders": [o.model_dump(mode="json") for o in list(self.orders.values())[-25:]],
            "position": self.position.model_dump(mode="json") if self.position else None,
            "pnl": self.pnl.model_dump(mode="json") if self.pnl else None,
            "trades": [t.model_dump(mode="json") for t in list(self.trades)[-25:]],
            "session": self.session.model_dump(mode="json") if self.session else None,
            "ai": self.ai,
            "reconciliation": self.reconciliation,
            "errors": list(self.errors),
        }

    def _system_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "environment": self.environment,
            "symbol": self.symbol,
            "broker": self.broker,
            "market_data_provider": self.market_data_provider,
            "ai_provider": self.ai_provider,
            "uptime_seconds": self.uptime_seconds(),
            "started_at": self.started_at.isoformat(),
            "components": list(self.components.values()),
            "notes": self.notes,
        }
