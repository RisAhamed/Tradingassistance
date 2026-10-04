"""PHASE C1: canonical Alpaca 1-minute bar stream, coverage, gaps and recovery."""
from datetime import timedelta

from app.config.loader import get_env, load_config
from app.config.models import CoveragePolicyConfig
from app.core.clock import utcnow
from app.domain.market import Candle
from app.events.types import EventType
from app.market_data.history import analyse_coverage
from app.runtime import build_runtime
from tests.support import SYMBOL


def _config(*, execution_enabled: bool = False, canonical: bool = True):
    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.execution.enabled = execution_enabled
    config.market_data.bars.canonical = canonical
    return config


def _engine(**kwargs):
    return build_runtime(_config(**kwargs), get_env()).engine


def _bar(minutes: int, *, base: float = 30000.0, symbol: str = SYMBOL, tf: str = "1m") -> Candle:
    """A well-formed base bar `minutes` after the engine's canonical epoch."""
    return Candle(
        timestamp=utcnow().replace(second=0, microsecond=0) + timedelta(minutes=minutes),
        symbol=symbol,
        open=base,
        high=base * 1.001,
        low=base * 0.999,
        close=base * 1.0005,
        volume=1.0,
        timeframe=tf,
    )


async def _watch(engine):
    events: list = []
    engine.bus.subscribe_all(events.append)
    return events


# --- #1-#6 live bar stream: valid, malformed, duplicate, ooo, gap ------------
async def test_valid_bar_is_accepted_and_builds_candles():
    engine = _engine()
    events = await _watch(engine)
    for i in range(1, 8):
        await engine.on_market_update(_bar(i))
    assert EventType.BAR_RECEIVED not in [e.type for e in events]  # high-frequency, not logged
    assert len(engine.store.candles(SYMBOL, "1m")) >= 1
    assert engine._last_bar_at is not None


async def test_malformed_bar_is_rejected():
    engine = _engine()
    events = await _watch(engine)
    bad = Candle(
        timestamp=utcnow().replace(second=0, microsecond=0) + timedelta(minutes=1),
        symbol=SYMBOL, open=1.0, high=2.0, low=0.5, close=1.5, volume=-1.0, timeframe="1m",
    )
    await engine.on_market_update(bad)
    assert EventType.BAR_REJECTED in [e.type for e in events]
    assert engine._last_bar_at is None


async def test_duplicate_bar_is_rejected():
    engine = _engine()
    events = await _watch(engine)
    bar = _bar(1)
    await engine.on_market_update(bar)
    await engine.on_market_update(bar)
    assert EventType.BAR_DUPLICATE in [e.type for e in events]
    assert len(engine.store.candles(SYMBOL, "1m")) == 1


async def test_out_of_order_bar_is_rejected():
    engine = _engine()
    events = await _watch(engine)
    await engine.on_market_update(_bar(5))
    await engine.on_market_update(_bar(2))
    assert EventType.BAR_OUT_OF_ORDER in [e.type for e in events]
    assert len(engine.store.candles(SYMBOL, "1m")) == 1


async def test_normal_one_minute_progression_is_not_a_gap():
    engine = _engine()
    events = await _watch(engine)
    for i in range(1, 6):
        await engine.on_market_update(_bar(i))
    assert EventType.BAR_GAP_DETECTED not in [e.type for e in events]
    assert engine._data_gap_ok is True


async def test_small_gap_is_detected_but_tolerated():
    engine = _engine()
    engine.config.market_data.bars.max_gap_candles = 3
    engine.config.market_data.bars.resync_enabled = False
    events = await _watch(engine)
    await engine.on_market_update(_bar(1))
    await engine.on_market_update(_bar(3))  # one missing bar
    assert EventType.BAR_GAP_DETECTED in [e.type for e in events]
    assert EventType.DATA_GAP not in [e.type for e in events]
    assert engine._data_gap_ok is True


async def test_large_gap_blocks_and_triggers_resync():
    engine = _engine()
    engine.config.market_data.bars.max_gap_candles = 2

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        raise RuntimeError("history unavailable")

    engine.history_client.fetch_candles = _fetch
    events = await _watch(engine)
    await engine.on_market_update(_bar(1))
    await engine.on_market_update(_bar(30))  # far beyond tolerance

    types = [e.type for e in events]
    assert EventType.BAR_GAP_DETECTED in types
    assert EventType.DATA_GAP in types
    assert EventType.RECOVERY_STARTED in types
    assert engine._data_gap_ok is False
    assert engine.recovery["state"] == "failed"
    # Fail-closed: still no orders.
    assert engine.oms.all_orders() == []


# --- #7-#8 historical coverage ----------------------------------------------
def _candles_at(minutes: list[int]) -> list[Candle]:
    return [_bar(m, base=30000.0 + m) for m in minutes]


def test_coverage_passes_when_complete():
    report = analyse_coverage(
        _candles_at(list(range(10))),
        requested=10,
        bar_minutes=1,
        policy=CoveragePolicyConfig(minimum_percent=90.0, maximum_gap_candles=5),
    )
    assert report.status == "PASS"
    assert report.coverage_percent == 100.0
    assert report.gap_count == 0


