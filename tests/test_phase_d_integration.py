"""Phase D integration smoke test — engine startup + freshness + timeframes + TradePlan."""
from __future__ import annotations

import asyncio
import pytest

from app.config.loader import get_env, load_config
from app.runtime import build_runtime


@pytest.mark.asyncio
async def test_engine_phase_d_smoke() -> None:
    env = get_env()
    cfg = load_config(env=env)
    cfg.ai.enabled = False
    cfg.storage.enabled = False
    cfg.market_data.provider = "mock"
    cfg.market_data.max_future_skew_seconds = 86400 * 30
    cfg.trading.broker = "mock"
    runtime = build_runtime(cfg, env)
    await runtime.engine.start()
    try:
        await asyncio.sleep(2.0)
        payload = runtime.engine.payload()
        assert "freshness_policy" in payload
        assert payload["freshness_policy"]["mode"] == "cadence_aware"
        assert "timeframe_selection" in payload
        ts = payload["timeframe_selection"]
        assert ts is None or "context_timeframe" in ts
        assert "trade_plan" in payload
        assert not runtime.engine.execution_enabled
    finally:
        await runtime.engine.stop()
