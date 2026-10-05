"""Phase C1 verification tooling: soak collector, fault injector, readiness matrix,
restart safety, and execution-off guarantees after reconnect/recovery/restart."""
import asyncio
from datetime import timedelta

import pytest

from app.config.loader import get_env, load_config
from app.core.clock import utcnow
from app.domain.market import Candle
from app.events.types import EventType
from app.runtime import build_runtime
from app.testing import (
    FORBIDDEN_WHILE_DISABLED,
    FaultInjectingProvider,
    SoakCollector,
    build_readiness_matrix,
    render_markdown,
)
from tests.support import SYMBOL


def _config(execution_enabled: bool = False):
    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.execution.enabled = execution_enabled
    return config


def _engine(execution_enabled: bool = False):
    return build_runtime(_config(execution_enabled), get_env()).engine


def _bar(minutes: int, *, price: float = 30000.0) -> Candle:
    return Candle(
        timestamp=utcnow().replace(second=0, microsecond=0) + timedelta(minutes=minutes),
        symbol=SYMBOL, open=price, high=price * 1.001, low=price * 0.999,
        close=price * 1.0005, volume=1.0, timeframe="1m",
    )


# --- soak collector ---------------------------------------------------------
async def test_soak_collector_counts_and_passes_on_a_clean_run():
    engine = _engine()
    collector = SoakCollector()
    collector.attach(engine.bus)
    collector.mark_live_baseline(engine)
    engine.config.testing.soak.required_live_timeframes = []
    for i in range(1, 7):
        await engine.on_market_update(_bar(i))
    result = collector.verify(
        config=engine.config, engine=engine, duration_seconds=5.0, execution_disabled=True
    )
    assert result.passed, result.failures
    assert collector.get(EventType.BAR_DUPLICATE) == 0
    assert engine.market_stats["bars_received"] == 6
    assert result.live_candles["5m"] >= 1


async def test_soak_fails_when_gaps_exceed_tolerance():
    engine = _engine()
    engine.config.market_data.bars.max_gap_candles = 3
    engine.config.testing.soak.maximum_missing_bars = 0
    engine.config.market_data.bars.resync_enabled = False
    collector = SoakCollector()
    collector.attach(engine.bus)
    await engine.on_market_update(_bar(1))
    await engine.on_market_update(_bar(30))
    result = collector.verify(
        config=engine.config, engine=engine, duration_seconds=5.0, execution_disabled=True
    )
    assert not result.passed
    assert any("gap" in failure for failure in result.failures)


def test_soak_policy_is_configuration_driven():
    config = load_config(env=get_env())
    assert config.testing.soak.enabled is False          # never auto-enabled
    assert config.testing.soak.expected_bar_interval_seconds == 60.0
    assert config.testing.soak.duration_minutes == 60.0


# --- fault injector ---------------------------------------------------------
async def test_fault_injector_duplicate_bars_are_rejected():
    engine = _engine()
    wrapper = FaultInjectingProvider(engine.provider, scenario="duplicate_bars")
    wrapper.set_handler(engine.on_market_update)
    for i in range(1, 4):
        await wrapper._apply(_bar(i))
    assert engine.market_stats["bars_duplicate"] >= 1
    assert engine.market_stats["bars_received"] == 3


async def test_fault_injector_out_of_order_bars_are_rejected():
    engine = _engine()
    wrapper = FaultInjectingProvider(engine.provider, scenario="out_of_order_bars")
    wrapper.set_handler(engine.on_market_update)
    await wrapper._apply(_bar(1))
    await wrapper._apply(_bar(2))
    assert engine.market_stats["bars_out_of_order"] >= 1


async def test_fault_injector_silence_blocks_entries():
    engine = _engine()
    wrapper = FaultInjectingProvider(engine.provider, scenario="disconnect")
    wrapper.set_handler(engine.on_market_update)
    await wrapper._apply(_bar(1))
    assert engine.market_stats["bars_received"] == 0


# --- readiness matrix -------------------------------------------------------
async def test_readiness_matrix_is_machine_and_human_readable():
    engine = _engine()
    await engine.start()
    try:
        for i in range(1, 6):
            await engine.on_market_update(_bar(i))
        matrix = build_readiness_matrix(engine)
        assert isinstance(matrix["rows"], list)
        assert {"area", "status", "evidence", "blocking"} <= set(matrix["rows"][0])
        markdown = render_markdown(matrix)
        assert "| Area | Status | Evidence | Blocking? |" in markdown
        assert matrix["execution_enabled"] is False
        assert matrix["readiness"]["orders"] == 0
    finally:
        await engine.stop()


# --- execution-off guarantees ----------------------------------------------
def test_forbidden_while_disabled_list_is_non_empty():
    assert EventType.ORDER_SUBMITTED in FORBIDDEN_WHILE_DISABLED
    assert EventType.ORDER_CREATED in FORBIDDEN_WHILE_DISABLED


async def test_execution_stays_disabled_after_recovery():
    engine = _engine()
    await engine.warm_up()
    await engine.resync(reason="verification")
    assert engine.execution_enabled is False
    assert engine.oms.all_orders() == []
    assert engine.position_manager.is_flat


async def test_restart_does_not_duplicate_candles_or_enable_execution():
    """Stop, rebuild a fresh engine, warm up again: no duplicated candles."""
    first = _engine()
    await first.start()
    try:
        provider = first.provider
        ticks = 0
        while not provider.exhausted and ticks < 120:
            await provider.pump_one()
            ticks += 1
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        assert len(first.store.candles(first.symbol, "5m")) >= 1
    finally:
        await first.stop()

    second = _engine()  # simulates a process restart (fresh in-memory state)
    await second.start()
    try:
        provider = second.provider
        ticks = 0
        while not provider.exhausted and ticks < 120:
            await provider.pump_one()
            ticks += 1
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        candles = second.store.candles(second.symbol, "5m")
        timestamps = [c.timestamp for c in candles]
        assert timestamps == sorted(timestamps), "candle timestamps must be ordered"
        assert len(timestamps) == len(set(timestamps)), "no duplicate candles"
        assert second.execution_enabled is False
        assert second.oms.all_orders() == []
        assert second.position_manager.is_flat
        assert second.readiness()["orders"] == 0
    finally:
        await second.stop()