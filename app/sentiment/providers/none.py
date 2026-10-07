"""Disabled sentiment provider (``provider: none`` / ``enabled: false`` path).

Always raises :class:`SentimentError` on fetch: the engine treats the layer
as unavailable and keeps running. Registered so provider selection never
needs an ``if/elif`` chain.
"""
from __future__ import annotations

from app.sentiment.errors import SentimentError
from app.sentiment.models import SentimentSnapshot
from app.sentiment.provider import ProviderHealth, SentimentProvider
from app.sentiment.registry import register


@register("none")
class NoneSentimentProvider(SentimentProvider):
    name = "none"

    def __init__(self, **_: object) -> None:
        pass

    async def fetch(self, symbol: str) -> SentimentSnapshot:  # noqa: ARG002
        raise SentimentError("sentiment provider 'none' is disabled")

    def health(self) -> ProviderHealth:
        return ProviderHealth(connected=False, detail="sentiment disabled")
