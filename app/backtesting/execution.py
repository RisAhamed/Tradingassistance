"""Simulated broker for backtesting — realistic execution model.

Every assumption comes from BacktestingExecutionConfig. No magic numbers.

The simulated broker NEVER touches a real broker. It produces Fill objects
that flow through the same FillLedger / OMS / PositionManager pipeline as
live trading, so the four-way accounting invariant holds:

    simulator == fill ledger == OMS == position manager

Fee model (matches the D.5.5-R2 Alpaca paper observation):
  - Buy: fee deducted in-kind from the received base-asset quantity.
  - Sell: fee deducted from quote-currency proceeds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.config.models import BacktestingExecutionConfig
from app.domain.enums import Direction, Side
from app.domain.orders import Fill
from app.domain.positions import Position


@dataclass(slots=True)
class SimulatedOrder:
    order_id: str
    side: Side
    direction: Direction
    requested_quantity: float
    requested_price: float
    submitted_at: datetime
    symbol: str = ""
    filled_quantity: float = 0.0
    average_fill_price: float = 0.0
    fee: float = 0.0
    slippage: float = 0.0
    status: str = "open"
    fills: list[Fill] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return max(0.0, self.requested_quantity - self.filled_quantity)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("filled", "rejected", "cancelled")


class SimulatedBroker:
    """Deterministic execution simulator. Never contacts a real broker."""

    def __init__(self, config: BacktestingExecutionConfig) -> None:
        self.config = config
        self.orders: list[SimulatedOrder] = []

    def submit(
        self,
        *,
        order_id: str,
        side: Side,
        direction: Direction,
        quantity: float,
        price: float,
        timestamp: datetime,
        symbol: str = "",
    ) -> SimulatedOrder:
        order = SimulatedOrder(
            order_id=order_id,
            side=side,
            direction=direction,
            requested_quantity=quantity,
            requested_price=price,
            submitted_at=timestamp,
            symbol=symbol,
        )
        self.orders.append(order)
        return order

    def try_fill(
        self,
        order: SimulatedOrder,
        *,
        candle_high: float,
        candle_low: float,
        candle_close: float,
        timestamp: datetime,
        fill_id: str,
    ) -> Fill | None:
        """Attempt to fill an order against one candle's price range.

        Returns a Fill if any quantity was filled, else None. Supports
        partial fills when configured.
        """
        if order.is_terminal or order.remaining <= 0:
            return None

        cfg = self.config
        fee_rate = cfg.fee_percent / 100.0
        spread_rate = cfg.spread_percent / 100.0
        slippage_rate = cfg.slippage_percent / 100.0

        # Determine the execution price for this candle.
        if order.side is Side.BUY:
            base = candle_close * (1.0 + spread_rate / 2.0 + slippage_rate)
        else:
            base = candle_close * (1.0 - spread_rate / 2.0 - slippage_rate)

        # Market orders always fill at the slippage/spread-adjusted price.
        # The candle's range is not used to reject market orders — they fill
        # at the prevailing price regardless of intrabar extremes.

        # Determine fill quantity (partial fills supported).
        fill_qty = order.remaining
        if cfg.partial_fill_enabled:
            max_frac = min(1.0, max(0.0, cfg.partial_fill_max_fraction))
            fill_qty = min(fill_qty, order.requested_quantity * max_frac)
            if fill_qty <= 0:
                return None

        # Apply the fee model.
        if cfg.fee_model == "in_kind" and order.side is Side.BUY:
            fill_qty = fill_qty * (1.0 - fee_rate)

        fill_qty = round(fill_qty, 8)
        if fill_qty <= 0:
            return None

        fee = 0.0
        if cfg.fee_model == "quote" or order.side is Side.SELL:
            fee = fill_qty * base * fee_rate

        slippage = abs(base - candle_close) * fill_qty

        fill = Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            timestamp=timestamp,
            symbol=order.symbol,
            side=order.side,
            quantity=fill_qty,
            price=base,
            fee=round(fee, 8),
        )
        order.fills.append(fill)
        order.filled_quantity += fill_qty
        order.fee += fee
        order.slippage += slippage
        if order.filled_quantity >= order.requested_quantity - 1e-12:
            order.status = "filled"
            order.average_fill_price = sum(
                f.price * f.quantity for f in order.fills
            ) / order.filled_quantity
        else:
            order.status = "partially_filled"
        return fill

    def cancel(self, order: SimulatedOrder) -> None:
        if not order.is_terminal:
            order.status = "cancelled"

    def reset(self) -> None:
        self.orders.clear()
