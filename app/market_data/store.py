"""In-memory market store: latest snapshots and rolling candle history.

Data freshness is derived here so stale data can block new entries.
Phase D: per-stream (bar / quote / trade) timestamps for
cadence-aware freshness.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Literal

from app.domain.market import Candle, MarketSnapshot, Quote, Trade

StreamKind = Literal["bar", "quote", "trade"]


class MarketStore:
    def __init__(self, timeframes: list[str], *, max_candles: int = 500) -> None:
        self.timeframes = timeframes
        self.max_candles = max_candles
        self._snapshots: dict[str, MarketSnapshot] = {}
        self._candles: dict[str, dict[str, deque[Candle]]] = {}
        self._last_message_at: dict[str, datetime] = {}
        # Phase D: per-stream last-received timestamps.
        self._stream_at: dict[tuple[str, StreamKind], datetime] = {}

    # -- ingestion ----------------------------------------------------------
    def update_quote(self, quote: Quote) -> MarketSnapshot:
        snapshot = self._snapshots.get(quote.symbol) or MarketSnapshot(
            symbol=quote.symbol, timestamp=quote.timestamp
        )
        snapshot.bid = quote.bid
        snapshot.ask = quote.ask
        snapshot.timestamp = quote.timestamp
        self._snapshots[quote.symbol] = snapshot
        self._last_message_at[quote.symbol] = quote.timestamp
        self._stream_at[(quote.symbol, "quote")] = quote.timestamp
        return snapshot

    def update_trade(self, trade: Trade) -> MarketSnapshot:
        snapshot = self._snapshots.get(trade.symbol) or MarketSnapshot(
            symbol=trade.symbol, timestamp=trade.timestamp
        )
        snapshot.last = trade.price
        snapshot.volume = trade.size
        snapshot.timestamp = trade.timestamp
        self._snapshots[trade.symbol] = snapshot
        self._last_message_at[trade.symbol] = trade.timestamp
        self._stream_at[(trade.symbol, "trade")] = trade.timestamp
        return snapshot

    def add_candle(self, candle: Candle) -> None:
        per_symbol = self._candles.setdefault(candle.symbol, {})
        series = per_symbol.setdefault(candle.timeframe, deque(maxlen=self.max_candles))
        series.append(candle)
        self._stream_at[(candle.symbol, "bar")] = candle.timestamp

    def upsert_candle(self, candle: Candle) -> None:
        """Insert or replace the in-progress candle for a bucket.

        Used by historical warm-up: a candle that is still open is replaced as it
        grows, completed candles are never rewritten, and out-of-order rows are
        ignored so replaying history can never corrupt the series.
        """
        per_symbol = self._candles.setdefault(candle.symbol, {})
        series = per_symbol.setdefault(candle.timeframe, deque(maxlen=self.max_candles))
        if series and series[-1].timestamp == candle.timestamp:
            series[-1] = candle
            return
        if series and candle.timestamp < series[-1].timestamp:
            return
        series.append(candle)
        self._stream_at[(candle.symbol, "bar")] = candle.timestamp

    def replace_candles(self, symbol: str, timeframe: str, candles: list[Candle]) -> None:
        """Replace a whole candle series (used by recovery/rebuild)."""
        per_symbol = self._candles.setdefault(symbol, {})
        series = deque(maxlen=self.max_candles)
        for candle in sorted(candles, key=lambda c: c.timestamp):
            series.append(candle)
        per_symbol[timeframe] = series

    # -- queries ------------------------------------------------------------
    def snapshot(self, symbol: str) -> MarketSnapshot | None:
        return self._snapshots.get(symbol)

    def candles(self, symbol: str, timeframe: str) -> list[Candle]:
        series = self._candles.get(symbol, {}).get(timeframe)
        return list(series) if series else []

    def last_message_at(self, symbol: str) -> datetime | None:
        return self._last_message_at.get(symbol)

    def stream_last_at(self, symbol: str, stream: StreamKind) -> datetime | None:
        return self._stream_at.get((symbol, stream))

    def data_age(self, symbol: str, now: datetime) -> float | None:
        last = self._last_message_at.get(symbol)
        if last is None:
            return None
        return (now - last).total_seconds()

    def stream_age(self, symbol: str, stream: StreamKind, now: datetime) -> float | None:
        last = self._stream_at.get((symbol, stream))
        if last is None:
            return None
        return (now - last).total_seconds()

    def is_stale(self, symbol: str, now: datetime, max_age_seconds: float) -> bool:
        age = self.data_age(symbol, now)
        return age is None or age > max_age_seconds

    def is_stream_stale(
        self, symbol: str, stream: StreamKind, now: datetime, max_age_seconds: float
    ) -> bool:
        age = self.stream_age(symbol, stream, now)
        return age is None or age > max_age_seconds

    def has_candles(self, symbol: str, timeframe: str, minimum: int) -> bool:
        return len(self.candles(symbol, timeframe)) >= minimum
