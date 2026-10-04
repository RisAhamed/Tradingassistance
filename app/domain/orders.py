"""Order lifecycle domain models (OMS)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import Direction, OrderStatus, OrderType, Side


class OrderIntent(BaseModel):
    """An intent to trade, created by execution *after* risk approval."""

    order_id: str
    client_order_id: str
    timestamp: datetime
    symbol: str
    side: Side
    direction: Direction
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_reference: float | None = None
    take_profit_reference: float | None = None
    signal_id: str | None = None
    correlation_id: str | None = None
    session_id: str | None = None
    reason: str | None = None


class Order(BaseModel):
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    direction: Direction
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    status: OrderStatus = OrderStatus.NEW
    broker_order_id: str | None = None
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    submitted_at: datetime | None = None
    updated_at: datetime | None = None
    reject_reason: str | None = None
    signal_id: str | None = None
    correlation_id: str | None = None
    session_id: str | None = None

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def is_filled(self) -> bool:
        return self.status is OrderStatus.FILLED


class Fill(BaseModel):
    fill_id: str
    order_id: str
    timestamp: datetime
    symbol: str
    side: Side
    quantity: float
    price: float
    fee: float = 0.0
    slippage: float = 0.0
    broker_fill_id: str | None = None
