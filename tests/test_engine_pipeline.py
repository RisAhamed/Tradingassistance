"""Deterministic pipeline test: mock market data -> engine -> fill -> flat."""
import asyncio

import pytest

from app.config.loader import get_env, load_config
from app.core.logging import configure_logging, reset_logging_state
from app.config.loader import PROJECT_ROOT
from app.market_data.mock import MockMarketDataProvider
from app.runtime import build_runtime


@pytest.mark.asyncio
async def test_mock_run_opens_and_flattens():
    reset_logging_state()
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    # This test exercises the FULL pipeline including order creation against the
    # in-memory mock broker, so it explicitly opens the Phase B execution gate.
    # (Phase B safety — "execution disabled blocks orders" — is covered by
    # tests/test_phase_b_execution_gate.py.)
    config.execution.enabled = True
    configure_logging(config, env, project_root=PROJECT_ROOT)

    runtime = build_runtime(config, env)
    await runtime.engine.start()
    try:
        provider = runtime.engine.provider
        assert isinstance(provider, MockMarketDataProvider)
        ticks = 0
        while not provider.exhausted and ticks < 700:
            assert await provider.pump_one()
            ticks += 1
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        assert runtime.engine.state.recent_signals, "expected at least one signal"
        assert runtime.engine.oms.all_orders(), "expected at least one order"
        assert await runtime.engine.flatten(session_closeout=True)
        assert runtime.engine.position_manager.is_flat
    finally:
        await runtime.engine.stop()
