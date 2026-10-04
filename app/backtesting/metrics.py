"""Backtest performance metrics (pure, deterministic functions)."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class BacktestMetrics:
    net_pnl: float = 0.0
    return_percent: float = 0.0
    trade_count: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    average_holding_seconds: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
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
) -> BacktestMetrics:
    wins = [p for p in closed_pnls if p > 0]
    losses = [p for p in closed_pnls if p < 0]
    net = sum(closed_pnls) - fees
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    avg_win = gross_wins / len(wins) if wins else 0.0
    avg_loss = gross_losses / len(losses) if losses else 0.0
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float(gross_wins > 0)
    total = len(closed_pnls)
    return BacktestMetrics(
        net_pnl=round(net, 6),
        return_percent=round((net / initial_capital) * 100.0, 4) if initial_capital else 0.0,
        trade_count=total,
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=round(len(wins) / total, 4) if total else 0.0,
        average_win=round(avg_win, 6),
        average_loss=round(avg_loss, 6),
        expectancy=round(net / total, 6) if total else 0.0,
        profit_factor=round(profit_factor, 4),
        max_drawdown=round(max_drawdown(equity_curve), 4),
        sharpe=round(sharpe_ratio(equity_curve), 4),
        average_holding_seconds=round(sum(holding_seconds) / len(holding_seconds), 2) if holding_seconds else 0.0,
        fees=round(fees, 6),
        slippage=round(slippage, 6),
        rejected_trades=rejected_trades,
        forced_exits=forced_exits,
    )
