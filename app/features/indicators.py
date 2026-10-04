"""Deterministic, dependency-light technical indicators.

All functions are pure and return lists aligned 1:1 with the input so results
are fully testable and reproducible.
"""
from __future__ import annotations

from collections.abc import Sequence

Number = float


def sma(values: Sequence[float], period: int) -> list[float | None]:
    """Simple moving average."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            out[index] = running / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average seeded with an SMA of the first ``period``."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    multiplier = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    previous = seed
    for index in range(period, len(values)):
        previous = (values[index] - previous) * multiplier + previous
        out[index] = previous
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder's Relative Strength Index."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[index] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def true_range(high: float, low: float, prev_close: float | None) -> float:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float | None]:
    """Wilder's Average True Range."""
    if period <= 0:
        raise ValueError("period must be positive")
    count = len(closes)
    out: list[float | None] = [None] * count
    if count < period:
        return out
    trs: list[float] = []
    for index in range(count):
        prev_close = closes[index - 1] if index > 0 else None
        trs.append(true_range(highs[index], lows[index], prev_close))
    first = sum(trs[:period]) / period
    out[period - 1] = first
    previous = first
    for index in range(period, count):
        previous = (previous * (period - 1) + trs[index]) / period
        out[index] = previous
    return out


def vwap(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
) -> float | None:
    """Volume-weighted average price over the supplied window."""
    total_volume = sum(volumes)
    if total_volume <= 0:
        typical = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(closes))]
        return sum(typical) / len(typical) if typical else None
    weighted = sum(
        ((highs[i] + lows[i] + closes[i]) / 3.0) * volumes[i] for i in range(len(closes))
    )
    return weighted / total_volume


def rolling_high(values: Sequence[float], lookback: int) -> float | None:
    window = values[-lookback:]
    return max(window) if window else None


def rolling_low(values: Sequence[float], lookback: int) -> float | None:
    window = values[-lookback:]
    return min(window) if window else None


def short_return(closes: Sequence[float], lookback: int = 1) -> float | None:
    """Simple return over ``lookback`` bars."""
    if len(closes) <= lookback or closes[-lookback - 1] == 0:
        return None
    return closes[-1] / closes[-lookback - 1] - 1.0


def returns_std(closes: Sequence[float], lookback: int = 20) -> float | None:
    """Population standard deviation of simple returns over the window."""
    if len(closes) < 3:
        return None
    window = closes[-(lookback + 1):]
    rets = [
        window[i] / window[i - 1] - 1.0
        for i in range(1, len(window))
        if window[i - 1] != 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / len(rets)
    return variance ** 0.5


def volume_ratio(volumes: Sequence[float], lookback: int = 20) -> float | None:
    window = volumes[-lookback:]
    if not window:
        return None
    average = sum(window) / len(window)
    if average == 0:
        return None
    return volumes[-1] / average


def crossover(fast: Sequence[float | None], slow: Sequence[float | None]) -> int:
    """Return 1 on bullish cross, -1 on bearish cross, 0 otherwise (last bar)."""
    if len(fast) < 2 or len(slow) < 2:
        return 0
    f_now, f_prev = fast[-1], fast[-2]
    s_now, s_prev = slow[-1], slow[-2]
    if None in (f_now, s_now, f_prev, s_prev):
        return 0
    assert f_now is not None and f_prev is not None  # for type checkers
    assert s_now is not None and s_prev is not None
    if f_prev <= s_prev and f_now > s_now:
        return 1
    if f_prev >= s_prev and f_now < s_now:
        return -1
    return 0
