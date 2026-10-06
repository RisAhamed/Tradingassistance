"""Feature engine: turns normalized candles into a FeatureSnapshot.

This module never makes trading decisions — it only computes deterministic,
testable numeric features that the regime/strategy layers consume.
"""
from __future__ import annotations

import math
from datetime import datetime

from app.config.models import FeaturesConfig
from app.core.clock import utcnow
from app.domain.features import FeatureSnapshot
from app.domain.market import Candle, MarketSnapshot
from app.features import indicators as ind


def session_vwap_value(candles: list[Candle]) -> float | None:
    """Cumulative VWAP over the current UTC session date (H1 anchor).

    Pure function of the passed closed-bar window: bars on the same UTC
    calendar date as the last bar, up to and including it. The session is
    the configured Trading GOAT session/day boundary (UTC 00:00 → 23:59),
    so a date change is a hard reset with no carry across sessions.

    Fail-closed contract (no fabricated fair value):
      - zero total session volume → None (deliberately NO unweighted-mean
        fallback — unlike the rolling ``ind.vwap``);
      - negative volume, NaN/inf price or volume → None;
      - timezone-naive timestamps are treated as UTC (CSV loader convention);
      - missing bars are ignored, never interpolated; repeated timestamps
        are not deduplicated here (uniqueness is an upstream data-quality
        guarantee in both live and backtest paths).
    """
    if not candles:
        return None
    day = candles[-1].timestamp.date()
    session = [c for c in candles if c.timestamp.date() == day]
    if not session:
        return None
    for bar in session:
        if not (
            math.isfinite(bar.high)
            and math.isfinite(bar.low)
            and math.isfinite(bar.close)
            and math.isfinite(bar.volume)
        ):
            return None
        if bar.volume < 0:
            return None
    total_volume = sum(bar.volume for bar in session)
    if total_volume <= 0:
        return None
    return ind.vwap(
        [bar.high for bar in session],
        [bar.low for bar in session],
        [bar.close for bar in session],
        [bar.volume for bar in session],
    )


class FeatureEngine:
    def __init__(self, config: FeaturesConfig) -> None:
        self.config = config

    def compute(
        self,
        candles: list[Candle],
        *,
        timeframe: str,
        snapshot: MarketSnapshot | None = None,
        now: datetime | None = None,
    ) -> FeatureSnapshot:
        """Compute the latest feature snapshot from closed candles."""
        now = now or utcnow()
        if not candles:
            return FeatureSnapshot(
                symbol=snapshot.symbol if snapshot else "",
                timestamp=now,
                timeframe=timeframe,
                spread_percent=snapshot.spread_percent if snapshot else None,
                data_age_seconds=snapshot.age_seconds(now) if snapshot else None,
            )

        symbol = candles[-1].symbol
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]
        lookback = self.config.range.lookback_periods
        values: dict[str, float | None] = {}

        if self.config.ema.enabled:
            for period in self.config.ema.periods:
                series = ind.ema(closes, period)
                values[f"ema_{period}"] = series[-1]

        if self.config.rsi.enabled:
            values["rsi"] = ind.rsi(closes, self.config.rsi.period)[-1]

        if self.config.atr.enabled:
            values["atr"] = ind.atr(highs, lows, closes, self.config.atr.period)[-1]

        if self.config.vwap.enabled:
            window = min(len(candles), max(lookback, 1))
            values["vwap"] = ind.vwap(
                highs[-window:], lows[-window:], closes[-window:], volumes[-window:]
            )

        if self.config.session_vwap.enabled:
            values["session_vwap"] = session_vwap_value(candles)

        prior_highs = highs[:-1] if len(highs) > 1 else highs
        prior_lows = lows[:-1] if len(lows) > 1 else lows
        values["range_high"] = ind.rolling_high(prior_highs, lookback)
        values["range_low"] = ind.rolling_low(prior_lows, lookback)
        values["return_short"] = ind.short_return(closes, 1)

        if self.config.volatility.enabled:
            values["volatility"] = ind.returns_std(closes, lookback)

        if self.config.volume.enabled:
            values["volume_ratio"] = ind.volume_ratio(volumes, lookback)

        return FeatureSnapshot(
            symbol=symbol,
            timestamp=candles[-1].timestamp,
            timeframe=timeframe,
            close=closes[-1],
            high=highs[-1],
            low=lows[-1],
            spread_percent=snapshot.spread_percent if snapshot else None,
            data_age_seconds=snapshot.age_seconds(now) if snapshot else None,
            candle_count=len(candles),
            values=values,
        )
