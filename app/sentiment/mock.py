"""Deterministic mock sentiment provider (tests + offline demos).

Seeded and reproducible: the same ``(seed, symbol)`` pair always yields the
same normalized shape. Emits the same :class:`SentimentSnapshot` contract as
every real provider.
"""
from __future__ import annotations

import hashlib
import random

from app.core.clock import utcnow
from app.sentiment.errors import UnsupportedSymbolError
from app.sentiment.models import SentimentSnapshot
from app.sentiment.provider import ProviderHealth, SentimentProvider
from app.sentiment.registry import register


@register("mock")
class MockSentimentProvider(SentimentProvider):
    name = "mock"

    def __init__(self, *, seed: int = 7, score: float | None = None) -> None:
        self._seed = int(seed)
        self._fixed_score = score
        self._fetches = 0
        self._last_at = None

    def _rng_for(self, symbol: str) -> random.Random:
        digest = hashlib.sha256(f"{self._seed}:{symbol}".encode("utf-8")).hexdigest()
        return random.Random(int(digest[:16], 16))

    async def fetch(self, symbol: str) -> SentimentSnapshot:
        if not symbol or not symbol.strip():
            raise UnsupportedSymbolError("mock sentiment provider requires a non-empty symbol")
        rng = self._rng_for(symbol.strip())
        score = self._fixed_score if self._fixed_score is not None else round(rng.uniform(-0.6, 0.6), 4)
        fear_greed = round(50.0 + score * 40.0 + rng.uniform(-2.0, 2.0), 2)
        fear_greed = max(0.0, min(100.0, fear_greed))
        now = utcnow()
        self._fetches += 1
        self._last_at = now
        return SentimentSnapshot(
            timestamp=now,
            symbol=symbol.strip(),
            source="mock",
            score=score,
            fear_greed=fear_greed,
            funding_rate=round(rng.uniform(-0.0005, 0.0005), 7),
            open_interest_change_24h=round(rng.uniform(-0.05, 0.05), 4),
            news_volume=rng.randint(0, 25),
            raw={"seed": self._seed, "fetch_index": self._fetches},
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(connected=True, detail="mock sentiment", last_message_at=self._last_at)
