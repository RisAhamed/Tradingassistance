"""Deterministic, rule-based timeframe selector.

Phase D: picks context / signal / execution timeframes from the
configured candidate universe using only information available at
or before the decision time (no look-ahead).

Selection inputs:
  * regime (trending vs range-bound)
  * volatility (ATR / close)
  * spread %
  * volume ratio
  * trend strength (EMA distance in ATR units)

All thresholds come from configuration — nothing is hard-coded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config.models import TimeframeSelectionConfig
from app.core.clock import utcnow
from app.domain.features import FeatureSnapshot
from app.domain.regime import RegimeSnapshot


@dataclass(slots=True, frozen=True)
class TimeframeSelection:
    context_timeframe: str
    signal_timeframe: str
    execution_timeframe: str
    reason: str
    method: str = "rule_based"
    inputs: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    blocked: bool = False


def _has_candles(features: FeatureSnapshot, timeframe: str, minimum: int = 10) -> bool:
    # The FeatureSnapshot carries candle_count for its own timeframe;
    # for other timeframes we conservatively assume availability when
    # the feature set is ready (historical coverage lives in the engine).
    return features.candle_count >= minimum


class TimeframeSelector:
    def __init__(self, config: TimeframeSelectionConfig) -> None:
        self.config = config

    def select(
        self,
        *,
        regime: RegimeSnapshot,
        features: FeatureSnapshot,
        snapshot: Any = None,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> TimeframeSelection:
        now = now or utcnow()
        reason_parts: list[str] = []
        blocked = False

        # Default fallbacks (static candidate heads).
        ctx = self.config.context_candidates.timeframes[0]
        sig = self.config.signal_candidates.timeframes[0]
        exe = self.config.execution_candidates.timeframes[0]

        # Blocked when data is unusable.
        if regime.is_unknown:
            blocked = True
            reason_parts.append("unknown_regime")
        if features.candle_count < self.config.context_candidates.min_candles:
            blocked = True
            reason_parts.append("insufficient_candles")

        volatility = features.volatility
        atr = features.atr
        close = features.close
        spread = features.spread_percent
        volume_ratio = features.volume_ratio

        vol_high = volatility is not None and volatility >= self.config.volatility_high_threshold
        vol_low = volatility is not None and volatility <= self.config.volatility_low_threshold
        spread_ok = spread is None or spread <= self.config.spread_max_percent
        liquidity_ok = volume_ratio is None or volume_ratio >= self.config.liquidity_min_volume_ratio

        # --- context: slower frames in strong trends, faster in range ---
        ctx_candidates = self.config.context_candidates.timeframes
        if not blocked and regime.is_trending and not regime.is_unknown:
            # Strong trend -> larger context window.
            ctx = ctx_candidates[-1]
            reason_parts.append("trend_context")
        elif not blocked and vol_high:
            ctx = ctx_candidates[0]  # faster context for volatile markets
            reason_parts.append("volatile_context")
        elif not blocked:
            ctx = ctx_candidates[max(len(ctx_candidates) // 2 - 1, 0)]
            reason_parts.append("default_context")

        # --- signal: match cadence or step up when volatility high ---
        sig_candidates = self.config.signal_candidates.timeframes
        if not blocked and vol_high and spread_ok:
            sig = sig_candidates[min(1, len(sig_candidates) - 1)]
            reason_parts.append("volatile_signal")
        elif not blocked:
            sig = sig_candidates[0]
            reason_parts.append("default_signal")

        # --- execution: faster when volatility high & spread tight ---
        exe_candidates = self.config.execution_candidates.timeframes
        if not blocked and vol_high and spread_ok and liquidity_ok:
            exe = exe_candidates[min(1, len(exe_candidates) - 1)]
            reason_parts.append("volatile_execution")
        elif not blocked:
            exe = exe_candidates[0]
            reason_parts.append("default_execution")

        # --- safety overrides ---
        if not spread_ok:
            blocked = True
            reason_parts.append("wide_spread")
        if not liquidity_ok:
            blocked = True
            reason_parts.append("poor_liquidity")

        reason = "; ".join(reason_parts) if reason_parts else "no_decision"
        return TimeframeSelection(
            context_timeframe=ctx,
            signal_timeframe=sig,
            execution_timeframe=exe,
            reason=reason,
            method=self.config.selection_method,
            inputs={
                "regime": regime.regime.value if regime else "unknown",
                "volatility": volatility,
                "atr": atr,
                "close": close,
                "spread_percent": spread,
                "volume_ratio": volume_ratio,
            },
            timestamp=now,
            blocked=blocked,
        )
