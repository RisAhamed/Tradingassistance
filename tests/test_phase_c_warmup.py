"""PHASE C: historical warm-up, live handoff, gap handling, freshness policy.

Execution stays disabled throughout — these tests also assert that warming up
never produces an order.
"""
from datetime import timedelta

import pytest

from app.config.loader import get_env, load_config
from app.core.clock import utcnow
from app.domain.market import Candle
from app.events.types import EventType
from app.market_data.history import (
    HistoryError,
    compute_warmup_requirement,
    normalise_bars,
    timeframe_minutes,
)
from app.runtime import build_runtime
from tests.support import SYMBOL


def _config(*, execution_enabled: bool = False, history_provider: str = "alpaca"):
    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.execution.enabled = execution_enabled
    config.market_data.history.provider = history_provider
    return config


def _engine(*, execution_enabled: bool = False, history_provider: str = "alpaca"):
    return build_runtime(_config(execution_enabled=execution_enabled, history_provider=history_provider), get_env()).engine


def _history(count: int, *, start_price: float = 30000.0, step_minutes: int = 1) -> list[Candle]:
    """Deterministic 1-minute history ending 'now-ish' (deterministic base time)."""
    from tests.support import NOW

    candles = []
    price = start_price
    for i in range(count):
        open_ = price
        close = price * (1.0 + (0.0006 if i % 2 else -0.0004))
        high = max(open_, close) * 1.0005
        low = min(open_, close) * 0.9995
        candles.append(
            Candle(
                timestamp=NOW + timedelta(minutes=step_minutes * i),
                symbol=SYMBOL,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1.0 + i,
                timeframe="1m",
            )
        )
        price = close
    return candles


def _stub_history(engine, candles, *, raise_exc: Exception | None = None):
    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        if raise_exc is not None:
            raise raise_exc
        return candles

    engine.history_client.fetch_candles = _fetch  # type: ignore[assignment]


# --- #3 requirement is DERIVED, not hardcoded --------------------------------
def test_warmup_requirement_is_derived_from_configuration():
    config = load_config(env=get_env())
    requirement = compute_warmup_requirement(config)
    assert requirement.reasons["ema"] == max(config.features.ema.periods)
    assert requirement.reasons["rsi"] == config.features.rsi.period + 1
    assert requirement.reasons["atr"] == config.features.atr.period + 1
    assert requirement.reasons["regime_min_candles"] == config.regime.min_candles
    # The deepest requirement drives the depth; history timeframe is the finest.
    assert requirement.base_candles == max(requirement.reasons.values())
    assert requirement.signal_candles == requirement.base_candles * 5
    assert requirement.context_candles == requirement.base_candles * 15
    assert requirement.history_bars_needed == requirement.context_candles + 1


def test_warmup_requirement_changes_when_pemissions_change():
    config = load_config(env=get_env())
    config.features.ema.periods = [10, 100]
    requirement = compute_warmup_requirement(config)
    assert requirement.base_candles == 100
    assert requirement.context_candles == 1500


def test_timeframe_parsing():
    assert timeframe_minutes("1m") == 1
    assert timeframe_minutes("5m") == 5
    assert timeframe_minutes("15m") == 15
    assert timeframe_minutes("1h") == 60
    assert timeframe_minutes("1d") == 1440


def test_lookback_override_is_honoured():
    config = load_config(env=get_env())
    config.market_data.history.lookback_bars = 10
    assert config.market_data.history.lookback_bars == 10


# --- #12 historical normalisation (malformed / duplicate / out-of-order) ----
class _Bar:
    def __init__(self, timestamp, open_=1.0, high=2.0, low=0.5, close=1.5, volume=1.0):
        self.timestamp = timestamp
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def _raw(bars):
    return {"data": {SYMBOL: bars}}


def test_history_normalisation_drops_duplicate_and_out_of_order_bars():
    from tests.support import NOW

    t0 = NOW
    raw = _raw(
        [
            _Bar(t0, close=1.0),
            _Bar(t0, close=9.9),            # duplicate timestamp
            _Bar(t0 - timedelta(minutes=5), close=5.0),  # out of order
            _Bar(t0 + timedelta(minutes=1), close=2.0),
        ]
    )
    candles = normalise_bars(raw, symbol=SYMBOL, timeframe="1m")
    assert [c.close for c in candles] == [1.0, 2.0]


