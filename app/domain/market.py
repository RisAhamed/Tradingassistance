"""Normalized market-data domain models.

Provider-specific structures (e.g. Alpaca payloads) are converted into these
objects at the boundary so the rest of the application never depends on a
broker's wire format.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class Quote(BaseModel):
    timestamp: datetime
    symbol: str
    bid: float
    ask: float
    bid_size: float | None = None
    ask_size: float | None = None

    @model_validator(mode="after")
    def _positive(self) -> "Quote":
        if self.bid <= 0 or self.ask <= 0:
            raise ValueError("bid/ask must be positive")
        return self

    @property
    def spread(self) -> float:
        return abs(self.ask - self.bid)

    @property
    def mid(self) -> float:
        return (self.ask + self.bid) / 2.0

    @property
    def spread_percent(self) -> float:
        mid = self.mid
        return (self.spread / mid * 100.0) if mid else 0.0


class Trade(BaseModel):
    timestamp: datetime
    symbol: str
    price: float = Field(gt=0)
    size: float = Field(ge=0)


class Candle(BaseModel):
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    timeframe: str = "1m"

    @model_validator(mode="after")
    def _consistent(self) -> "Candle":
        if self.high < self.low:
            raise ValueError("candle high < low")
        if not (self.low <= self.open <= self.high) or not (self.low <= self.close <= self.high):
            raise ValueError("candle open/close outside high/low band")
        return self


class MarketSnapshot(BaseModel):
    """The latest normalized view of one symbol's market."""

    symbol: str
    timestamp: datetime
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None

    def age_seconds(self, now: datetime) -> float:
        return (now - self.timestamp).total_seconds()

    def is_stale(self, now: datetime, max_age_seconds: float) -> bool:
        return self.age_seconds(now) > max_age_seconds

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return abs(self.ask - self.bid)

    @property
    def spread_percent(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        mid = (self.bid + self.ask) / 2.0
        return self.spread / mid * 100.0 if mid else None

    @property
    def price(self) -> float | None:
        if self.last is not None:
            return self.last
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return self.bid if self.bid is not None else self.ask
