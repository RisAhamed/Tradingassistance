"""End-to-end paper run against the deterministic mock provider + mock broker.

Usage:  .venv/Scripts/python.exe scripts/demo_mock.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.logging import configure_logging
from app.market_data.mock import MockMarketDataProvider
from app.runtime import build_runtime


async def main() -> int:
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.logging.console.format = "structured"
    # Phase D.1.1 is diagnostic only: the execution gate stays closed and the
    # mock provider exercises the decision pipeline without creating orders.
    config.execution.enabled = False
    configure_logging(config, env, project_root=PROJECT_ROOT)

    runtime = build_runtime(config, env)
    await runtime.startup()

    engine = runtime.engine
    provider = engine.provider
    assert isinstance(provider, MockMarketDataProvider)

    ticks = 0
    while not provider.exhausted and ticks < 700:
        if not await provider.pump_one():
            break
        ticks += 1
        await asyncio.sleep(0)

    await asyncio.sleep(0.1)

    payload = engine.payload()
    summary = {
        "ticks": ticks,
        "session_state": engine.session.state.value,
        "signals": len(engine.state.recent_signals),
        "orders": len(engine.oms.all_orders()),
        "rejections": len(engine.state.rejections),
        "position_flat": engine.position_manager.is_flat,
        "trades": len(engine.state.trades),
        "realized_pnl": round(engine.pnl.realized, 4),
        "fees": round(engine.pnl.fees, 4),
        "equity": round(engine.pnl.equity, 4),
        "regime": payload["regime"]["regime"] if payload["regime"] else None,
        "health": payload["health"]["status"],
        "reconciliation_ok": payload["reconciliation"]["ok"],
        "candles_5m": len(engine.store.candles(engine.symbol, "5m")),
        "candles_15m": len(engine.store.candles(engine.symbol, "15m")),
    }
    print("DEMO SUMMARY")
    print(json.dumps(summary, indent=2))

    summary["final_flat"] = engine.position_manager.is_flat
    print(json.dumps({k: summary[k] for k in ("orders", "final_flat", "session_state")}, indent=2))

    await runtime.shutdown()
    return 0 if summary["orders"] == 0 and summary["final_flat"] and summary["health"] != "error" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
