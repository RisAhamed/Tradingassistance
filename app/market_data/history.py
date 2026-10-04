"""Historical warm-up: candle history, requirement calculation, and handoff.

This module answers two questions without changing the trading pipeline:

1. **How much history does the configured pipeline actually need?** The answer is
   derived from the real configuration (EMA periods, RSI/ATR periods, range and
   breakout lookbacks, regime minimum candles) — never a magic "50 candles".
2. **Where does that history come from?** The existing Alpaca integration, fetched
   as 1-minute bars and normalised into the very same :class:`Candle` objects the
   live stream produces, so historical and live data converge on one model.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.core.clock import utcnow
from app.domain.market import Candle

logger = logging.getLogger("app.market_data.history")

_TF_PATTERN = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.IGNORECASE)
_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440, "w": 10080}


class HistoryError(RuntimeError):
    """Historical data could not be retrieved or validated."""


def timeframe_minutes(timeframe: str) -> int:
    """Parse ``1m`` / ``5m`` / ``15m`` / ``1h`` / ``1d`` into minutes."""
    match = _TF_PATTERN.match(str(timeframe))
    if not match:
        raise HistoryError(f"unsupported timeframe: {timeframe!r}")
    amount, unit = int(match.group(1)), match.group(2).lower()
    return amount * _UNIT_MINUTES[unit]


@dataclass(frozen=True)
class WarmupRequirement:
    """The minimum history the *configured* pipeline needs before it can trade."""

    history_timeframe: str
    signal_candles: int
    context_candles: int
    base_candles: int
    history_bars_needed: int
    signal_minutes: int
    context_minutes: int
    reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "history_timeframe": self.history_timeframe,
            "signal_candles": self.signal_candles,
            "context_candles": self.context_candles,
            "base_candles": self.base_candles,
            "history_bars_needed": self.history_bars_needed,
            "signal_minutes": self.signal_minutes,
            "context_minutes": self.context_minutes,
            "reasons": dict(self.reasons),
        }


def compute_warmup_requirement(config) -> WarmupRequirement:
    """Derive warm-up depth from configuration (features, strategy, regime)."""
    features = config.features
    strategy = config.strategy
    regime = config.regime

    reasons: dict[str, int] = {}
    if features.ema.enabled and features.ema.periods:
        reasons["ema"] = max(features.ema.periods)
    if features.rsi.enabled:
        reasons["rsi"] = features.rsi.period + 1
    if features.atr.enabled:
        reasons["atr"] = features.atr.period + 1
    if features.vwap.enabled:
        reasons["vwap"] = 1
    reasons["range_lookback"] = features.range.lookback_periods + 1
    reasons["breakout_lookback"] = strategy.breakout.lookback_periods + 1
    # The regime classifier must also see enough data before it may classify.
    reasons["regime_min_candles"] = regime.min_candles

    base = max(reasons.values()) if reasons else 1
    signal_minutes = timeframe_minutes(config.timeframes.signal)
    context_minutes = timeframe_minutes(config.timeframes.context)

    # Both derived timeframes need the deeper requirement; the history timeframe
    # is the finest one, so it needs that many base bars.
    history_tf = config.market_data.history.bar_timeframe
    history_minutes = timeframe_minutes(history_tf)
    signal_candles = max(base, 1) * max(1, signal_minutes // history_minutes)
    context_candles = max(base, 1) * max(1, context_minutes // history_minutes)

    # One extra base bar so the *final* (in-progress) derived candle can close.
    bars_needed = max(signal_candles, context_candles) + 1
    return WarmupRequirement(
        history_timeframe=history_tf,
        signal_candles=signal_candles,
        context_candles=context_candles,
        base_candles=max(base, 1),
        history_bars_needed=bars_needed,
        signal_minutes=signal_minutes,
        context_minutes=context_minutes,
        reasons=reasons,
    )

# -- Alpaca historical bars -------------------------------------------------
class AlpacaHistoricalDataClient:
    """Fetches historical bars from Alpaca and normalises them to Candles.

    Uses the *same* Alpaca credential/integration surface as the live provider and
    produces the application's internal :class:`Candle` model, so history and
    live data converge. Credentials are never logged.
    """

    def __init__(self, config, env) -> None:
        self._config = config
        self._env = env

    async def fetch_candles(
        self,
        *,
        symbol: str,
        bars: int,
        timeframe: str,
        timeout_seconds: float,
    ) -> list[Candle]:
        if not self._env.has_alpaca_credentials():
            raise HistoryError("Alpaca credentials missing for historical data")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._fetch_sync, symbol, bars, timeframe),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise HistoryError(f"historical request timed out after {timeout_seconds}s") from exc

    def _fetch_sync(self, symbol: str, bars: int, timeframe: str) -> list[Candle]:
        feed = self._config.market_data.feed
        if feed == "crypto":
            from alpaca.data.historical.crypto import CryptoHistoricalDataClient
            from alpaca.data.requests import CryptoBarsRequest
            from alpaca.data.timeframe import TimeFrame

            client = CryptoHistoricalDataClient(self._env.alpaca_api_key, self._env.alpaca_api_secret)
            minutes = timeframe_minutes(timeframe)
            if minutes != 1:
                raise HistoryError(f"unsupported history timeframe: {timeframe}")
            end = utcnow()
            start = end - timedelta(minutes=bars * minutes + minutes)
            request = CryptoBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
                limit=self._config.market_data.history.maximum_history_bars,
            )
            raw = client.get_crypto_bars(request)
        else:
            from alpaca.data.historical.stock import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            client = StockHistoricalDataClient(self._env.alpaca_api_key, self._env.alpaca_api_secret)
            minutes = timeframe_minutes(timeframe)
            if minutes != 1:
                raise HistoryError(f"unsupported history timeframe: {timeframe}")
            end = utcnow()
            start = end - timedelta(minutes=bars * minutes + minutes)
            request = StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
                limit=self._config.market_data.history.maximum_history_bars,
                feed=feed,
            )
            raw = client.get_stock_bars(request)

        return normalise_bars(raw, symbol=symbol, timeframe=timeframe)


def normalise_bars(raw, *, symbol: str, timeframe: str) -> list[Candle]:
    """Convert SDK bar objects into Candles, dropping invalid/duplicate rows.

    Fail-closed and look-ahead free: rows are kept in timestamp order, duplicates
    and out-of-order rows are dropped, and nothing is fabricated.
    """
    data = getattr(raw, "data", raw)
    # Alpaca returns a BarSet (has .data); accept a plain {"data": {...}} mapping too.
    if isinstance(data, dict) and symbol not in data and isinstance(data.get("data"), dict):
        data = data["data"]
    if not data:
        raise HistoryError("empty historical response")

    by_symbol = data.get(symbol) if isinstance(data, dict) else None
    bars = by_symbol if by_symbol is not None else []
    if not bars:
        raise HistoryError("no historical bars for symbol")

    candles: list[Candle] = []
    seen: set[datetime] = set()
    rejected = 0
    for bar in bars:
        timestamp = getattr(bar, "timestamp", None)
        o, h, l, c = (
            getattr(bar, "open", None),
            getattr(bar, "high", None),
            getattr(bar, "low", None),
            getattr(bar, "close", None),
        )
        volume = getattr(bar, "volume", 0.0) or 0.0
        if timestamp is None or None in (o, h, l, c):
            rejected += 1
            continue
        timestamp = _as_utc(timestamp)
        if timestamp in seen:
            rejected += 1  # duplicate bar
            continue
        if candles and timestamp <= candles[-1].timestamp:
            rejected += 1  # out-of-order bar
            continue
        try:
            candles.append(
                Candle(
                    timestamp=timestamp,
                    symbol=symbol,
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                    volume=float(volume),
                    timeframe=timeframe,
                )
            )
        except ValueError:
            rejected += 1  # inconsistent OHLC — never repaired
        seen.add(timestamp)

    if rejected:
        logger.warning(
            "discarded malformed/duplicate historical bars",
            extra={
                "structured": {
                    "event": "INVALID_MARKET_DATA",
                    "component": "market_data.history",
                    "rejected": rejected,
                }
            },
        )
    if not candles:
        raise HistoryError("no usable historical candles")
    candles.sort(key=lambda item: item.timestamp)
    return candles


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)