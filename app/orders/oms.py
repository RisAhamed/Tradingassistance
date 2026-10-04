"""Order Management System (OMS).

Owns the order state machine and is the single place from which orders are
submitted. Prevents duplicate submission and tracks every fill.
"""
from __future__ import annotations

import logging

from app.brokers.base import BrokerAdapter, BrokerExecution
from app.core.clock import utcnow
from app.core.ids import new_order_id
from app.domain.enums import OrderStatus
from app.domain.orders import Fill, Order, OrderIntent

logger = logging.getLogger("app.orders.oms")


class DuplicateOrderError(Exception):
    """Raised when the same client order id is submitted twice."""


class OMS:
    def __init__(self, broker: BrokerAdapter, *, duplicate_protection: bool = True) -> None:
        self.broker = broker
        self.duplicate_protection = duplicate_protection
        self._orders: dict[str, Order] = {}
        self._by_client_id: dict[str, str] = {}
        self._submitted: set[str] = set()

    # -- creation -----------------------------------------------------------
    def create(self, intent: OrderIntent) -> Order:
        order = Order(
            order_id=intent.order_id or new_order_id(),
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            direction=intent.direction,
            quantity=intent.quantity,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            status=OrderStatus.NEW,
            signal_id=intent.signal_id,
            correlation_id=intent.correlation_id,
            session_id=intent.session_id,
        )
        self._orders[order.order_id] = order
        self._by_client_id[order.client_order_id] = order.order_id
        return order

    def validate(self, order: Order) -> bool:
        if order.quantity <= 0:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "invalid_quantity"
            return False
        order.status = OrderStatus.VALIDATED
        return True

    # -- submission ---------------------------------------------------------
    async def submit(self, order: Order) -> tuple[Order, BrokerExecution]:
        if self.duplicate_protection and order.order_id in self._submitted:
            raise DuplicateOrderError(f"order {order.order_id} already submitted")
        if order.status not in (OrderStatus.NEW, OrderStatus.VALIDATED):
            raise ValueError(f"cannot submit order in state {order.status}")

        self._submitted.add(order.order_id)
        order.status = OrderStatus.SUBMITTED
        order.submitted_at = utcnow()

        execution = await self.broker.submit_order(order)
        order.broker_order_id = execution.broker_order_id
        order.updated_at = utcnow()

        if execution.rejected:
            order.status = OrderStatus.REJECTED
            order.reject_reason = execution.reject_reason or "rejected"
            return order, execution

        order.status = OrderStatus.ACKNOWLEDGED
        for fill in execution.fills:
            self.apply_fill(order, fill)
        return order, execution

    def apply_fill(self, order: Order, fill: Fill) -> Order:
        total_qty = order.filled_quantity + fill.quantity
        if total_qty > 0 and order.average_fill_price is not None:
            weighted = order.average_fill_price * order.filled_quantity + fill.price * fill.quantity
            order.average_fill_price = weighted / total_qty
        else:
            order.average_fill_price = fill.price
        order.filled_quantity = total_qty
        order.updated_at = fill.timestamp
        if order.filled_quantity >= order.quantity - 1e-12:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        return order

    # -- cancellation / state ----------------------------------------------
    async def cancel(self, order: Order) -> Order:
        order.status = OrderStatus.CANCEL_REQUESTED
        order = await self.broker.cancel_order(order)
        order.updated_at = utcnow()
        return order

    def mark_rejected(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        order.updated_at = utcnow()
        return order

    # -- queries ------------------------------------------------------------
    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def get_by_client_id(self, client_order_id: str) -> Order | None:
        order_id = self._by_client_id.get(client_order_id)
        return self._orders.get(order_id) if order_id else None

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if not o.is_terminal]

    def was_submitted(self, client_order_id: str) -> bool:
        order_id = self._by_client_id.get(client_order_id)
        return bool(order_id and order_id in self._submitted)
