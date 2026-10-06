"""Broker abstraction.

The rest of the application depends on :class:`BrokerAdapter`, never on a
specific broker SDK. This is what keeps the trading engine broker-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.domain.enums import OrderStatus
from app.domain.orders import Fill, Order
from app.domain.positions import Position
from app.domain.symbols import canonical_symbol


@dataclass(slots=True)
class AccountInfo:
    equity: float = 0.0
    cash: float = 0.0
    buying_power: float = 0.0
    currency: str = "USD"


@dataclass(slots=True)
class BrokerExecution:
    """Result of a broker order submission."""

    broker_order_id: str
    status: OrderStatus
    fills: list[Fill] = field(default_factory=list)
    reject_reason: str | None = None

    @property
    def rejected(self) -> bool:
        return self.status is OrderStatus.REJECTED or (
            self.status is OrderStatus.NEW and self.reject_reason is not None
        )


@dataclass(slots=True)
class BrokerHealth:
    connected: bool = False
    detail: str = ""


class BrokerAdapter(ABC):
    """Common interface implemented by every broker (paper or simulated)."""

    name: str = "base"
    paper: bool = True

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def get_account(self) -> AccountInfo: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_orders(self, *, status: str | None = None) -> list[Order]: ...

    @abstractmethod
    async def submit_order(self, order: Order) -> BrokerExecution: ...

    @abstractmethod
    async def cancel_order(self, order: Order) -> Order: ...

    @abstractmethod
    async def get_order(self, order: Order) -> Order: ...

    @abstractmethod
    async def close_position(self, position: Position) -> BrokerExecution: ...

    @abstractmethod
    def health(self) -> BrokerHealth: ...

    async def reconcile(self, internal: list[Position]) -> list[str]:
        """Compare broker positions against internal state.

        Returns a list of human-readable discrepancies (empty when consistent).
        """
        discrepancies: list[str] = []
        try:
            broker_positions = {canonical_symbol(p.symbol): p for p in await self.get_positions() if not p.is_flat}
        except Exception as exc:  # pragma: no cover - defensive
            return [f"broker_position_query_failed: {exc}"]
        internal_positions = {canonical_symbol(p.symbol): p for p in internal if not p.is_flat}
        for symbol, position in internal_positions.items():
            broker_position = broker_positions.get(symbol)
            if broker_position is None:
                discrepancies.append(f"internal position {symbol} not present at broker")
            elif abs(broker_position.quantity - position.quantity) > 1e-9:
                discrepancies.append(
                    f"quantity mismatch {symbol}: internal={position.quantity} broker={broker_position.quantity}"
                )
        for symbol in broker_positions:
            if symbol not in internal_positions:
                discrepancies.append(f"broker position {symbol} not tracked internally")
        return discrepancies

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "paper": self.paper, **vars(self.health())}