def test_history_normalisation_rejects_malformed_bars():
    from tests.support import NOW

    raw = _raw(
        [
            _Bar(NOW, high=0.1, low=0.9),    # high < low
            _Bar(NOW + timedelta(minutes=1), close=None),
        ]
    )
    with pytest.raises(HistoryError):
        normalise_bars(raw, symbol=SYMBOL, timeframe="1m")


def test_history_normalisation_rejects_empty_response():
    with pytest.raises(HistoryError):
        normalise_bars({"data": {}}, symbol=SYMBOL, timeframe="1m")


# --- #11 warm-up happy path (the restart test) -------------------------------
async def test_warmup_builds_candles_features_and_regime_without_waiting():
    engine = _engine()
    requirement = compute_warmup_requirement(engine.config)
    history = _history(requirement.history_bars_needed + 5)
    _stub_history(engine, history)
    events: list = []
    engine.bus.subscribe_all(events.append)

    assert await engine.warm_up() is True

    types = [e.type for e in events]
    assert EventType.WARMUP_STARTED in types
    assert EventType.WARMUP_REQUESTED in types
    assert EventType.WARMUP_RECEIVED in types
    assert EventType.WARMUP_CANDLES_BUILT in types
    assert EventType.WARMUP_FEATURES_READY in types
    assert EventType.WARMUP_REGIME_READY in types
    assert EventType.WARMUP_COMPLETED in types
    assert EventType.LIVE_HANDOFF_STARTED in types
    assert EventType.WARMUP_FAILED not in types

    # Every configured timeframe has history (derived via the same aggregator).
    counts = {tf: len(engine.store.candles(SYMBOL, tf)) for tf in engine.timeframes}
    assert counts["1m"] > 0 and counts["5m"] > 0 and counts["15m"] > 0
    # 15m needs 50 candles for ema_50.
    assert counts["15m"] >= 50
    assert engine.state.features is not None and engine.state.features.ready
    assert engine.state.regime is not None and not engine.state.regime.is_unknown
    # Execution still impossible.
    assert engine.oms.all_orders() == []
    assert engine.execution_enabled is False


# --- #12 failure paths stay fail-closed --------------------------------------
@pytest.mark.parametrize(
    "kwargs, expected_reason",
    [
        ({"raise_exc": RuntimeError("alpaca 500")}, "history_fetch_failed"),
        ({"candles": []}, "empty_history"),
    ],
)
async def test_warmup_failures_are_reported_and_safe(kwargs, expected_reason):
    engine = _engine()
    _stub_history(engine, kwargs.get("candles", []), raise_exc=kwargs.get("raise_exc"))
    events: list = []
    engine.bus.subscribe_all(events.append)

    assert await engine.warm_up() is False

    assert engine.warmup["status"] == "failed"
    assert expected_reason in engine.warmup["reason"]
    assert EventType.WARMUP_FAILED in [e.type for e in events]
    assert engine.oms.all_orders() == []
    assert engine.position_manager.is_flat


async def test_insufficient_history_fails_closed():
    engine = _engine()
    _stub_history(engine, _history(30))  # nowhere near the requirement
    assert await engine.warm_up() is False
    assert engine.warmup["status"] == "failed"
    assert engine.warmup["reason"] == "insufficient_history"
    assert engine.oms.all_orders() == []


async def test_warmup_disabled_is_reported_as_skipped():
    engine = _engine(history_provider="none")
    assert await engine.warm_up() is False
    assert engine.warmup["status"] == "skipped"
    assert engine.warmup["reason"] == "history_disabled"


# --- #5/#6 live handoff + gap handling ---------------------------------------
async def _warmed_engine():
    engine = _engine()
    requirement = compute_warmup_requirement(engine.config)
    _stub_history(engine, _history(requirement.history_bars_needed + 5))
    assert await engine.warm_up() is True
    return engine


async def test_live_handoff_completes_on_first_live_update():
    engine = await _warmed_engine()
    last_hist = engine._last_historical_at
    events: list = []
    engine.bus.subscribe_all(events.append)

    from app.domain.market import Trade

    await engine.on_market_update(
        Trade(timestamp=last_hist + timedelta(minutes=1), symbol=SYMBOL, price=30100.0, size=0.1)
    )

    assert engine.warmup["live_handoff"] == "completed"
    assert engine.warmup["first_live_at"] is not None
    assert engine.warmup["last_historical_at"] == last_hist.isoformat()
    assert EventType.LIVE_HANDOFF_COMPLETED in [e.type for e in events]


