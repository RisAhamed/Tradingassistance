"""Market-data provider interface.

Providers translate a broker's wire format into normalized domain objects and
never leak provider-specific structures to the rest of the application.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from app.domain.market import Candle, Quote, Trade

# Phase C1: a normalized 1-minute bar is a first-class market update, so the rest
# of the application keeps using the existing domain models (no second model).
MarketUpdate = Quote | Trade | Candle
UpdateHandler = Callable[[MarketUpdate], Awaitable[None] | None]


@dataclass(slots=True)
class ProviderHealth:
    connected: bool = False
    detail: str = ""
    last_message_at: datetime | None = None


class MarketDataProvider(ABC):
    name: str = "base"

    def __init__(self) -> None:
        self._handler: UpdateHandler | None = None

    def set_handler(self, handler: UpdateHandler) -> None:
        self._handler = handler

    async def _dispatch(self, update: MarketUpdate) -> None:
        if self._handler is None:
            return
        result = self._handler(update)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]

    @abstractmethod
    async def connect(self, symbols: list[str]) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    def health(self) -> ProviderHealth: ...
