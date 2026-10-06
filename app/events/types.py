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
    WARMUP_STARTED = "WarmupStarted"
    WARMUP_REQUESTED = "WarmupRequested"
    WARMUP_RECEIVED = "WarmupReceived"
    WARMUP_CANDLES_BUILT = "WarmupCandlesBuilt"
    WARMUP_FEATURES_READY = "WarmupFeaturesReady"
    WARMUP_REGIME_READY = "WarmupRegimeReady"
    WARMUP_COMPLETED = "WarmupCompleted"
    WARMUP_FAILED = "WarmupFailed"
    LIVE_HANDOFF_STARTED = "LiveHandoffStarted"
    LIVE_HANDOFF_COMPLETED = "LiveHandoffCompleted"
    DATA_GAP = "DataGap"
    READINESS_CHANGED = "ReadinessChanged"
    ENTRY_BLOCKED = "EntryBlocked"
    BAR_STREAM_CONNECTED = "BarStreamConnected"
    BAR_STREAM_SUBSCRIBED = "BarStreamSubscribed"
    BAR_RECEIVED = "BarReceived"
    QUOTE_RECEIVED = "QuoteReceived"
    TRADE_RECEIVED = "TradeReceived"
    BAR_REJECTED = "BarRejected"
    BAR_DUPLICATE = "BarDuplicate"
    BAR_OUT_OF_ORDER = "BarOutOfOrder"
    BAR_GAP_DETECTED = "BarGapDetected"
    HISTORICAL_COVERAGE_CHECKED = "HistoricalCoverageChecked"
    HISTORICAL_COVERAGE_FAILED = "HistoricalCoverageFailed"
    RECOVERY_STARTED = "RecoveryStarted"
    RECOVERY_HISTORICAL_FETCH = "RecoveryHistoricalFetch"
    RECOVERY_CANDLE_REBUILD = "RecoveryCandleRebuild"
    RECOVERY_FEATURE_REBUILD = "RecoveryFeatureRebuild"
    RECOVERY_REGIME_REBUILD = "RecoveryRegimeRebuild"
    RECOVERY_COMPLETED = "RecoveryCompleted"
    RECOVERY_FAILED = "RecoveryFailed"
    FEATURE_REBUILD_STARTED = "FeatureRebuildStarted"
    FEATURE_REBUILD_COMPLETED = "FeatureRebuildCompleted"
    REGIME_REBUILD_STARTED = "RegimeRebuildStarted"
    REGIME_REBUILD_COMPLETED = "RegimeRebuildCompleted"
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
    # Phase D — decision observability
    FRESHNESS_EVALUATED = "FreshnessEvaluated"
    FRESHNESS_CHANGED = "FreshnessChanged"
    TIMEFRAME_SELECTION_STARTED = "TimeframeSelectionStarted"
    TIMEFRAME_SELECTED = "TimeframeSelected"
    TIMEFRAME_SELECTION_BLOCKED = "TimeframeSelectionBlocked"
    TRADE_PLAN_CREATED = "TradePlanCreated"
    TRADE_PLAN_UPDATED = "TradePlanUpdated"
    TRADE_PLAN_INVALIDATED = "TradePlanInvalidated"
    ENTRY_CONDITION_EVALUATED = "EntryConditionEvaluated"
    EXIT_CONDITION_EVALUATED = "ExitConditionEvaluated"
    HOLDING_DURATION_UPDATED = "HoldingDurationUpdated"
    DECISION_BLOCKED = "DecisionBlocked"
    FUTURE_TIMESTAMP_REJECTED = "FutureTimestampRejected"
    RECOVERY_STATE_CHANGED = "RecoveryStateChanged"
    ENTRY_CUTOFF_REACHED = "EntryCutoffReached"
    ENTRY_BLOCKED_SESSION_CUTOFF = "EntryBlockedSessionCutoff"
    MAX_HOLDING_REACHED = "MaxHoldingReached"
    FLATTEN_STARTED = "FlattenStarted"
    FLATTEN_ORDER_CREATED = "FlattenOrderCreated"
    FLATTEN_PARTIAL_FILL = "FlattenPartialFill"
    FLATTEN_INCOMPLETE = "FlattenIncomplete"
    BROKER_POSITION_READ = "BrokerPositionRead"
    BROKER_POSITION_ZERO = "BrokerPositionZero"
    SESSION_FLAT = "SessionFlat"
    SESSION_CLOSED = "SessionClosed"
    SESSION_CLOSEOUT_FAILED = "SessionCloseoutFailed"
    SESSION_HALTED = "SessionHalted"


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
