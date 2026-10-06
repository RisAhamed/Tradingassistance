"""Backtest performance metrics (pure, deterministic functions).

All metrics are computed from closed trades and the equity curve. No
look-ahead: every value is derived from information available at the time.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class BacktestMetrics:
    # Trade metrics
    net_pnl: float = 0.0
    return_percent: float = 0.0
    trade_count: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    win_rate: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    average_holding_seconds: float = 0.0
    maximum_holding_seconds: float = 0.0
    # P&L
    gross_pnl: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    # Risk
    max_drawdown: float = 0.0
    max_drawdown_percent: float = 0.0
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0
    sharpe: float = 0.0
    # Exposure
    max_position_size: float = 0.0
    max_position_value: float = 0.0
    max_simultaneous_exposure: float = 0.0
    percent_time_flat: float = 0.0
    percent_time_exposed: float = 0.0
    # Diagnostics
    rejected_trades: int = 0
    forced_exits: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def max_drawdown(equity_curve: list[float]) -> float:
    peak = float("-inf")
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak)
    return abs(worst) * 100.0


def max_drawdown_absolute(equity_curve: list[float]) -> float:
    peak = float("-inf")
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return abs(worst)


def sharpe_ratio(equity_curve: list[float], *, periods_per_year: float = 52560.0) -> float:
    if len(equity_curve) < 3:
        return 0.0
    returns = [
        equity_curve[i] / equity_curve[i - 1] - 1.0
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] != 0
    ]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = variance ** 0.5
    if std == 0:
        return 0.0
    return (mean / std) * (periods_per_year ** 0.5)


def _max_consecutive(values: list[float], predicate) -> int:
    best = current = 0
    for value in values:
        if predicate(value):
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def compute_metrics(
    *,
    closed_pnls: list[float],
    holding_seconds: list[float],
    equity_curve: list[float],
    initial_capital: float,
    fees: float = 0.0,
    slippage: float = 0.0,
    rejected_trades: int = 0,
    forced_exits: int = 0,
    position_sizes: list[float] | None = None,
    position_values: list[float] | None = None,
    exposure_flags: list[bool] | None = None,
) -> BacktestMetrics:
    wins = [p for p in closed_pnls if p > 0]
    losses = [p for p in closed_pnls if p < 0]
    breakevens = [p for p in closed_pnls if p == 0]
    net = sum(closed_pnls) - fees
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    avg_win = gross_wins / len(wins) if wins else 0.0
    avg_loss = gross_losses / len(losses) if losses else 0.0
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float(gross_wins > 0)
    total = len(closed_pnls)
    dd_pct = max_drawdown(equity_curve)
    dd_abs = max_drawdown_absolute(equity_curve)
    max_pos_size = max(position_sizes) if position_sizes else 0.0
    max_pos_value = max(position_values) if position_values else 0.0
    max_exposure = max_pos_value  # single-position strategy: max value = max exposure
    if exposure_flags:
        flat_count = sum(1 for f in exposure_flags if not f)
        pct_flat = (flat_count / len(exposure_flags)) * 100.0
        pct_exposed = 100.0 - pct_flat
    else:
        pct_flat = 100.0 if total == 0 else 0.0
        pct_exposed = 0.0 if total == 0 else 100.0
    return BacktestMetrics(
        net_pnl=round(net, 6),
        return_percent=round((net / initial_capital) * 100.0, 4) if initial_capital else 0.0,
        trade_count=total,
        winning_trades=len(wins),
        losing_trades=len(losses),
        breakeven_trades=len(breakevens),
        win_rate=round(len(wins) / total, 4) if total else 0.0,
        average_win=round(avg_win, 6),
        average_loss=round(avg_loss, 6),
        largest_win=round(max(wins), 6) if wins else 0.0,
        largest_loss=round(min(losses), 6) if losses else 0.0,
        profit_factor=round(profit_factor, 4),
        expectancy=round(net / total, 6) if total else 0.0,
        average_holding_seconds=round(sum(holding_seconds) / len(holding_seconds), 2) if holding_seconds else 0.0,
        maximum_holding_seconds=round(max(holding_seconds), 2) if holding_seconds else 0.0,
        gross_pnl=round(sum(closed_pnls), 6),
        fees=round(fees, 6),
        slippage=round(slippage, 6),
        max_drawdown=round(dd_abs, 4),
        max_drawdown_percent=round(dd_pct, 4),
        max_consecutive_losses=_max_consecutive(closed_pnls, lambda p: p < 0),
        max_consecutive_wins=_max_consecutive(closed_pnls, lambda p: p > 0),
        sharpe=round(sharpe_ratio(equity_curve), 4),
        max_position_size=round(max_pos_size, 8),
        max_position_value=round(max_pos_value, 2),
        max_simultaneous_exposure=round(max_exposure, 2),
        percent_time_flat=round(pct_flat, 2),
        percent_time_exposed=round(pct_exposed, 2),
        rejected_trades=rejected_trades,
        forced_exits=forced_exits,
    )
