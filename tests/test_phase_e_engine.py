"""Phase E tests part 2: staleness, ready-immunity, failure isolation."""
from __future__ import annotations

from datetime import datetime, timezone

from app.config.loader import get_env, load_config
from app.core.clock import utcnow
from app.domain.market import Candle
from app.events.types import EventType
from app.features.engine import FeatureEngine
from app.runtime import build_runtime
from app.sentiment.errors import SentimentError
from app.sentiment.provider import ProviderHealth, SentimentProvider
from tests import support as support_mod
from tests.test_phase_e_sentiment import _run, _snapshot

UTC = timezone.utc


def _engine_config(**overrides):
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.market_data.max_future_skew_seconds = 86400 * 30
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.sentiment.enabled = True
    config.sentiment.provider = "mock"
    config.sentiment.refresh_interval_seconds = 300.0
    config.sentiment.maximum_age_seconds = 900.0
    for key, value in overrides.items():
        setattr(config.sentiment, key, value)
    return config, env


def _candles():
    return [
        Candle(
            timestamp=datetime(2026, 1, 1, 11, 59, tzinfo=UTC),
            symbol=support_mod.SYMBOL,
            timeframe="5m",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
        )
        for _ in range(60)
    ]


def test_stale_snapshot_flagged_and_excluded_from_numerics():
    from app.config.models import FeaturesConfig

    engine = FeatureEngine(FeaturesConfig())
    now = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    stale = _snapshot(timestamp=datetime(2026, 1, 1, 11, 0, tzinfo=UTC))
    feats = engine.compute(
        _candles(), timeframe="5m", now=now, sentiment=stale, sentiment_maximum_age_seconds=900.0
    )
    assert feats.sentiment_stale is True
    assert feats.sentiment_score is None
    assert "sentiment_score" not in feats.numeric_values()

    fresh = _snapshot(timestamp=datetime(2026, 1, 1, 12, 29, tzinfo=UTC))
    feats_fresh = engine.compute(
        _candles(), timeframe="5m", now=now, sentiment=fresh, sentiment_maximum_age_seconds=900.0
    )
    assert feats_fresh.sentiment_stale is False
    assert feats_fresh.numeric_values()["sentiment_score"] == fresh.score


def test_feature_snapshot_ready_unaffected_by_missing_sentiment():
    feats = support_mod.make_features()
    assert feats.ready is True
    assert feats.sentiment_score is None
    assert "sentiment_score" not in feats.numeric_values()


async def test_provider_failure_emits_unavailable_and_produces_zero_orders():
    class _Failing(SentimentProvider):
        name = "failing"

        async def fetch(self, symbol: str):
            raise SentimentError("boom")

        def health(self) -> ProviderHealth:
            return ProviderHealth(connected=False, detail="failing")

    config, env = _engine_config()
    runtime = build_runtime(config, env)
    engine = runtime.engine
    engine.sentiment_provider = _Failing()
    seen: list = []
    engine.bus.subscribe_all(lambda event: seen.append(event))
    await engine._poll_sentiment_once()
    await engine.tick(utcnow())
    assert EventType.SENTIMENT_UNAVAILABLE in {event.type for event in seen}
    assert engine._sentiment is None
    assert engine.oms.all_orders() == []
    assert config.execution.enabled is False


async def test_engine_poll_caches_and_emits_updated():
    config, env = _engine_config()
    runtime = build_runtime(config, env)
    engine = runtime.engine
    seen: list = []
    engine.bus.subscribe_all(lambda event: seen.append(event))
    await engine._poll_sentiment_once()
    assert engine._sentiment is not None
    assert EventType.SENTIMENT_UPDATED in {event.type for event in seen}
    payload = engine.sentiment_payload()
    assert payload is not None
    assert payload["snapshot"]["score"] == engine._sentiment.score
    assert payload["stale"] is False
