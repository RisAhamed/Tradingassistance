"""Performance analyzer: breaks down backtest results by regime, timeframe,
exit reason, direction, and session. Pure functions over trade records."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(slots=True)
class BreakdownRow:
    label: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    average_trade: float = 0.0
    average_holding_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "net_pnl": self.net_pnl,
            "profit_factor": self.profit_factor,
            "max_drawdown": self.max_drawdown,
            "average_trade": self.average_trade,
            "average_holding_seconds": self.average_holding_seconds,
        }


def _build_row(label: str, trades: list[dict]) -> BreakdownRow:
    if not trades:
        return BreakdownRow(label=label)
    wins = [t for t in trades if t.get("net_pnl", 0) > 0]
    losses = [t for t in trades if t.get("net_pnl", 0) < 0]
    gross_wins = sum(t["net_pnl"] for t in wins)
    gross_losses = abs(sum(t["net_pnl"] for t in losses))
    pf = (gross_wins / gross_losses) if gross_losses > 0 else float(gross_wins > 0)
    net = sum(t.get("net_pnl", 0) for t in trades)
    holdings = [t.get("holding_seconds", 0) for t in trades]
    return BreakdownRow(
        label=label,
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(len(wins) / len(trades), 4),
        net_pnl=round(net, 6),
        profit_factor=round(pf, 4),
        max_drawdown=0.0,
        average_trade=round(net / len(trades), 6),
        average_holding_seconds=round(sum(holdings) / len(holdings), 2) if holdings else 0.0,
    )


def analyze(trades: list[dict]) -> dict:
    """Break down trades by regime, timeframe, exit reason, direction, day."""
    by_regime: dict[str, list[dict]] = defaultdict(list)
    by_timeframe: dict[str, list[dict]] = defaultdict(list)
    by_exit_reason: dict[str, list[dict]] = defaultdict(list)
    by_direction: dict[str, list[dict]] = defaultdict(list)
    by_day: dict[str, list[dict]] = defaultdict(list)

    for trade in trades:
        by_regime[trade.get("regime", "unknown")].append(trade)
        by_timeframe[trade.get("timeframe", "unknown")].append(trade)
        by_exit_reason[trade.get("exit_reason", "unknown")].append(trade)
        by_direction[trade.get("side", "unknown")].append(trade)
        entry_time = trade.get("entry_time", "")
        day = entry_time[:10] if entry_time else "unknown"
        by_day[day].append(trade)

    return {
        "by_regime": {k: _build_row(k, v).to_dict() for k, v in sorted(by_regime.items())},
        "by_timeframe": {k: _build_row(k, v).to_dict() for k, v in sorted(by_timeframe.items())},
        "by_exit_reason": {k: _build_row(k, v).to_dict() for k, v in sorted(by_exit_reason.items())},
        "by_direction": {k: _build_row(k, v).to_dict() for k, v in sorted(by_direction.items())},
        "by_day": {k: _build_row(k, v).to_dict() for k, v in sorted(by_day.items())},
    }
