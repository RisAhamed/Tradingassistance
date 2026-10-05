"""Phase D — FreshnessPolicy unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta
import pytest

from app.config.models import DataFreshnessConfig
from app.decision.freshness import FreshnessPolicy, FreshnessResult


@pytest.fixture()
def policy() -> FreshnessPolicy:
    return FreshnessPolicy(DataFreshnessConfig())


def _past(seconds: float) -> datetime:
    return datetime.utcnow() - timedelta(seconds=seconds)


def _future(seconds: float) -> datetime:
    return datetime.utcnow() + timedelta(seconds=seconds)


def test_fresh_bar_healthy_cadence(policy: FreshnessPolicy) -> None:
    r = policy.evaluate(source="bar", last_received=_past(30), now=datetime.utcnow(), expected_interval_seconds=60.0)
    assert not r.is_stale
    assert r.reason == "fresh"


def test_stale_bar_beyond_threshold(policy: FreshnessPolicy) -> None:
    r = policy.evaluate(source="bar", last_received=_past(200), now=datetime.utcnow(), expected_interval_seconds=60.0)
    assert r.is_stale


def test_no_data_is_stale(policy: FreshnessPolicy) -> None:
    r = policy.evaluate(source="bar", last_received=None, now=datetime.utcnow())
    assert r.is_stale
    assert r.reason == "no_data"


def test_quote_threshold_independent(policy: FreshnessPolicy) -> None:
    r = policy.evaluate(source="quote", last_received=_past(60), now=datetime.utcnow())
    assert not r.is_stale  # quote max age = 90; 60 < 90


def test_trade_sparse_over_threshold(policy: FreshnessPolicy) -> None:
    r = policy.evaluate(source="trade", last_received=_past(200), now=datetime.utcnow())
    assert r.is_stale  # trade max age = 90; 200 > 90


def test_future_timestamp_flagged(policy: FreshnessPolicy) -> None:
    r = policy.evaluate(source="bar", last_received=_future(120), now=datetime.utcnow(), expected_interval_seconds=60.0)
    assert r.clock_skew_seconds > 0


def test_cadence_floor_prevents_false_stale(policy: FreshnessPolicy) -> None:
    # 60s bar interval + 30s grace = 90s effective; 80s age is fresh.
    r = policy.evaluate(source="bar", last_received=_past(80), now=datetime.utcnow(), expected_interval_seconds=60.0)
    assert not r.is_stale


def test_static_mode_ignores_cadence(policy: FreshnessPolicy) -> None:
    cfg = DataFreshnessConfig(mode="static", bar=policy.config.bar)
    p = FreshnessPolicy(cfg)
    r = p.evaluate(source="bar", last_received=_past(80), now=datetime.utcnow(), expected_interval_seconds=60.0)
    assert r.effective_threshold_seconds == 90.0  # unchanged
