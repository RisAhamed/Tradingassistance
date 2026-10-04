"""Deterministic feature snapshot produced by the feature engine."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class FeatureSnapshot(BaseModel):
    symbol: str
    timestamp: datetime
    timeframe: str
    close: float | None = None
    high: float | None = None
    low: float | None = None
    spread_percent: float | None = None
    data_age_seconds: float | None = None
    candle_count: int = 0
    values: dict[str, float | None] = Field(default_factory=dict)

    def get(self, name: str, default: float | None = None) -> float | None:
        return self.values.get(name, default)

    def ema(self, period: int) -> float | None:
        return self.values.get(f"ema_{period}")

    @property
    def rsi(self) -> float | None:
        return self.values.get("rsi")

    @property
    def atr(self) -> float | None:
        return self.values.get("atr")

    @property
    def vwap(self) -> float | None:
        return self.values.get("vwap")

    @property
    def range_high(self) -> float | None:
        return self.values.get("range_high")

    @property
    def range_low(self) -> float | None:
        return self.values.get("range_low")

    @property
    def volatility(self) -> float | None:
        return self.values.get("volatility")

    @property
    def return_short(self) -> float | None:
        return self.values.get("return_short")

    @property
    def volume_ratio(self) -> float | None:
        return self.values.get("volume_ratio")

    def numeric_values(self) -> dict[str, float]:
        return {k: v for k, v in self.values.items() if v is not None}
