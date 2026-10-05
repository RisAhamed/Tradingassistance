"""TradePlan — the observable candidate trading decision.

Phase D: data + decision architecture only. No order is created by a
TradePlan; it must still pass RiskEngine -> PositionSizer -> OMS.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.core.clock import utcnow
from app.domain.enums import Direction, Regime


class TradePlanStatus(str, Enum):
    WAIT = "wait"
    READY = "ready"
    ACTIVE = "active"
    UPDATED = "updated"
    INVALIDATED = "invalidated"
    EXECUTED = "executed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FLAT = "flat"


class TradePlan(BaseModel):
    plan_id: str
    timestamp: datetime
    symbol: str
    market: str
    regime: Regime
    strategy: str
    context_timeframe: str
    signal_timeframe: str
    execution_timeframe: str
    direction: Direction | None
    entry_reference: float | None
    stop_price: float | None
    target_price: float | None
    risk_amount: float
    position_quantity: float | None
    expected_holding_minutes: float | None
    maximum_holding_minutes: float
    entry_conditions: list[dict] = Field(default_factory=list)
    exit_conditions: list[dict] = Field(default_factory=list)
    invalidation_conditions: list[dict] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    status: TradePlanStatus = TradePlanStatus.WAIT
    correlation_id: str | None = None
    session_id: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    # --- lifecycle tracking ---
    previous_status: TradePlanStatus | None = None
    status_changed_at: datetime | None = None
    invalidation_reason: str | None = None
    lifecycle_history: list[dict] = Field(default_factory=list)

    def is_actionable(self) -> bool:
        return (
            self.status == TradePlanStatus.READY
            and self.direction is not None
            and self.entry_reference is not None
            and self.stop_price is not None
        )

    def transition_to(
        self,
        new_status: TradePlanStatus,
        *,
        reason: str = "",
        trigger: str = "",
        source_event: str = "",
    ) -> None:
        """Record a lifecycle transition with full audit information."""
        self.previous_status = self.status
        self.status = new_status
        self.status_changed_at = utcnow()
        entry = {
            "previous_status": self.previous_status.value if self.previous_status else None,
            "new_status": new_status.value,
            "timestamp": self.status_changed_at.isoformat(),
            "reason": reason,
            "trigger": trigger,
            "source_event": source_event,
        }
        self.lifecycle_history.append(entry)
