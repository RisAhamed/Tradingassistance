"""Candle aggregation from normalized trades.

Ticks build candles; strategy evaluation happens on candle close (see the
engine). This keeps tick-level data separate from candle-close evaluation.
"""
from __future__ import annotations

from typing import Callable

from app.core.clock import floor_to_timeframe
from app.domain.market import Candle, Trade


class CandleAggregator:
    def __init__(
        self,
        symbol: str,
        timeframes: list[str],
        *,
        on_candle_started: Callable[[Candle], None] | None = None,
    ) -> None:
        self.symbol = symbol
        self.timeframes = timeframes
        # Phase B #5: lets the engine emit CANDLE_STARTED for every new bucket.
        self.on_candle_started = on_candle_started
        self._current: dict[str, Candle] = {}
        self._order: list[str] = []

    def add_trade(self, trade: Trade) -> list[Candle]:
        """Feed a trade; return any candles that *closed* as a result.

        Phase B: only *completed* candles are returned. Duplicate and
        out-of-order trades never roll a candle backwards — an older trade that
        falls into an already-closed bucket is ignored rather than corrupting
        the aggregate.
        """
        closed: list[Candle] = []
        for timeframe in self.timeframes:
            bucket = floor_to_timeframe(trade.timestamp, timeframe)
            current = self._current.get(timeframe)
            if current is None:
                self._start(timeframe, bucket, trade)
            elif bucket > current.timestamp:
                closed.append(current)
                self._start(timeframe, bucket, trade)
            elif bucket < current.timestamp:
                # Late trade for an already-closed bucket: drop, never rewrite.
                continue
            else:
                current.high = max(current.high, trade.price)
                current.low = min(current.low, trade.price)
                current.close = trade.price
                current.volume += trade.size
        return closed

    def _start(self, timeframe: str, bucket, trade: Trade) -> None:
        candle = self._new_candle(timeframe, bucket, trade)
        self._current[timeframe] = candle
        self._remember(timeframe)
        if self.on_candle_started is not None:
            self.on_candle_started(candle)

    def current(self, timeframe: str) -> Candle | None:
        return self._current.get(timeframe)

    def flush(self) -> list[Candle]:
        """Return the in-progress candles (used on graceful shutdown)."""
        return [self._current[tf] for tf in self._order if tf in self._current]

    def _new_candle(self, timeframe: str, bucket, trade: Trade) -> Candle:
        return Candle(
            timestamp=bucket,
            symbol=self.symbol,
            open=trade.price,
            high=trade.price,
            low=trade.price,
            close=trade.price,
            volume=trade.size,
            timeframe=timeframe,
        )

    def _remember(self, timeframe: str) -> None:
        if timeframe not in self._order:
            self._order.append(timeframe)
