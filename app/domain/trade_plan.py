"""TradePlan — the observable candidate trading decision.

Phase D: data + decision architecture only. No order is created by a
TradePlan; it must still pass RiskEngine -> PositionSizer -> OMS.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import Direction, Regime


class TradePlanStatus(str, Enum):
    WAIT = "wait"
    READY = "ready"
    INVALIDATED = "invalidated"
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

    def is_actionable(self) -> bool:
        return (
            self.status == TradePlanStatus.READY
            and self.direction is not None
            and self.entry_reference is not None
            and self.stop_price is not None
        )
