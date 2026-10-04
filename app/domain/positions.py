"""Position domain model maintained by the Position Manager."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import Direction


class Position(BaseModel):
    position_id: str
    symbol: str
    direction: Direction = Direction.FLAT
    quantity: float = 0.0
    average_entry: float = 0.0
    current_price: float = 0.0
    stop: float | None = None
    target: float | None = None
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fees: float = 0.0
    opened_at: datetime | None = None
    updated_at: datetime | None = None
    correlation_id: str | None = None
    session_id: str | None = None

    @property
    def is_flat(self) -> bool:
        return self.direction is Direction.FLAT or self.quantity == 0.0

    @property
    def is_open(self) -> bool:
        return not self.is_flat

    @property
    def market_value(self) -> float:
        return abs(self.quantity) * self.current_price

    def holding_seconds(self, now: datetime) -> float:
        if self.opened_at is None:
            return 0.0
        return (now - self.opened_at).total_seconds()

    def compute_unrealized(self) -> float:
        """Mark-to-market the open position at ``current_price``."""
        if self.is_flat or self.average_entry == 0.0:
            self.unrealized_pnl = 0.0
            return 0.0
        sign = 1.0 if self.direction is Direction.LONG else -1.0
        self.unrealized_pnl = sign * (self.current_price - self.average_entry) * abs(self.quantity)
        return self.unrealized_pnl

    @classmethod
    def flat(cls, symbol: str, *, position_id: str, session_id: str | None = None) -> "Position":
        return cls(position_id=position_id, symbol=symbol, session_id=session_id)
