"""Patch script for app/config/models.py — Phase D (timeframes + tradeplan)."""
from __future__ import annotations

p = 'app/config/models.py'
s = open(p, encoding='utf-8').read()

old_tf = '''class TimeframesConfig(_Section):
    context: str = "15m"
    signal: str = "5m"
    execution: str = "1m"

class EmaConfig'''
new_tf = '''class TimeframeCandidateConfig(_Section):
    """Candidate timeframes for one slot plus the data minimum required."""

    timeframes: list[str] = Field(default_factory=list)
    min_candles: int = 10


class TimeframeSelectionConfig(_Section):
    """Dynamic timeframe selection (deterministic, rule-based, configurable)."""

    mode: Literal["dynamic", "static"] = "dynamic"
    context_candidates: TimeframeCandidateConfig = Field(
        default_factory=lambda: TimeframeCandidateConfig(timeframes=["15m", "30m", "1h"])
    )
    signal_candidates: TimeframeCandidateConfig = Field(
        default_factory=lambda: TimeframeCandidateConfig(timeframes=["1m", "3m", "5m", "15m"])
    )
    execution_candidates: TimeframeCandidateConfig = Field(
        default_factory=lambda: TimeframeCandidateConfig(timeframes=["1m", "3m"])
    )
    selection_method: Literal["rule_based"] = "rule_based"
    volatility_high_threshold: float = 0.02
    volatility_low_threshold: float = 0.005
    spread_max_percent: float = 0.10
    liquidity_min_volume_ratio: float = 0.5


class TradePlanConfig(_Section):
    """TradePlan construction policy (data/decision architecture only)."""

    enabled: bool = True
    stop_atr_multiplier: float = 2.0
    target_atr_multiplier: float = 3.0
    default_expected_holding_minutes: float = 30.0
    maximum_holding_minutes: float = 240.0


class TimeframesConfig(_Section):
    context: str = "15m"
    signal: str = "5m"
    execution: str = "1m"
    available: list[str] = Field(default_factory=lambda: ["1m", "3m", "5m", "15m", "30m", "1h"])
    selection: TimeframeSelectionConfig = Field(default_factory=TimeframeSelectionConfig)


class EmaConfig'''
assert old_tf in s, 'timeframes block missing'
s = s.replace(old_tf, new_tf)

open(p, 'w', encoding='utf-8').write(s)
print('timeframes patch OK')
