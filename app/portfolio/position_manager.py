"""Position manager: maintains the authoritative open position per symbol.

Applies fills, computes realized P&L on reductions, and reports whether a
position was opened, changed, reduced, or fully closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.ids import new_position_id
from app.domain.enums import Direction, Side
from app.domain.orders import Fill
from app.domain.positions import Position


@dataclass(slots=True)
class PositionUpdate:
    opened: bool = False
    increased: bool = False
    reduced: bool = False
    closed: bool = False
    flipped: bool = False
    realized_delta: float = 0.0


class PositionManager:
    def __init__(self, symbol: str, *, session_id: str | None = None) -> None:
        self.symbol = symbol
        self.session_id = session_id
        self.position = Position.flat(
            symbol, position_id=new_position_id(), session_id=session_id
        )
        # FILL SAFETY: markers of fills already applied (idempotency).
        self._seen_fills: set[str] = set()

    # -- queries ------------------------------------------------------------
    @property
    def is_flat(self) -> bool:
        return self.position.is_flat

    @property
    def direction(self) -> Direction:
        return self.position.direction

    def mark(self, price: float) -> float:
        self.position.current_price = price
        return self.position.compute_unrealized()

    # -- mutations ----------------------------------------------------------
    def apply_fill(
        self,
        fill: Fill,
        *,
        stop: float | None = None,
        target: float | None = None,
        correlation_id: str | None = None,
    ) -> PositionUpdate:
        update = PositionUpdate()
        position = self.position
        # FILL SAFETY (Phase A #7): invalid fills must never corrupt position
        # state. Non-positive quantities and non-positive prices are rejected
        # (positions are derived from actual fills, so a bad fill is worse
        # than a missed fill — the miss is caught by reconciliation).
        # Duplicate fills (same broker_fill_id / fill_id replay) are ignored
        # idempotently so replays cannot double-count quantity.
        if fill.quantity <= 0 or fill.price <= 0:
            return update
        marker = fill.broker_fill_id or fill.fill_id
        if marker and marker in self._seen_fills:
            return update
        if marker:
            self._seen_fills.add(marker)
        signed_qty = fill.quantity if fill.side is Side.BUY else -fill.quantity

        if position.is_flat:
            self._open(fill, signed_qty, stop, target, correlation_id)
            update.opened = True
            return update

        pos_sign = 1.0 if position.direction is Direction.LONG else -1.0
        if signed_qty * pos_sign > 0:
            self._increase(fill, signed_qty)
            update.increased = True
            return update

        # Opposite side: reduce (and possibly flip).
        closing_qty = min(abs(signed_qty), abs(position.quantity))
        update.realized_delta = self._realize(position, fill, closing_qty)
        remaining = abs(signed_qty) - closing_qty
        update.reduced = True

        if remaining > 0:
            # Flip: close the old position entirely and open the other side.
            self._open(fill, (1.0 if signed_qty > 0 else -1.0) * remaining, stop, target, correlation_id)
            update.flipped = True
        elif abs(position.quantity) <= 1e-12:
            self._flatten(correlation_id)
            update.closed = True
        else:
            position.fees += fill.fee
            position.updated_at = fill.timestamp
        return update

    # -- internals ----------------------------------------------------------
    def _open(
        self,
        fill: Fill,
        signed_qty: float,
        stop: float | None,
        target: float | None,
        correlation_id: str | None,
    ) -> None:
        previous = self.position
        position = Position(
            position_id=new_position_id(),
            symbol=self.symbol,
            direction=Direction.LONG if signed_qty > 0 else Direction.SHORT,
            quantity=abs(signed_qty),
            average_entry=fill.price,
            current_price=fill.price,
            stop=stop,
            target=target,
            # Cumulative realized P&L and fees carry forward across trades.
            realized_pnl=previous.realized_pnl,
            fees=previous.fees + fill.fee,
            opened_at=fill.timestamp,
            updated_at=fill.timestamp,
            correlation_id=correlation_id,
            session_id=self.session_id,
        )
        self.position = position

    def _increase(self, fill: Fill, signed_qty: float) -> None:
        position = self.position
        total = abs(position.quantity) + abs(signed_qty)
        position.average_entry = (
            position.average_entry * abs(position.quantity) + fill.price * abs(signed_qty)
        ) / total
        position.quantity = total
        position.current_price = fill.price
        position.fees += fill.fee
        position.updated_at = fill.timestamp

    @staticmethod
    def _realize(position: Position, fill: Fill, closing_qty: float) -> float:
        sign = 1.0 if position.direction is Direction.LONG else -1.0
        pnl = sign * (fill.price - position.average_entry) * closing_qty
        position.realized_pnl += pnl
        position.quantity = abs(position.quantity) - closing_qty
        position.fees += fill.fee
        position.current_price = fill.price
        position.updated_at = fill.timestamp
        return pnl

    def _flatten(self, correlation_id: str | None) -> None:
        previous = self.position
        self.position = Position(
            position_id=previous.position_id,
            symbol=self.symbol,
            direction=Direction.FLAT,
            quantity=0.0,
            average_entry=0.0,
            current_price=previous.current_price,
            realized_pnl=previous.realized_pnl,
            fees=previous.fees,
            updated_at=previous.updated_at,
            correlation_id=correlation_id or previous.correlation_id,
            session_id=self.session_id,
        )


__all__ = ["PositionManager", "PositionUpdate"]
