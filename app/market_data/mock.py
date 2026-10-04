"""Deterministic mock market-data source for fast, reproducible testing.

It emits the same normalized Quote/Trade objects as the live provider so the
entire downstream pipeline can be exercised without a network connection.
"""
from __future__ import annotations

import random
from datetime import timedelta

from app.core.clock import utcnow
from app.domain.market import Quote, Trade
from app.market_data.base import MarketDataProvider, ProviderHealth


class MockMarketDataProvider(MarketDataProvider):
    name = "mock"

    def __init__(
        self,
        symbol: str,
        prices: list[float] | None = None,
        *,
        spread_percent: float = 0.02,
        tick_seconds: float = 1.0,
        seed: int = 7,
        size: float = 1.0,
    ) -> None:
        super().__init__()
        self.symbol = symbol
        self.spread_percent = spread_percent
        self.tick_seconds = tick_seconds
        self.size = size
        self._prices = list(prices) if prices else generate_series(seed=seed, length=600)
        self._index = 0
        self._connected = False
        self._last_at = None
        self._clock_offset = timedelta(0)

    async def connect(self, symbols: list[str]) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def health(self) -> ProviderHealth:
        return ProviderHealth(connected=self._connected, detail="mock feed", last_message_at=self._last_at)

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._prices)

    async def push(self, price: float, *, timestamp=None) -> None:
        moment = timestamp or (utcnow() + self._clock_offset)
        self._clock_offset += timedelta(seconds=self.tick_seconds)
        half_spread = price * (self.spread_percent / 100.0) / 2.0
        quote = Quote(
            timestamp=moment,
            symbol=self.symbol,
            bid=price - half_spread,
            ask=price + half_spread,
            bid_size=self.size,
            ask_size=self.size,
        )
        trade = Trade(timestamp=moment, symbol=self.symbol, price=price, size=self.size)
        self._last_at = moment
        await self._dispatch(quote)
        await self._dispatch(trade)

    async def pump_one(self) -> bool:
        """Emit the next scripted price; return False when exhausted."""
        if self.exhausted:
            return False
        price = self._prices[self._index]
        self._index += 1
        await self.push(price)
        return True

    def reset(self) -> None:
        self._index = 0
        self._clock_offset = timedelta(0)


def generate_series(
    *,
    seed: int = 7,
    length: int = 240,
    start: float = 30000.0,
    drift: float = 0.0008,
    noise: float = 0.008,
) -> list[float]:
    """Build a deterministic, gently trending price path."""
    rng = random.Random(seed)
    price = start
    series: list[float] = []
    for _ in range(length):
        price = max(1.0, price * (1.0 + drift + rng.uniform(-noise, noise)))
        series.append(round(price, 2))
    return series
