"""Deterministic mock market-data source for fast, reproducible testing.

It emits the same normalized Quote/Trade objects as the live provider so the
entire downstream pipeline can be exercised without a network connection.
"""
from __future__ import annotations

import random
from datetime import timedelta

from app.core.clock import floor_to_timeframe, utcnow
from app.domain.market import Candle, Quote, Trade
from app.market_data.base import MarketDataProvider, ProviderHealth
from app.market_data.history import timeframe_minutes


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
        bars_enabled: bool = False,
        bar_timeframe: str = "1m",
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
        # Phase C1: mirror the real feed's canonical 1-minute bar stream.
        self.bars_enabled = bars_enabled
        self.bar_timeframe = bar_timeframe
        self._last_bar_at: datetime | None = None
        self._last_price: float | None = None

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
        if self.bars_enabled:
            await self._emit_bars(moment, price)
        await self._dispatch(quote)
        await self._dispatch(trade)

    async def _emit_bars(self, moment: datetime, price: float) -> None:
        """Emit one completed bar per elapsed base-timeframe interval."""
        step = timedelta(minutes=timeframe_minutes(self.bar_timeframe))
        bucket = floor_to_timeframe(moment, self.bar_timeframe)
        if self._last_bar_at is None:
            self._last_bar_at = bucket
            self._last_price = price
            return
        while self._last_bar_at + step <= bucket:
            self._last_bar_at = self._last_bar_at + step
            open_price = self._last_price if self._last_price is not None else price
            candle = Candle(
                timestamp=self._last_bar_at,
                symbol=self.symbol,
                open=open_price,
                high=max(open_price, price),
                low=min(open_price, price),
                close=price,
                volume=self.size,
                timeframe=self.bar_timeframe,
            )
            self._last_price = price
            await self._dispatch(candle)

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
