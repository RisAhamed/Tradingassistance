"""Deterministic market-regime classifier.

If conditions are ambiguous the regime is UNKNOWN — a regime is never forced.
"""
from __future__ import annotations

from datetime import datetime

from app.config.models import RegimeConfig
from app.core.clock import utcnow
from app.domain.enums import Regime
from app.domain.features import FeatureSnapshot
from app.domain.regime import RegimeSnapshot


class RegimeEngine:
    def __init__(
        self,
        config: RegimeConfig,
        *,
        fast_period: int = 20,
        slow_period: int = 50,
    ) -> None:
        self.config = config
        self.fast_period = fast_period
        self.slow_period = slow_period

    def classify(
        self,
        features: FeatureSnapshot,
        *,
        now: datetime | None = None,
    ) -> RegimeSnapshot:
        now = now or utcnow()
        base = RegimeSnapshot(
            symbol=features.symbol,
            timestamp=features.timestamp,
            timeframe=features.timeframe,
        )

        if features.candle_count < self.config.min_candles:
            base.regime = Regime.UNKNOWN
            base.reason = f"insufficient_candles({features.candle_count}<{self.config.min_candles})"
            return base

        fast = features.ema(self.fast_period)
        slow = features.ema(self.slow_period)
        atr = features.atr
        close = features.close

        if fast is None or slow is None or atr is None or close in (None, 0):
            base.regime = Regime.UNKNOWN
            base.reason = "missing_indicators"
            return base

        assert close is not None  # for type checkers
        ema_distance = abs(fast - slow)
        atr_units = (ema_distance / atr) if atr > 0 else 0.0
        volatility_percent = atr / close if close else 0.0
        detail = {
            "ema_fast": fast,
            "ema_slow": slow,
            "atr": atr,
            "close": close,
            "ema_distance_atr": atr_units,
            "volatility_percent": volatility_percent,
        }

        if atr_units >= self.config.trending.ema_distance_atr_min:
            if fast > slow:
                base.regime = Regime.TRENDING_BULLISH
                base.reason = "ema_distance_atr_up"
            else:
                base.regime = Regime.TRENDING_BEARISH
                base.reason = "ema_distance_atr_down"
        elif volatility_percent >= self.config.volatility.high_volatility_percent:
            base.regime = Regime.HIGH_VOLATILITY
            base.reason = "atr_percent_high"
        elif volatility_percent <= self.config.volatility.low_volatility_percent:
            base.regime = Regime.LOW_VOLATILITY
            base.reason = "atr_percent_low"
        else:
            base.regime = Regime.RANGE_BOUND
            base.reason = "no_trend_moderate_volatility"

        base.detail = detail
        return base
