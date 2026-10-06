"""Session-VWAP feature tests (H1 approved design).

Covers the fail-closed numerical contract: accumulation, UTC reset,
zero/negative/NaN volume, gaps, determinism, naive timestamps, toggle,
snapshot population, readiness invariance, and rolling-VWAP isolation.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config.models import FeaturesConfig
from app.domain.market import Candle
from app.features import indicators as ind
from app.features.engine import FeatureEngine, session_vwap_value

UTC = timezone.utc


def _candle(day: int, hour: int, minute: int, *, o, h, l, c, v, naive=False):
    ts = datetime(2026, 10, day, hour, minute)
    if not naive:
        ts = ts.replace(tzinfo=UTC)
    return Candle(timestamp=ts, symbol="BTC/USD", open=o, high=h, low=l,
                  close=c, volume=v, timeframe="5m")


def _engine(enabled=True) -> FeatureEngine:
    config = FeaturesConfig()
    config.session_vwap.enabled = enabled
    return FeatureEngine(config)


# 1. first positive-volume bar --------------------------------------------------
def test_first_bar_with_volume_is_typical_price():
    bars = [_candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10)]
    assert session_vwap_value(bars) == pytest.approx(100.0)


# 2. first zero-volume bar --------------------------------------------------------
def test_first_bar_without_volume_is_none():
    bars = [_candle(1, 0, 5, o=100, h=110, l=90, c=100, v=0.0)]
    assert session_vwap_value(bars) is None


# 3-4. hand-calculated multi-bar cumulative weighting ------------------------------
def test_hand_calculated_two_bar_vwap():
    bars = [
        _candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10),   # typical 100
        _candle(1, 0, 10, o=95, h=100, l=80, c=90, v=30),    # typical 90
    ]
    # (100*10 + 90*30) / 40 = 92.5 — volume-weighted, not the simple mean 95.
    assert session_vwap_value(bars) == pytest.approx(92.5)
    assert session_vwap_value(bars) != pytest.approx(95.0)


# 5-6. UTC session reset / previous-day exclusion ------------------------------------
def test_session_reset_excludes_previous_day():
    bars = [
        _candle(1, 23, 55, o=100, h=110, l=90, c=100, v=100),  # typical 100, day 1
        _candle(2, 0, 5, o=50, h=60, l=40, c=50, v=10),        # typical 50, day 2
    ]
    assert session_vwap_value(bars) == pytest.approx(50.0)


# 7. zero-volume middle bar --------------------------------------------------------------
def test_zero_volume_middle_bar_contributes_nothing():
    bars = [
        _candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10),
        _candle(1, 0, 10, o=100, h=110, l=90, c=100, v=0.0),
        _candle(1, 0, 15, o=95, h=100, l=80, c=90, v=30),
    ]
    assert session_vwap_value(bars) == pytest.approx(92.5)


# 8. all-zero-volume session -------------------------------------------------------------------
def test_all_zero_volume_session_is_none():
    bars = [_candle(1, 0, 5 + 5 * i, o=100, h=110, l=90, c=100, v=0.0) for i in range(5)]
    assert session_vwap_value(bars) is None


# 9. negative volume --------------------------------------------------------------------------------
def test_negative_volume_is_none():
    bars = [
        _candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10),
        _candle(1, 0, 10, o=95, h=100, l=80, c=90, v=-3.0),
    ]
    assert session_vwap_value(bars) is None


# 10. NaN/inf --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_inputs_are_none(bad):
    bars = [
        _candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10),
        _candle(1, 0, 10, o=95, h=100, l=80, c=90, v=30),
    ]
    bars[1].volume = bad
    assert session_vwap_value(bars) is None


# 11. one-bar session ----------------------------------------------------------------------------------------
def test_one_bar_session():
    bars = [_candle(3, 12, 0, o=70, h=80, l=60, c=75, v=4)]
    assert session_vwap_value(bars) == pytest.approx((80 + 60 + 75) / 3.0)


# 12. gaps ----------------------------------------------------------------------------------------------------------
def test_missing_bars_are_not_interpolated():
    bars = [
        _candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10),
        # 00:10 missing entirely
        _candle(1, 0, 15, o=95, h=100, l=80, c=90, v=30),
    ]
    assert session_vwap_value(bars) == pytest.approx(92.5)


# 13. determinism -------------------------------------------------------------------------------------------------------
def test_repeated_calculation_is_identical():
    bars = [
        _candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10),
        _candle(1, 0, 10, o=95, h=100, l=80, c=90, v=30),
    ]
    assert session_vwap_value(bars) == session_vwap_value(bars)


# 14. naive timestamps ------------------------------------------------------------------------------------------------------
def test_naive_timestamps_treated_as_utc():
    aware = [
        _candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10),
        _candle(1, 0, 10, o=95, h=100, l=80, c=90, v=30),
    ]
    naive = [
        _candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10, naive=True),
        _candle(1, 0, 10, o=95, h=100, l=80, c=90, v=30, naive=True),
    ]
    assert session_vwap_value(naive) == pytest.approx(session_vwap_value(aware))


# 15. disabled toggle --------------------------------------------------------------------------------------------------------------
def test_disabled_toggle_leaves_key_absent():
    bars = [_candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10)]
    snapshot = _engine(enabled=False).compute(bars, timeframe="5m")
    assert snapshot.session_vwap is None
    assert "session_vwap" not in snapshot.numeric_values()


# 16-17. snapshot population + property ---------------------------------------------------------------------------------------------------
def test_snapshot_populated_and_property():
    bars = [
        _candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10),
        _candle(1, 0, 10, o=95, h=100, l=80, c=90, v=30),
    ]
    snapshot = _engine().compute(bars, timeframe="5m")
    assert snapshot.values["session_vwap"] == pytest.approx(92.5)
    assert snapshot.session_vwap == pytest.approx(92.5)


# 18. numeric_values behavior ---------------------------------------------------------------------------------------------------------------
def test_numeric_values_includes_session_vwap_when_present():
    bars = [_candle(1, 0, 5, o=100, h=110, l=90, c=100, v=10)]
    numeric = _engine().compute(bars, timeframe="5m").numeric_values()
    assert numeric["session_vwap"] == pytest.approx(100.0)


# 19. readiness unchanged -------------------------------------------------------------------------------------------------------------------------
def test_ready_does_not_require_session_vwap():
    from app.backtesting.data import generate_history

    candles = generate_history("BTC/USD", timeframe="5m", length=60, seed=7)
    snapshot = _engine(enabled=False).compute(candles, timeframe="5m")
    assert "session_vwap" not in snapshot.values
    assert snapshot.ready


# 20. rolling VWAP unchanged ----------------------------------------------------------------------------------------------------------------------------
def test_rolling_vwap_unaffected_by_session_vwap():
    from app.backtesting.data import generate_history

    candles = generate_history("BTC/USD", timeframe="5m", length=60, seed=7)
    snap = _engine().compute(candles, timeframe="5m")
    closes = [c.close for c in candles]
    expected = ind.vwap(
        [c.high for c in candles[-20:]],
        [c.low for c in candles[-20:]],
        closes[-20:],
        [c.volume for c in candles[-20:]],
    )
    assert snap.vwap == pytest.approx(expected)
