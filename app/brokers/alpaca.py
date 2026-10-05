"""Alpaca PAPER broker adapter.

Only the paper endpoint is ever used. The client is created with ``paper=True``
and credentials are read from the environment layer (never logged).
"""
from __future__ import annotations

import asyncio
import logging

from app.brokers.base import AccountInfo, BrokerAdapter, BrokerExecution, BrokerHealth
from app.core.clock import utcnow
from app.core.ids import new_fill_id
from app.domain.enums import Direction, OrderStatus, OrderType, Side
from app.domain.orders import Fill, Order
from app.domain.positions import Position

logger = logging.getLogger("app.brokers.alpaca")

_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.ACKNOWLEDGED,
    "pending_new": OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "pending_cancel": OrderStatus.CANCEL_REQUESTED,
    "expired": OrderStatus.EXPIRED,
    "replaced": OrderStatus.EXPIRED,
    "done_for_day": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
    "suspended": OrderStatus.REJECTED,
}


class AlpacaPaperBroker(BrokerAdapter):
    name = "alpaca"
    paper = True

    def __init__(self, api_key: str, api_secret: str, *, feed: str = "crypto") -> None:
        from alpaca.trading.client import TradingClient  # lazy import

        if not api_key or not api_secret:
            raise ValueError("Alpaca paper credentials are required")
        self._client = TradingClient(api_key, api_secret, paper=True)
        self.feed = feed
        self._connected = False

    async def connect(self) -> None:
        await asyncio.to_thread(self._client.get_account)
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def health(self) -> BrokerHealth:
        return BrokerHealth(connected=self._connected, detail="alpaca paper endpoint")

    async def get_account(self) -> AccountInfo:
        account = await asyncio.to_thread(self._client.get_account)
        return AccountInfo(
            equity=float(account.equity or 0.0),
            cash=float(account.cash or 0.0),
            buying_power=float(account.buying_power or 0.0),
            currency=getattr(account, "currency", "USD") or "USD",
        )

    async def get_positions(self) -> list[Position]:
        raw = await asyncio.to_thread(self._client.get_all_positions)
        positions: list[Position] = []
        for item in raw:
            quantity = abs(float(item.qty or 0.0))
            if quantity == 0:
                continue
            side = str(getattr(item, "side", "long")).split(".")[-1]
            positions.append(
                Position(
                    position_id=f"alpaca::{item.symbol}",
                    symbol=item.symbol,
                    direction=Direction.LONG if side == "long" else Direction.SHORT,
                    quantity=quantity,
                    average_entry=float(item.avg_entry_price or 0.0),
                    current_price=float(item.current_price or 0.0),
                    unrealized_pnl=float(item.unrealized_pl or 0.0),
                )
            )
        return positions

    # -- order flow ---------------------------------------------------------
    def _build_request(self, order: Order):
        from alpaca.trading.enums import OrderSide, TimeInForce as AlpacaTif
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        side = OrderSide.BUY if order.side is Side.BUY else OrderSide.SELL
        tif = AlpacaTif.GTC
        if "/" in order.symbol:
            # D.4: Alpaca crypto market orders must use IOC; GTC is rejected.
            tif = AlpacaTif.IOC
        if order.order_type is OrderType.LIMIT and order.limit_price:
            return LimitOrderRequest(
                symbol=order.symbol,
                qty=order.quantity,
                side=side,
                time_in_force=tif,
                limit_price=order.limit_price,
                client_order_id=order.client_order_id,
            )
        return MarketOrderRequest(
            symbol=order.symbol,
            qty=order.quantity,
            side=side,
            time_in_force=tif,
            client_order_id=order.client_order_id,
        )

    async def submit_order(self, order: Order) -> BrokerExecution:
        from alpaca.common.exceptions import APIError

        try:
            request = self._build_request(order)
            raw = await asyncio.to_thread(self._client.submit_order, order_data=request)
        except APIError as exc:
            logger.error(
                "alpaca submit rejected",
                extra={
                    "structured": {
                        "event": "ORDER_REJECTED",
                        "component": "brokers.alpaca",
                        "order_id": order.order_id,
                        "error": _clean(str(exc)),
                    }
                },
            )
            return BrokerExecution(order.order_id, OrderStatus.REJECTED, reject_reason=_clean(str(exc)))
        order.broker_order_id = str(getattr(raw, "id", ""))
        return self._to_execution(order, raw)

    async def cancel_order(self, order: Order) -> Order:
        if not order.broker_order_id:
            order.status = OrderStatus.CANCELLED
            return order
        try:
            await asyncio.to_thread(self._client.cancel_order_by_id, order.broker_order_id)
            order.status = OrderStatus.CANCELLED
        except Exception as exc:  # noqa: BLE001 - surfaced through order state
            logger.warning(
                "alpaca cancel failed",
                extra={
                    "structured": {
                        "event": "ORDER_CANCEL_FAILED",
                        "component": "brokers.alpaca",
                        "order_id": order.order_id,
                        "error": _clean(str(exc)),
                    }
                },
            )
        order.updated_at = utcnow()
        return order

    async def get_order(self, order: Order) -> Order:
        if not order.broker_order_id:
            return order
        raw = await asyncio.to_thread(self._client.get_order_by_id, order.broker_order_id)
        self._to_execution(order, raw)
        return order

    async def get_orders(self, *, status: str | None = None) -> list[Order]:
        # D.3: list paper orders through the same paper TradingClient.
        from alpaca.trading.requests import GetOrdersRequest

        try:
            raw_orders = await asyncio.to_thread(
                self._client.get_orders,
                filter=GetOrdersRequest(status=(status or "all").lower()),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("alpaca get_orders failed", extra={"structured": {"event": "ORDER_QUERY_FAILED", "component": "broker", "error": str(exc)[:200]}})
            return []
        out: list[Order] = []
        for raw in raw_orders or []:
            order = Order(
                order_id=str(getattr(raw, "client_order_id", "") or str(getattr(raw, "id", ""))),
                client_order_id=str(getattr(raw, "client_order_id", "") or ""),
                symbol=str(getattr(raw, "symbol", SYMBOL_FALLBACK)),
                side=Side.BUY if str(getattr(raw, "side", "buy")).split(".")[-1].lower() == "buy" else Side.SELL,
                direction=Direction.LONG,
                quantity=float(getattr(raw, "qty", 0.0) or 0.0),
                broker_order_id=str(getattr(raw, "id", "")),
            )
            execution = self._to_execution(order, raw)
            order.status = execution.status
            order.filled_quantity = sum(f.quantity for f in execution.fills)
            out.append(order)
        return out

    async def close_position(self, position: Position) -> BrokerExecution:
        try:
            raw = await asyncio.to_thread(self._client.close_position, position.symbol)
        except Exception as exc:  # noqa: BLE001
            return BrokerExecution("", OrderStatus.REJECTED, reject_reason=_clean(str(exc)))
        synthetic = Order(
            order_id="flatten",
            client_order_id="flatten",
            symbol=position.symbol,
            side=Side.SELL if position.direction is Direction.LONG else Side.BUY,
            direction=position.direction,
            quantity=abs(position.quantity),
        )
        return self._to_execution(synthetic, raw)

    def _to_execution(self, order: Order, raw) -> BrokerExecution:
        status_value = str(getattr(raw, "status", "new")).split(".")[-1].lower()
        status = _STATUS_MAP.get(status_value, OrderStatus.SUBMITTED)
        filled_qty = float(getattr(raw, "filled_qty", 0.0) or 0.0)
        avg_price = getattr(raw, "filled_avg_price", None)
        fills: list[Fill] = []
        if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and filled_qty > 0 and avg_price:
            fills.append(
                Fill(
                    fill_id=new_fill_id(),
                    order_id=order.order_id,
                    timestamp=utcnow(),
                    symbol=order.symbol,
                    side=order.side,
                    quantity=filled_qty,
                    price=float(avg_price),
                    broker_fill_id=str(getattr(raw, "id", "")),
                )
            )
        reject_reason = f"alpaca_status={status_value}" if status is OrderStatus.REJECTED else None
        # D.4: keep the caller's Order object authoritative-consistent so
        # get_order/submit_order cannot leave the local model at NEW forever.
        order.status = status
        if fills:
            order.filled_quantity = sum(f.quantity for f in fills)
            order.average_fill_price = fills[-1].price
        if order.submitted_at is None:
            order.submitted_at = utcnow()
        order.updated_at = utcnow()
        return BrokerExecution(str(getattr(raw, "id", "")), status, fills=fills, reject_reason=reject_reason)


SYMBOL_FALLBACK = "UNKNOWN"


def _clean(message: str) -> str:
    return message.replace("\n", " ").strip()[:300]
