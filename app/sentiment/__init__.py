"""Observation-only sentiment + microstructure snapshots (Phase E).

This package enriches :class:`app.domain.features.FeatureSnapshot` with a
strictly informational layer. It NEVER influences a trading decision in this
phase: no gating, no confidence adjustment, no strategy selection.

Symbol-agnostic contract:
- No module, class, or test in this package references a specific instrument,
  venue, or currency. ``fetch(symbol)`` takes the symbol as a parameter.
- If a provider cannot serve a symbol, it raises
  :class:`UnsupportedSymbolError` — it never fabricates a value.
"""
from __future__ import annotations

from app.sentiment.errors import SentimentError, UnsupportedSymbolError
from app.sentiment.models import SentimentSnapshot
from app.sentiment.provider import ProviderHealth, SentimentProvider
from app.sentiment.registry import (
    build_sentiment_provider,
    known_sentiment_providers,
    register,
)

__all__ = [
    "ProviderHealth",
    "SentimentError",
    "SentimentProvider",
    "SentimentSnapshot",
    "UnsupportedSymbolError",
    "build_sentiment_provider",
    "known_sentiment_providers",
    "register",
]
