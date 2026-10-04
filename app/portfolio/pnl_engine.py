"""P&L engine — the authoritative tracker of realized/unrealized P&L and fees.

Realized P&L and fees are driven by *actual fills*, never by assumptions.
"""
from __future__ import annotations

from datetime import datetime

from app.core.clock import utcnow
from app.domain.pnl import PnlSnapshot


class PnlEngine:
    def __init__(self, symbol: str, *, starting_equity: float = 0.0) -> None:
        self.symbol = symbol
        self.starting_equity = starting_equity
        self.realized: float = 0.0
        self.unrealized: float = 0.0
        self.fees: float = 0.0
        self.daily_realized: float = 0.0

    def add_realized(self, amount: float, *, fee: float = 0.0) -> None:
        self.realized += amount
        self.daily_realized += amount
        self.fees += fee

    def add_fee(self, fee: float) -> None:
        self.fees += fee

    def set_equity(self, equity: float) -> None:
        self.starting_equity = equity

    def reset_daily(self) -> None:
        self.daily_realized = 0.0

    @property
    def equity(self) -> float:
        return self.starting_equity + self.realized - self.fees + self.unrealized

    def mark(self, unrealized: float, *, now: datetime | None = None) -> PnlSnapshot:
        self.unrealized = unrealized
        return PnlSnapshot(
            timestamp=now or utcnow(),
            symbol=self.symbol,
            realized=self.realized,
            unrealized=self.unrealized,
            fees=self.fees,
            daily_realized=self.daily_realized,
            equity=self.equity,
            cash=self.starting_equity + self.realized - self.fees,
        )
