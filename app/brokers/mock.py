"""In-memory simulated broker for deterministic tests and offline demos.

It reuses :class:`PositionManager` so the broker-side position bookkeeping
follows exactly the same rules as the engine's own position manager.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from app.brokers.base import AccountInfo, BrokerAdapter, BrokerExecution, BrokerHealth
from app.core.clock import utcnow
from app.core.ids import new_fill_id, new_order_id
from app.domain.enums import OrderStatus, Side
from app.domain.orders import Fill, Order
from app.domain.positions import Position
from app.portfolio.position_manager import PositionManager

logger = logging.getLogger("app.brokers.mock")


class MockBroker(BrokerAdapter):
    name = "mock"
    paper = True

    def __init__(
        self,
        *,
        starting_equity: float = 100000.0,
        fee_percent: float = 0.0,
        slippage_percent: float = 0.0,
        price_provider: Callable[[str], float | None] | None = None,
    ) -> None:
        self.starting_equity = starting_equity
        self.fee_percent = fee_percent
        self.slippage_percent = slippage_percent
        self._price_provider = price_provider
        self._prices: dict[str, float] = {}
        self._positions: dict[str, PositionManager] = {}
        self._orders: dict[str, Order] = {}
        self._cash = starting_equity
        self._connected = False

    # -- lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def health(self) -> BrokerHealth:
        return BrokerHealth(connected=self._connected, detail="simulated broker")

    # -- price seam ---------------------------------------------------------
    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def _resolve_price(self, symbol: str) -> float | None:
        if symbol in self._prices:
            return self._prices[symbol]
        if self._price_provider is not None:
            return self._price_provider(symbol)
        return None

    # -- account / positions ------------------------------------------------
    async def get_account(self) -> AccountInfo:
        return AccountInfo(equity=self._cash, cash=self._cash, buying_power=self._cash)

    async def get_positions(self) -> list[Position]:
        return [pm.position for pm in self._positions.values() if not pm.is_flat]

    async def get_orders(self, *, status: str | None = None) -> list[Order]:
        orders = list(self._orders.values())
        if status:
            orders = [o for o in orders if o.status.value == status]
        return orders

    # -- order flow ---------------------------------------------------------
    async def submit_order(self, order: Order) -> BrokerExecution:
        if not self._connected:
            return BrokerExecution(order.order_id, OrderStatus.REJECTED, reject_reason="not_connected")
        price = self._resolve_price(order.symbol)
        if price is None or price <= 0:
            return BrokerExecution(order.order_id, OrderStatus.REJECTED, reject_reason="no_price")
        if order.quantity <= 0:
            return BrokerExecution(order.order_id, OrderStatus.REJECTED, reject_reason="invalid_quantity")

        slippage = price * (self.slippage_percent / 100.0)
        fill_price = price + slippage if order.side is Side.BUY else price - slippage
        notional = fill_price * order.quantity
        fee = notional * (self.fee_percent / 100.0)
        broker_order_id = new_order_id()
        fill = Fill(
            fill_id=new_fill_id(),
            order_id=order.order_id,
            timestamp=utcnow(),
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            fee=fee,
            slippage=abs(fill_price - price),
            broker_fill_id=broker_order_id,
        )

        manager = self._positions.setdefault(order.symbol, PositionManager(order.symbol))
        manager.apply_fill(fill, stop=order.limit_price)
        # Cash: buying reduces cash, selling increases it.
        self._cash += -notional if order.side is Side.BUY else notional
        self._cash -= fee

        # D.5.5: the broker keeps its OWN view of the order. Writing these
        # fields onto the caller's Order would double-count the fill, because
        # OMS.submit applies the very same fills on top of them.
        broker_view = order.model_copy(deep=True)
        broker_view.status = OrderStatus.FILLED
        broker_view.filled_quantity = order.quantity
        broker_view.average_fill_price = fill_price
        broker_view.broker_order_id = broker_order_id
        broker_view.submitted_at = fill.timestamp
        broker_view.updated_at = fill.timestamp
        self._orders[order.order_id] = broker_view
        return BrokerExecution(broker_order_id, OrderStatus.FILLED, fills=[fill])

    async def cancel_order(self, order: Order) -> Order:
        if order.status in (OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED):
            order.status = OrderStatus.CANCELLED
            order.updated_at = utcnow()
        self._orders[order.order_id] = order
        return order

    async def get_order(self, order: Order) -> Order:
        return self._orders.get(order.order_id, order)

    async def close_position(self, position: Position) -> BrokerExecution:
        side = Side.SELL if position.direction.value == "long" else Side.BUY
        order = Order(
            order_id=new_order_id(),
            client_order_id=new_order_id(),
            symbol=position.symbol,
            side=side,
            direction=position.direction,
            quantity=abs(position.quantity),
        )
        return await self.submit_order(order)
