"""P&L snapshots and completed-trade records."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import Direction


class PnlSnapshot(BaseModel):
    timestamp: datetime
    symbol: str
    realized: float = 0.0
    unrealized: float = 0.0
    fees: float = 0.0
    daily_realized: float = 0.0
    equity: float = 0.0
    cash: float = 0.0

    @property
    def gross(self) -> float:
        return self.realized + self.unrealized

    @property
    def net(self) -> float:
        return self.gross - self.fees


class TradeRecord(BaseModel):
    trade_id: str
    symbol: str
    direction: Direction
    quantity: float
    entry_price: float
    exit_price: float
    realized_pnl: float
    fees: float = 0.0
    opened_at: datetime
    closed_at: datetime
    holding_seconds: float = 0.0
    signal_id: str | None = None
    entry_order_id: str | None = None
    exit_order_id: str | None = None
    reason: str | None = None
    correlation_id: str | None = None
    session_id: str | None = None

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl - self.fees
