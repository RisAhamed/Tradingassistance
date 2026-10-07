"""Structured, explainable trading signal.

Signals originate ONLY from deterministic strategy code — never from an LLM.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

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
    # Phase G: auditable score components (finite floats; weights live under
    # "_weight_<margin>" keys so every value stays a finite float).
    confidence_components: dict[str, float] = Field(default_factory=dict)
    features: dict[str, float | None] = Field(default_factory=dict)
    correlation_id: str | None = None
    session_id: str | None = None

    @field_validator("confidence")
    @classmethod
    def _confidence_bounds(cls, value: float) -> float:
        import math

        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("confidence must be a finite float")
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be within [0.0, 1.0]")
        return float(value)

    @field_validator("confidence_components")
    @classmethod
    def _components_finite(cls, value: dict[str, float]) -> dict[str, float]:
        import math

        for key, component in (value or {}).items():
            if not isinstance(component, (int, float)) or not math.isfinite(component):
                raise ValueError(f"confidence_components['{key}'] must be a finite float")
        return {key: float(component) for key, component in (value or {}).items()}

    @property
    def is_actionable(self) -> bool:
        return self.direction in (Direction.LONG, Direction.SHORT)

    @property
    def risk_distance(self) -> float | None:
        if self.stop_reference is None:
            return None
        return abs(self.entry_reference - self.stop_reference)
