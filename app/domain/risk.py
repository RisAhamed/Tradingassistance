"""Risk decision domain models."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import ReasonCode


class RiskCheckResult(BaseModel):
    name: str
    passed: bool
    reason: ReasonCode | None = None
    detail: str | None = None


class RiskDecision(BaseModel):
    risk_id: str
    timestamp: datetime
    signal_id: str
    symbol: str
    approved: bool
    reason: ReasonCode
    checks: list[RiskCheckResult] = Field(default_factory=list)
    correlation_id: str | None = None
    session_id: str | None = None

    @property
    def failed_checks(self) -> list[RiskCheckResult]:
        return [check for check in self.checks if not check.passed]
