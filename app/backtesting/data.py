"""Historical data for backtests.

Three sources: deterministic synthetic generator (default/tests), CSV, and an
optional Alpaca historical fetch (lazy import, requires credentials).
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta

from app.core.clock import floor_to_timeframe, timeframe_seconds, utcnow
from app.domain.market import Candle


def generate_history(
    symbol: str = "BTC/USD",
    *,
    timeframe: str = "1m",
    length: int = 600,
    seed: int = 7,
    start_price: float = 30000.0,
    drift: float = 0.0008,
    noise: float = 0.0035,
    start: datetime | None = None,
) -> list[Candle]:
    """Deterministic synthetic candles (reproducible via ``seed``)."""
    rng = random.Random(seed)
    start = start or (utcnow() - timedelta(minutes=length))
    step_seconds = timeframe_seconds(timeframe) or 60
    price = start_price
    buckets: dict[datetime, list[float]] = {}
    volumes: dict[datetime, float] = {}
    for index in range(length):
        moment = start + timedelta(seconds=index * step_seconds)
        bucket = floor_to_timeframe(moment, timeframe)
        price = max(1.0, price * (1.0 + drift + rng.uniform(-noise, noise)))
        buckets.setdefault(bucket, []).append(price)
        volumes[bucket] = volumes.get(bucket, 0.0) + rng.uniform(0.5, 3.0)
    candles: list[Candle] = []
    for bucket in sorted(buckets):
        prices = buckets[bucket]
        candles.append(
            Candle(
                timestamp=bucket,
                symbol=symbol,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=volumes[bucket],
                timeframe=timeframe,
            )
        )
    return candles


def load_csv(path: str, symbol: str = "BTC/USD", timeframe: str = "1m") -> list[Candle]:
    """Load candles from a CSV with columns timestamp,open,high,low,close,volume."""
    candles: list[Candle] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            timestamp = datetime.fromisoformat(row["timestamp"])
            if timestamp.tzinfo is None:
                from datetime import timezone

                timestamp = timestamp.replace(tzinfo=timezone.utc)
            candles.append(
                Candle(
                    timestamp=timestamp,
                    symbol=symbol,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0.0)),
                    timeframe=timeframe,
                )
            )
    return candles


async def fetch_alpaca_bars(
    symbol: str,
    *,
    api_key: str,
    api_secret: str,
    timeframe: str = "1m",
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Candle]:
    """Fetch historical crypto bars from Alpaca (optional, lazy import)."""
    import asyncio

    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    start = start or (utcnow() - timedelta(days=7))
    end = end or utcnow()
    client = CryptoHistoricalDataClient(api_key, api_secret)

    def _fetch():
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start, end=end
        )
        return client.get_crypto_bars(request)

    response = await asyncio.to_thread(_fetch)
    bars = response.data.get(symbol, []) if hasattr(response, "data") else []
    return [
        Candle(
            timestamp=b.timestamp if hasattr(b, "timestamp") else b.t,
            symbol=symbol,
            open=float(b.open),
            high=float(b.high),
            low=float(b.low),
            close=float(b.close),
            volume=float(getattr(b, "volume", 0.0) or 0.0),
            timeframe=timeframe,
        )
        for b in bars
    ]
