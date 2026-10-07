"""Sentiment provider registry (same decorator + dict pattern as strategies).

Provider selection goes through :func:`build_sentiment_provider` — never an
``if/elif`` chain on the provider name at the call site.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.sentiment.provider import SentimentProvider

Builder = Callable[..., SentimentProvider]

_BUILDERS: dict[str, Builder] = {}


def register(name: str):
    """Decorator registering a sentiment provider builder under ``name``."""

    def wrap(builder: Builder) -> Builder:
        _BUILDERS[name] = builder
        return builder

    return wrap


def known_sentiment_providers() -> list[str]:
    return sorted(_BUILDERS)


def build_sentiment_provider(name: str, **kwargs: Any) -> SentimentProvider:
    """Instantiate the named sentiment provider or fail clearly."""
    try:
        builder = _BUILDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown sentiment provider '{name}' (known: {known_sentiment_providers()})"
        ) from None
    return builder(**kwargs)
