"""Market-regime domain model."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import Regime


class RegimeSnapshot(BaseModel):
    symbol: str
    timestamp: datetime
    timeframe: str
    regime: Regime = Regime.UNKNOWN
    reason: str = "insufficient_data"
    detail: dict[str, float | None] = Field(default_factory=dict)

    @property
    def is_unknown(self) -> bool:
        return self.regime is Regime.UNKNOWN

    @property
    def is_trending(self) -> bool:
        return self.regime in (Regime.TRENDING_BULLISH, Regime.TRENDING_BEARISH)

    @property
    def allows_long(self) -> bool:
        return self.regime is Regime.TRENDING_BULLISH

    @property
    def allows_short(self) -> bool:
        return self.regime is Regime.TRENDING_BEARISH
