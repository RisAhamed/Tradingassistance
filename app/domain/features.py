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

    @property
    def ready(self) -> bool:
        """True only when every required feature resolved to a real number.

        Phase A #2: missing/insufficient/stale inputs surface as ``None`` in
        ``values`` — they must never silently become valid numerics. The
        strategy refuses to evaluate while ``ready`` is False.
        """
        required = {"rsi", "atr", "vwap", "range_high", "range_low"}
        for period in (20, 50):
            required.add(f"ema_{period}")
        if self.close is None:
            return False
        for name in required:
            value = self.values.get(name)
            if value is None:
                return False
            if not isinstance(value, (int, float)):
                return False
            if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
                return False
            if name == "atr" and value <= 0:
                return False
        return True

    @property
    def missing(self) -> list[str]:
        """Names of required features that are unavailable (for WAIT reasons)."""
        required = ["rsi", "atr", "vwap", "range_high", "range_low", "ema_20", "ema_50"]
        absent = [name for name in required if self.values.get(name) is None]
        if self.close is None:
            absent.append("close")
        return absent

    @property
    def stale(self) -> bool:
        """True when the snapshot carries an explicit stale-data flag."""
        return bool(self.values.get("__stale__"))
