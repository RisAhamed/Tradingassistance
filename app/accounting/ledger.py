"""Authoritative cumulative fill ledger (D.5.4).

Single source of truth for applied fills. OMS and PositionManager surface
this ledger's canonical net position so the two cannot silently diverge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.domain.enums import Direction, Side
from app.domain.orders import Fill


@dataclass
class LedgerEntry:
    key: str
    symbol: str
    side: Side
    quantity: float
    price: float
    timestamp: datetime
    broker_order_id: str | None = None


@dataclass
class FillLedger:
    entries: list[LedgerEntry] = field(default_factory=list)
    duplicates_ignored: int = 0
    _keys: set[str] = field(default_factory=set)

    def add_fill(self, fill: Fill) -> float:
        """Add a fill. Returns applied signed quantity (+/-), or 0 if duplicate."""
        key = fill.broker_fill_id or fill.fill_id
        if key in self._keys:
            self.duplicates_ignored += 1
            return 0.0
        self._keys.add(key)
        self.entries.append(LedgerEntry(
            key=key, symbol=fill.symbol, side=fill.side, quantity=fill.quantity,
            price=fill.price, timestamp=fill.timestamp, broker_order_id=fill.broker_fill_id,
        ))
        return fill.quantity if fill.side is Side.BUY else -fill.quantity

    def net_quantity(self, symbol: str) -> float:
        net = 0.0
        for e in self.entries:
            if e.symbol != symbol:
                continue
            net += e.quantity if e.side is Side.BUY else -e.quantity
        return net
