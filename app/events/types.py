"""Internal event system: types and the asynchronous event bus."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.core.clock import utcnow
from app.core.ids import new_event_id


class EventType(str, Enum):
    CANDLE_STARTED = "CandleStarted"
    CANDLE_COMPLETED = "CandleCompleted"
    CANDLE_REJECTED = "CandleRejected"
    STRATEGY_EVALUATED = "StrategyEvaluated"
    RISK_EVALUATED = "RiskEvaluated"
    EXECUTION_DISABLED = "ExecutionDisabled"
    EXECUTION_BLOCKED = "ExecutionBlocked"
    ALPACA_CONNECTING = "AlpacaConnecting"
    ALPACA_CONNECTED = "AlpacaConnected"
    ALPACA_DISCONNECTED = "AlpacaDisconnected"
    ALPACA_SUBSCRIPTION_STARTED = "AlpacaSubscriptionStarted"
    ALPACA_SUBSCRIPTION_FAILED = "AlpacaSubscriptionFailed"
    ALPACA_RECONNECTING = "AlpacaReconnecting"
    ALPACA_RECONNECTED = "AlpacaReconnected"
    ALPACA_RECONNECT_FAILED = "AlpacaReconnectFailed"
    MARKET_DATA_RECEIVED = "MarketDataReceived"
    CANDLE_CLOSED = "CandleClosed"
    FEATURES_UPDATED = "FeaturesUpdated"
    REGIME_CHANGED = "RegimeChanged"
    SIGNAL_GENERATED = "SignalGenerated"
    SIGNAL_REJECTED = "SignalRejected"
    RISK_APPROVED = "RiskApproved"
    RISK_REJECTED = "RiskRejected"
    ORDER_CREATED = "OrderCreated"
    ORDER_SUBMITTED = "OrderSubmitted"
    ORDER_FILLED = "OrderFilled"
    ORDER_PARTIALLY_FILLED = "OrderPartiallyFilled"
    ORDER_REJECTED = "OrderRejected"
    ORDER_CANCELLED = "OrderCancelled"
    POSITION_UPDATED = "PositionUpdated"
    TRADE_CLOSED = "TradeClosed"
    PNL_UPDATED = "PnlUpdated"
    SESSION_STARTED = "SessionStarted"
    SESSION_STATE_CHANGED = "SessionStateChanged"
    CLOSEOUT_STARTED = "CloseoutStarted"
    FLATTEN_COMPLETED = "FlattenCompleted"
    FLATTEN_FAILED = "FlattenFailed"
    RECONCILIATION_STARTED = "ReconciliationStarted"
    RECONCILIATION_FAILED = "ReconciliationFailed"
    RECONCILIATION_COMPLETED = "ReconciliationCompleted"
    AI_INVESTIGATION_STARTED = "AIInvestigationStarted"
    AI_INVESTIGATION_COMPLETED = "AIInvestigationCompleted"
    AI_ACTION_REQUESTED = "AIActionRequested"
    AI_ACTION_REJECTED = "AIActionRejected"
    AI_TOOL_CALLED = "AIToolCalled"
    AI_UNAVAILABLE = "AIUnavailable"
    MARKET_DATA_STALE = "MarketDataStale"
    MARKET_DATA_DISCONNECTED = "MarketDataDisconnected"
    MARKET_DATA_CONNECTED = "MarketDataConnected"
    BROKER_CONNECTED = "BrokerConnected"
    BROKER_DISCONNECTED = "BrokerDisconnected"
    SYSTEM_ERROR = "SystemError"
    SYSTEM_READY = "SystemReady"
    ALERT = "Alert"


class Event(BaseModel):
    event_id: str = Field(default_factory=new_event_id)
    type: EventType
    timestamp: datetime = Field(default_factory=utcnow)
    session_id: str | None = None
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type.value,
            "timestamp": self.timestamp.isoformat(),
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }


def make_event(
    event_type: EventType,
    *,
    payload: dict[str, Any] | None = None,
    session_id: str | None = None,
    correlation_id: str | None = None,
    timestamp: datetime | None = None,
) -> Event:
    return Event(
        type=event_type,
        payload=payload or {},
        session_id=session_id,
        correlation_id=correlation_id,
        timestamp=timestamp or utcnow(),
    )
