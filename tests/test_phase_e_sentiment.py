"""Phase E tests: observation-only sentiment + microstructure layer (part 1)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.domain.features import FeatureSnapshot
from app.events.types import EventType
from app.features.engine import FeatureEngine
from app.sentiment.errors import SentimentError, UnsupportedSymbolError
from app.sentiment.mock import MockSentimentProvider
from app.sentiment.models import SentimentSnapshot
from app.sentiment.providers import none as _none_provider  # noqa: F401  (registers "none")
from app.sentiment.providers.fear_greed import FearGreedProvider
from app.sentiment.registry import build_sentiment_provider, known_sentiment_providers

UTC = timezone.utc


def _snapshot(**overrides) -> SentimentSnapshot:
    base = dict(
        timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        symbol="SYM/QUOTE",
        source="test",
        score=0.25,
        fear_greed=62.0,
        funding_rate=0.0001,
        open_interest_change_24h=0.02,
        news_volume=7,
        raw={"k": "v"},
    )
    base.update(overrides)
    return SentimentSnapshot(**base)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_valid_snapshot_accepted():
    snap = _snapshot()
    assert snap.score == 0.25
    assert snap.fear_greed == 62.0


def test_out_of_range_score_raises_sentiment_error():
    with pytest.raises(SentimentError):
        _snapshot(score=1.5)
    with pytest.raises(SentimentError):
        _snapshot(score=float("nan"))
    with pytest.raises(SentimentError):
        _snapshot(score=float("inf"))


def test_missing_timestamp_raises():
    with pytest.raises(Exception):
        SentimentSnapshot(symbol="X", source="t", score=0.1)  # type: ignore[call-arg]


def test_registry_resolves_without_branching():
    assert set(known_sentiment_providers()) >= {"mock", "fear_greed", "none"}
    assert isinstance(build_sentiment_provider("mock", seed=3), MockSentimentProvider)
    assert isinstance(build_sentiment_provider("fear_greed"), FearGreedProvider)
    with pytest.raises(ValueError, match="unknown sentiment provider"):
        build_sentiment_provider("nope")


def test_empty_symbol_raises_unsupported():
    provider = MockSentimentProvider(seed=1)
    with pytest.raises(UnsupportedSymbolError):
        _run(provider.fetch("  "))


def test_mock_provider_deterministic_given_same_seed():
    first = _run(MockSentimentProvider(seed=11).fetch("SYM/QUOTE"))
    second = _run(MockSentimentProvider(seed=11).fetch("SYM/QUOTE"))
    assert first.model_dump(exclude={"timestamp"}) == second.model_dump(exclude={"timestamp"})
    assert first.score == second.score
    other = _run(MockSentimentProvider(seed=12).fetch("SYM/QUOTE"))
    assert other.score != first.score or other.fear_greed != first.fear_greed
