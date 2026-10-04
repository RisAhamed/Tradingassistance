"""Structured, explainable trading signal.

Signals originate ONLY from deterministic strategy code — never from an LLM.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import Direction, ReasonCode, Regime


class StrategySignal(BaseModel):
    signal_id: str
    timestamp: datetime
    symbol: str
    strategy: str
    direction: Direction
    reason_code: ReasonCode
    reason: str
    entry_reference: float
    stop_reference: float | None = None
    take_profit_reference: float | None = None
    timeframe: str = "5m"
    regime: Regime = Regime.UNKNOWN
    confidence: float = 0.0
    features: dict[str, float | None] = Field(default_factory=dict)
    correlation_id: str | None = None
    session_id: str | None = None

    @property
    def is_actionable(self) -> bool:
        return self.direction in (Direction.LONG, Direction.SHORT)

    @property
    def risk_distance(self) -> float | None:
        if self.stop_reference is None:
            return None
        return abs(self.entry_reference - self.stop_reference)