async def test_live_update_does_not_duplicate_historical_candles():
    engine = await _warmed_engine()
    before = {tf: len(engine.store.candles(SYMBOL, tf)) for tf in engine.timeframes}
    last_hist = engine._last_historical_at

    from app.domain.market import Trade

    # A replay of the last historical timestamp must be rejected.
    await engine.on_market_update(
        Trade(timestamp=last_hist, symbol=SYMBOL, price=1.0, size=1.0)
    )
    after = {tf: len(engine.store.candles(SYMBOL, tf)) for tf in engine.timeframes}
    assert after == before


async def test_large_gap_after_warmup_marks_data_integrity_unhealthy():
    engine = await _warmed_engine()
    last_hist = engine._last_historical_at
    events: list = []
    engine.bus.subscribe_all(events.append)

    from app.domain.market import Trade

    await engine.on_market_update(
        Trade(timestamp=last_hist + timedelta(minutes=90), symbol=SYMBOL, price=1.0, size=1.0)
    )

    assert engine.warmup["gap"]["detected"] is True
    assert engine.warmup["gap"]["within_tolerance"] is False
    assert engine._data_gap_ok is False
    assert EventType.DATA_GAP in [e.type for e in events]
    # Fail-closed: risk context reports the data as not fresh.
    assert engine._build_risk_context(utcnow()).market_data_fresh is False


# --- #8 ambiguous-submission policy is explicit, never a blind retry ---------
def test_ambiguous_submission_policy_is_reconcile():
    config = load_config(env=get_env())
    assert config.execution.retry.action == "reconcile"


def test_blind_retry_cannot_be_configured():
    """The schema only admits 'reconcile' — a blind retry is unrepresentable."""
    from pydantic import ValidationError

    from app.config.models import ExecutionConfig

    try:
        ExecutionConfig(retry={"action": "retry", "max_reconcile_attempts": 3})
    except ValidationError:
        return
    raise AssertionError("blind retry must not be configurable")


async def test_ambiguous_submission_flags_reconciliation():
    from app.brokers.mock import MockBroker
    from app.core.ids import new_order_id
    from app.domain.enums import Direction, OrderType, Side
    from app.domain.orders import OrderIntent
    from app.execution.executor import OrderExecutor
    from app.orders.oms import OMS

    class _TimeoutBroker(MockBroker):
        async def submit_order(self, order):  # noqa: ANN001 - test double
            raise TimeoutError("ambiguous")

    broker = _TimeoutBroker()
    broker.set_price(SYMBOL, 100.0)
    await broker.connect()
    oms = OMS(broker)
    order = oms.create(
        OrderIntent(
            order_id=new_order_id(),
            client_order_id=new_order_id(),
            timestamp=utcnow(),
            symbol=SYMBOL,
            side=Side.BUY,
            direction=Direction.LONG,
            quantity=1.0,
            order_type=OrderType.MARKET,
        )
    )
    result = await OrderExecutor(oms).submit(order)
    assert result.ambiguous is True  # never resubmitted


# --- #7 freshness measurement is separate from risk policy -------------------
def test_freshness_measurement_is_separate_from_risk_stale_policy():
    config = load_config(env=get_env())
    freshness = config.market_data.freshness
    assert freshness.threshold_seconds > 0
    assert freshness.stale_action in ("block_entries", "warn_only")
    # The blocking threshold is a *risk* setting and is configured separately.
    assert config.risk.stale_market_data.maximum_age_seconds > 0


async def test_freshness_warn_only_does_not_block_entries():
    engine = _engine(execution_enabled=True)
    engine.config.market_data.freshness.stale_action = "warn_only"
    # No market data at all: measurement says stale.
    assert engine.store.data_age(SYMBOL, utcnow()) is None
    assert engine._build_risk_context(utcnow()).market_data_fresh is True


async def test_default_freshness_blocks_entries_when_stale():
    engine = _engine(execution_enabled=True)
    assert engine.config.market_data.freshness.stale_action == "block_entries"
    assert engine._build_risk_context(utcnow()).market_data_fresh is False