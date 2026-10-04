"""Breakout + momentum strategy (experimental, architecture-validation only).

LONG  : close breaks above the recent range high, momentum confirms, regime is
        trend-compatible, price is above VWAP, and the spread is acceptable.
SHORT : symmetric to the downside.

This strategy makes NO profitability claims. Thresholds come from configuration.
"""
from __future__ import annotations

from app.config.models import StrategyConfig
from app.core.ids import new_signal_id
from app.domain.enums import Direction, ReasonCode
from app.domain.signals import StrategySignal
from app.strategies.base import Strategy, StrategyContext


class BreakoutMomentumStrategy(Strategy):
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.name = config.name

    def evaluate(self, context: StrategyContext) -> StrategySignal | None:
        features = context.features
        close = features.close
        range_high = features.range_high
        range_low = features.range_low
        rsi = features.rsi
        atr = features.atr

        if close is None or range_high is None or range_low is None:
            return None
        if atr is None or atr <= 0:
            return None

        if not self._volatility_ok(atr):
            return None
        if not self._spread_ok(features.spread_percent):
            return None

        buffer = self.config.breakout.buffer_percent / 100.0
        above = range_high * (1.0 + buffer)
        below = range_low * (1.0 - buffer)

        if context.position_open:
            # One position at a time is enforced by risk; do not stack signals.
            return None

        if close > above and self._momentum_long(rsi) and self._trend_long(context):
            reasons = [
                ReasonCode.BREAKOUT_ABOVE_RANGE,
                ReasonCode.BULLISH_REGIME,
                ReasonCode.MOMENTUM_CONFIRMED,
            ]
            if self.config.trend.enabled and self._above_vwap(features):
                reasons.append(ReasonCode.TREND_CONFIRMED)
            if features.spread_percent is not None:
                reasons.append(ReasonCode.SPREAD_ACCEPTABLE)
            return self._signal(context, Direction.LONG, reasons, close)

        if close < below and self._momentum_short(rsi) and self._trend_short(context):
            reasons = [
                ReasonCode.BREAKOUT_BELOW_RANGE,
                ReasonCode.BEARISH_REGIME,
                ReasonCode.MOMENTUM_CONFIRMED,
            ]
            if self.config.trend.enabled and self._below_vwap(features):
                reasons.append(ReasonCode.TREND_CONFIRMED)
            if features.spread_percent is not None:
                reasons.append(ReasonCode.SPREAD_ACCEPTABLE)
            return self._signal(context, Direction.SHORT, reasons, close)

        return None

    # -- helpers ------------------------------------------------------------
    def _momentum_long(self, rsi: float | None) -> bool:
        if not self.config.momentum.enabled:
            return True
        return rsi is not None and self.config.momentum.rsi_min_long <= rsi <= self.config.momentum.rsi_max_long

    def _momentum_short(self, rsi: float | None) -> bool:
        if not self.config.momentum.enabled:
            return True
        return rsi is not None and self.config.momentum.rsi_min_short <= rsi <= self.config.momentum.rsi_max_short

    def _trend_long(self, context: StrategyContext) -> bool:
        if context.regime.is_trending and not context.regime.allows_long:
            return False
        if not self.config.trend.enabled:
            return True
        fast = context.features.ema(self.config.trend.fast_ema)
        slow = context.features.ema(self.config.trend.slow_ema)
        return fast is not None and slow is not None and fast > slow

    def _trend_short(self, context: StrategyContext) -> bool:
        if context.regime.is_trending and not context.regime.allows_short:
            return False
        if not self.config.trend.enabled:
            return True
        fast = context.features.ema(self.config.trend.fast_ema)
        slow = context.features.ema(self.config.trend.slow_ema)
        return fast is not None and slow is not None and fast < slow

    def _above_vwap(self, features) -> bool:
        vwap = features.vwap
        return vwap is None or (features.close is not None and features.close >= vwap)

    def _below_vwap(self, features) -> bool:
        vwap = features.vwap
        return vwap is None or (features.close is not None and features.close <= vwap)

    def _volatility_ok(self, atr: float) -> bool:
        cfg = self.config.volatility
        if cfg.maximum_atr and atr > cfg.maximum_atr:
            return False
        if cfg.minimum_atr and atr < cfg.minimum_atr:
            return False
        return True

    def _spread_ok(self, spread_percent: float | None) -> bool:
        if spread_percent is None:
            return True
        return spread_percent <= self.config.spread.maximum_percent

    def _signal(
        self,
        context: StrategyContext,
        direction: Direction,
        reasons: list[ReasonCode],
        entry_reference: float,
    ) -> StrategySignal:
        return StrategySignal(
            signal_id=new_signal_id(),
            timestamp=context.now,
            symbol=context.symbol,
            strategy=self.name,
            direction=direction,
            reason_code=reasons[0],
            reason=" AND ".join(code.value for code in reasons),
            entry_reference=entry_reference,
            timeframe=context.timeframe,
            regime=context.regime.regime,
            features=context.features.numeric_values(),
            correlation_id=context.correlation_id,
            session_id=context.session_id,
        )
