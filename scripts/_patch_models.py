"""Patch script for app/config/models.py — Phase D."""
from __future__ import annotations

p = 'app/config/models.py'
s = open(p, encoding='utf-8').read()

# 1. Insert freshness classes before DataFreshnessConfig
anchor1 = 'class DataFreshnessConfig(_Section):'
freshness_classes = '''class FreshnessStreamConfig(_Section):
    """Per-stream freshness ceiling (seconds)."""

    maximum_age_seconds: float = 90.0


class FreshnessBarConfig(_Section):
    """Cadence-aware freshness for the canonical bar stream."""

    expected_interval_seconds: float = 60.0
    grace_period_seconds: float = 30.0
    maximum_age_seconds: float = 90.0


'''
assert anchor1 in s, 'anchor1 missing'
s = s.replace(anchor1, freshness_classes + anchor1)

# 2. Replace flat freshness body with cadence-aware body
old_freshness_body = '''    threshold_seconds: float = 30.0            # primary: live 1m bars
    quote_threshold_seconds: float = 30.0
    trade_threshold_seconds: float = 300.0      # trades are sparse on crypto
    stale_action: Literal["block_entries", "warn_only"] = "block_entries"'''
new_freshness_body = '''    mode: Literal["cadence_aware", "static"] = "cadence_aware"
    bar: FreshnessBarConfig = Field(default_factory=FreshnessBarConfig)
    quote: FreshnessStreamConfig = Field(default_factory=FreshnessStreamConfig)
    trade: FreshnessStreamConfig = Field(default_factory=FreshnessStreamConfig)
    stale_action: Literal["block_entries", "warn_only"] = "block_entries"
    # Deprecated flat knobs kept for backward compatibility; the nested
    # fields above are authoritative when mode == "cadence_aware".
    threshold_seconds: float = 30.0
    quote_threshold_seconds: float = 30.0
    trade_threshold_seconds: float = 300.0

    @model_validator(mode="after")
    def _migrate_flat_thresholds(self) -> "DataFreshnessConfig":
        if self.mode == "cadence_aware":
            if self.bar.maximum_age_seconds == 90.0 and self.threshold_seconds != 30.0:
                object.__setattr__(self.bar, "maximum_age_seconds", self.threshold_seconds)
            if self.quote.maximum_age_seconds == 90.0 and self.quote_threshold_seconds != 30.0:
                object.__setattr__(self.quote, "maximum_age_seconds", self.quote_threshold_seconds)
            if self.trade.maximum_age_seconds == 90.0 and self.trade_threshold_seconds != 300.0:
                object.__setattr__(self.trade, "maximum_age_seconds", self.trade_threshold_seconds)
        return self'''
assert old_freshness_body in s, 'freshness body missing'
s = s.replace(old_freshness_body, new_freshness_body)

# 3. Extend TimeframesConfig + add selection/tradeplan configs
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
print('models patched OK')
