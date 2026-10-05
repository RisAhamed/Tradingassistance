"""Phase D — TimeframeSelector unit tests."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.config.models import TimeframeSelectionConfig, TimeframeCandidateConfig
from app.decision.selector import TimeframeSelector, TimeframeSelection
from app.domain.enums import Regime
from app.domain.features import FeatureSnapshot
from app.domain.regime import RegimeSnapshot


def _features(**overrides) -> FeatureSnapshot:
    defaults = dict(
        symbol="BTC/USD",
        timestamp=datetime.utcnow(),
        timeframe="5m",
        close=100.0,
        high=101.0,
        low=99.0,
        candle_count=50,
        ema_20=100.0,
        ema_50=99.0,
        rsi=55.0,
        atr=1.0,
        vwap=99.5,
        range_high=105.0,
        range_low=95.0,
        spread_percent=0.05,
        volatility=0.015,
        volume_ratio=1.2,
        values={},
        data_age_seconds=1.0,
        ready=True,
        missing=[],
    )
    defaults.update(overrides)
    return FeatureSnapshot(**defaults)


def _regime(regime: Regime = Regime.TRENDING_BULLISH) -> RegimeSnapshot:
    return RegimeSnapshot(symbol="BTC/USD", timestamp=datetime.utcnow(), timeframe="15m", regime=regime, reason="test")


@pytest.fixture()
def selector() -> TimeframeSelector:
    return TimeframeSelector(
        TimeframeSelectionConfig(
            context_candidates=TimeframeCandidateConfig(timeframes=["15m", "30m", "1h"]),
            signal_candidates=TimeframeCandidateConfig(timeframes=["1m", "3m", "5m", "15m"]),
            execution_candidates=TimeframeCandidateConfig(timeframes=["1m", "3m"]),
        )
    )


def test_selector_returns_context_signal_execution(selector: TimeframeSelector) -> None:
    sel = selector.select(regime=_regime(), features=_features())
    assert sel.context_timeframe in selector.config.context_candidates.timeframes
    assert sel.signal_timeframe in selector.config.signal_candidates.timeframes
    assert sel.execution_timeframe in selector.config.execution_candidates.timeframes


def test_selector_blocks_on_unknown_regime(selector: TimeframeSelector) -> None:
    sel = selector.select(regime=_regime(Regime.UNKNOWN), features=_features())
    assert sel.blocked
    assert "unknown_regime" in sel.reason


def test_selector_blocks_on_wide_spread(selector: TimeframeSelector) -> None:
    sel = selector.select(regime=_regime(), features=_features(spread_percent=0.50))
    assert sel.blocked
    assert "wide_spread" in sel.reason


def test_selector_blocks_insufficient_candles(selector: TimeframeSelector) -> None:
    sel = selector.select(regime=_regime(), features=_features(candle_count=3))
    assert sel.blocked
    assert "insufficient_candles" in sel.reason


def test_selector_reason_contains_method(selector: TimeframeSelector) -> None:
    sel = selector.select(regime=_regime(), features=_features())
    assert sel.method == "rule_based"


def test_volatile_market_picks_faster_context(selector: TimeframeSelector) -> None:
    sel = selector.select(regime=_regime(Regime.RANGE_BOUND), features=_features(volatility=0.03))
    assert sel.context_timeframe == "15m"  # first candidate for volatile context
