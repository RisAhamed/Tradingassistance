"""Sentiment provider interface (symbol-agnostic, observation-only)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from app.sentiment.models import SentimentSnapshot


@dataclass(slots=True)
class ProviderHealth:
    connected: bool = False
    detail: str = ""
    last_message_at: datetime | None = None


class SentimentProvider(ABC):
    """Abstract sentiment source. Implementations are symbol-agnostic."""

    name: str = "base"

    @abstractmethod
    async def fetch(self, symbol: str) -> SentimentSnapshot:
        """Fetch the latest normalized snapshot for ``symbol``."""

    @abstractmethod
    def health(self) -> ProviderHealth:
        """Report provider liveness (never raises)."""
