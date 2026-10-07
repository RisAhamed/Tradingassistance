"""Typed sentiment errors (fail-closed, never fabricated values)."""
from __future__ import annotations

from app.core.errors import TradingAgentError


class SentimentError(TradingAgentError):
    """Raised when a sentiment snapshot is missing, invalid, or unfetchable."""


class UnsupportedSymbolError(SentimentError):
    """Raised when a provider cannot serve the requested symbol."""
