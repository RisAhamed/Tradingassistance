"""Domain enums shared across the application."""
from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"

    @property
    def is_long(self) -> bool:
        return self is Direction.LONG

    @property
    def is_short(self) -> bool:
        return self is Direction.SHORT


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(str, Enum):
    GTC = "gtc"
    IOC = "ioc"


class OrderStatus(str, Enum):
    NEW = "new"
    VALIDATED = "validated"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }


class Regime(str, Enum):
    TRENDING_BULLISH = "trending_bullish"
    TRENDING_BEARISH = "trending_bearish"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    UNKNOWN = "unknown"


class SessionState(str, Enum):
    OFFLINE = "offline"
    STARTING = "starting"
    READY = "ready"
    TRADING = "trading"
    PAUSED = "paused"
    CLOSEOUT = "closeout"
    HALTED = "halted"
    RECONCILIATION = "reconciliation"
    COMPLETED = "completed"


class HealthState(str, Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    ERROR = "error"
    HALTED = "halted"
    UNKNOWN = "unknown"


class Permission(str, Enum):
    READ_ONLY = "read_only"
    LOW_RISK_CONTROL = "low_risk_control"
    HIGH_RISK_CONTROL = "high_risk_control"


class ReasonCode(str, Enum):
    """Canonical, explainable reason codes for signals and risk decisions."""

    # Signal reasons
    BREAKOUT_ABOVE_RANGE = "breakout_above_range"
    BREAKOUT_BELOW_RANGE = "breakout_below_range"
    BULLISH_REGIME = "bullish_regime"
    BEARISH_REGIME = "bearish_regime"
    MOMENTUM_CONFIRMED = "momentum_confirmed"
    TREND_CONFIRMED = "trend_confirmed"
    SPREAD_ACCEPTABLE = "spread_acceptable"
    NO_SIGNAL = "no_signal"
    # VWAP-reversion strategy reasons (additive; existing codes untouched)
    VWAP_STRETCH = "vwap_stretch"
    REVERSION_SETUP = "reversion_setup"
    RANGE_CONTAINED = "range_contained"

    # Risk rejection reasons
    RISK_DISABLED = "risk_disabled"
    SESSION_CLOSED = "session_closed"
    ENTRY_CUTOFF_REACHED = "entry_cutoff_reached"
    SYMBOL_NOT_ALLOWED = "symbol_not_allowed"
    STALE_DATA = "stale_data"
    SPREAD_TOO_HIGH = "spread_too_high"
    POSITION_LIMIT = "position_limit"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    RISK_PER_TRADE_LIMIT = "risk_per_trade_limit"
    MAX_EXPOSURE = "max_exposure"
    MAX_HOLDING_TIME = "max_holding_time"
    DUPLICATE_TRADE = "duplicate_trade"
    COOLDOWN_ACTIVE = "cooldown_active"
    SYSTEM_UNHEALTHY = "system_unhealthy"
    RECONCILIATION_FAILED = "reconciliation_failed"
    INVALID_SIGNAL = "invalid_signal"
    ORDERS_PER_SESSION_LIMIT = "orders_per_session_limit"
    APPROVED = "approved"