def test_coverage_fails_and_reports_gaps():
    report = analyse_coverage(
        _candles_at([0, 1, 2, 3, 20, 21]),
        requested=30,
        bar_minutes=1,
        policy=CoveragePolicyConfig(minimum_percent=90.0, maximum_gap_candles=5, on_insufficient="fail"),
    )
    assert report.status == "FAIL"
    assert report.coverage_percent < 90.0
    assert report.gap_count == 1
    assert report.largest_gap == 16
    assert report.missing_intervals == 16
    assert report.reasons


def test_coverage_can_warn_instead_of_failing():
    report = analyse_coverage(
        _candles_at([0, 1, 2]),
        requested=100,
        bar_minutes=1,
        policy=CoveragePolicyConfig(minimum_percent=90.0, on_insufficient="warn"),
    )
    assert report.status == "WARNING"


async def test_insufficient_coverage_fails_warmup_closed():
    engine = _engine()
    engine.config.market_data.history.provider = "alpaca"
    engine.config.market_data.history.coverage = CoveragePolicyConfig(
        minimum_percent=90.0, maximum_gap_candles=5, on_insufficient="fail"
    )
    events = await _watch(engine)

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        return _candles_at(list(range(30)))

    engine.history_client.fetch_candles = _fetch
    assert await engine.warm_up() is False
    types = [e.type for e in events]
    assert EventType.HISTORICAL_COVERAGE_CHECKED in types
    assert EventType.WARMUP_FAILED in types
    assert engine.warmup["reason"].startswith("insufficient_coverage")
    assert engine.oms.all_orders() == []


# --- #9-#10 historical/live continuity ---------------------------------------
async def _warmed_engine(candles=None):
    engine = _engine()
    engine.config.market_data.history.provider = "alpaca"
    engine.config.market_data.history.coverage = CoveragePolicyConfig(
        minimum_percent=50.0, maximum_gap_candles=5, on_insufficient="warn"
    )
    requirement = __import__(
        "app.market_data.history", fromlist=["compute_warmup_requirement"]
    ).compute_warmup_requirement(engine.config)
    data = candles or _candles_at(list(range(requirement.history_bars_needed + 2)))

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        return data

    engine.history_client.fetch_candles = _fetch
    assert await engine.warm_up() is True
    return engine


async def test_live_bar_overlapping_history_is_rejected():
    engine = await _warmed_engine()
    last_hist = engine._last_historical_at
    events = await _watch(engine)
    await engine.on_market_update(_bar(0, symbol=SYMBOL))  # placeholder, replaced below
    await engine.on_market_update(
        Candle(
            timestamp=last_hist,  # exactly the last historical bar
            symbol=SYMBOL, open=1, high=2, low=0.5, close=1.5, volume=1, timeframe="1m",
        )
    )
    assert EventType.BAR_DUPLICATE in [e.type for e in events]


async def test_first_live_bar_after_history_is_accepted():
    engine = await _warmed_engine()
    last_hist = engine._last_historical_at
    await engine.on_market_update(
        Candle(
            timestamp=last_hist + timedelta(minutes=1),
            symbol=SYMBOL, open=1, high=2, low=0.5, close=1.5, volume=1, timeframe="1m",
        )
    )
    assert engine._last_bar_at == last_hist + timedelta(minutes=1)


# --- #11-#14 recovery, rebuilds, readiness -----------------------------------
async def test_recovery_rebuilds_and_returns_to_ready():
    engine = await _warmed_engine()
    engine.config.market_data.bars.max_gap_candles = 2
    repaired = _candles_at(range(-20, 3))

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        return repaired

    engine.history_client.fetch_candles = _fetch
    events = await _watch(engine)
    ok = await engine.resync(reason="test_gap")

    types = [e.type for e in events]
    assert ok is True
    assert EventType.RECOVERY_STARTED in types
    assert EventType.RECOVERY_HISTORICAL_FETCH in types
    assert EventType.RECOVERY_CANDLE_REBUILD in types
    assert EventType.FEATURE_REBUILD_STARTED in types
    assert EventType.FEATURE_REBUILD_COMPLETED in types
    assert EventType.REGIME_REBUILD_STARTED in types
    assert EventType.REGIME_REBUILD_COMPLETED in types
    assert EventType.RECOVERY_COMPLETED in types
    assert engine.recovery["state"] == "completed"
    assert engine._data_gap_ok is True
    assert engine.readiness()["strategy_ready"] is True
    assert engine.oms.all_orders() == []


async def test_failed_recovery_keeps_system_not_ready():
    engine = await _warmed_engine()

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        raise RuntimeError("alpaca down")

    engine.history_client.fetch_candles = _fetch
    events = await _watch(engine)
    ok = await engine.resync(reason="test_failure")

    assert ok is False
    assert EventType.RECOVERY_FAILED in [e.type for e in events]
    assert engine.recovery["state"] == "failed"
    assert engine._data_gap_ok is False
    assert engine._build_risk_context(utcnow()).market_data_fresh is False


async def test_recovery_impossible_without_history_provider():
    engine = await _warmed_engine()
    engine.config.market_data.history.provider = "none"
    assert await engine.resync(reason="no_history") is False
    assert engine.recovery["state"] == "failed"