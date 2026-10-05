"""Cadence-aware freshness policy.

The effective threshold for bars is:
    max(configured_maximum_age, expected_interval + grace_period)
so a healthy 60 s bar stream is never falsely stale between arrivals.
Quote/trade streams keep their own independent ceilings.

All thresholds are read from configuration — nothing trading-critical is
hard-coded here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.config.models import DataFreshnessConfig

FUTURE_TOLERANCE_SECONDS = 60.0  # messages more than 60 s in the future are suspicious


@dataclass(slots=True, frozen=True)
class FreshnessResult:
    source: str  # bar | quote | trade
    age_seconds: float | None
    expected_interval_seconds: float | None
    effective_threshold_seconds: float
    is_stale: bool
    reason: str
    future_timestamp: bool = False
    clock_skew_seconds: float = 0.0


class FreshnessPolicy:
    def __init__(self, config: DataFreshnessConfig) -> None:
        self.config = config

    def evaluate(
        self,
        *,
        source: str,
        last_received: datetime | None,
        now: datetime,
        expected_interval_seconds: float | None = None,
    ) -> FreshnessResult:
        if last_received is None:
            return FreshnessResult(
                source=source,
                age_seconds=None,
                expected_interval_seconds=expected_interval_seconds,
                effective_threshold_seconds=self._threshold_for(source),
                is_stale=True,
                reason="no_data",
            )

        age = (now - last_received).total_seconds()
        skew = 0.0
        future = False
        if age < 0:
            # Clock skew: last_received is ahead of now. Observed but
            # treated as age 0 so it never silently becomes valid stale data.
            skew = abs(age)
            age = 0.0
            if skew > FUTURE_TOLERANCE_SECONDS:
                future = True

        effective = self._threshold_for(source)
        if source == "bar" and expected_interval_seconds:
            cadence_floor = expected_interval_seconds + self.config.bar.grace_period_seconds
            effective = max(effective, cadence_floor)

        stale = age > effective
        reason = "stale" if stale else "fresh"
        if future:
            reason = "future_timestamp"
        elif skew > 0:
            reason = "clock_skew"

        return FreshnessResult(
            source=source,
            age_seconds=age,
            expected_interval_seconds=expected_interval_seconds,
            effective_threshold_seconds=effective,
            is_stale=stale,
            reason=reason,
            future_timestamp=future,
            clock_skew_seconds=skew,
        )

    def _threshold_for(self, source: str) -> float:
        if source == "bar":
            return self.config.bar.maximum_age_seconds
        if source == "quote":
            return self.config.quote.maximum_age_seconds
        if source == "trade":
            return self.config.trade.maximum_age_seconds
        return self.config.bar.maximum_age_seconds

    def stale_action(self) -> Literal["block_entries", "warn_only"]:
        return self.config.stale_action
