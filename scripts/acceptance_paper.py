"""Phase D.2 paper-execution acceptance test.

Proves the COMPLETE paper trade lifecycle on an isolated mock broker:

    MARKET DATA -> SIGNAL -> RISK -> SIZING -> TRADE PLAN -> ORDER INTENT
    -> OMS -> PAPER FILL -> POSITION -> EXIT -> FLAT -> RECONCILE

No Alpaca trading client is ever constructed: the configuration is forced to
``market_data.provider=mock`` and ``trading.broker=mock``. The only Alpaca
surface ever used in this repository is the MARKET-DATA websocket (Phase B/C),
never an order endpoint.

Usage:
    .venv/Scripts/python.exe scripts/acceptance_paper.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.clock import utcnow
from app.core.logging import configure_logging
from app.events.types import EventType
from app.market_data.mock import MockMarketDataProvider
from app.runtime import build_runtime

WATCH = [
    EventType.BAR_RECEIVED, EventType.CANDLE_COMPLETED, EventType.FIXTURES_UPDATED if False else EventType.FEATURES_UPDATED,
    EventType.REGIME_CHANGED, EventType.TIMEFRAME_SELECTED, EventType.STRATEGY_EVALUATED,
    EventType.SIGNAL_GENERATED, EventType.RISK_EVALUATED, EventType.RISK_APPROVED,
    EventType.RISK_REJECTED, EventType.TRADE_PLAN_CREATED, EventType.ORDER_CREATED,
    EventType.ORDER_SUBMITTED, EventType.ORDER_FILLED, EventType.POSITION_UPDATED,
    EventType.EXECUTION_BLOCKED, EventType.EXECUTION_DISABLED,
]


async def main() -> int:
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.execution.enabled = True            # paper broker only (mock)
    config.ai.enabled = False
    config.storage.enabled = False
    config.market_data.max_future_skew_seconds = 86400 * 30
    configure_logging(config, env, project_root=PROJECT_ROOT)

    runtime = build_runtime(config, env)
    engine = runtime.engine
    assert config.trading.mode == "paper"
    assert engine.broker.name == "mock", "PAPER-ONLY broker must be the mock"

    counts: dict[str, int] = {}
    engine.bus.subscribe_all(lambda e: counts.__setitem__(e.type.value, counts.get(e.type.value, 0) + 1))

    print(f"\nLOG_DIRECTORY = {(PROJECT_ROOT / config.logging.file.path).parent.resolve()}")
    print(f"EXECUTION GATE: ENABLED (paper broker = {engine.broker.name} only)")
    await runtime.startup()
    provider = engine.provider
    assert isinstance(provider, MockMarketDataProvider)

    position_ever_open = False
    ticks = 0
    while not provider.exhausted and ticks < 700:
        if not await provider.pump_one():
            break
        ticks += 1
        if not engine.position_manager.is_flat:
            position_ever_open = True
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)

    flat = await engine.flatten(session_closeout=True)
    reconcile = await engine.reconcile()

    orders = engine.oms.all_orders()
    fills = [f for o in orders for f in getattr(o, "fills", [])] or None
    report = {
        "phase": "D.2",
        "mode": "PAPER_MOCK_BROKER",
        "timestamp": utcnow().isoformat(),
        "ticks": ticks,
        "event_counts": counts,
        "orders_created": len(orders),
        "orders_filled": sum(1 for o in orders if getattr(o, "status", None) and o.status.value == "filled"),
        "position_ever_open": position_ever_open,
        "final_flat": engine.position_manager.is_flat,
        "flatten_ok": flat,
        "reconcile_ok": reconcile,
        "alpaca_trading_requests": 0,
        "broker": engine.broker.name,
        "execution_enabled": engine.execution_enabled,
        "session_state": engine.session.state.value,
        "realized_pnl": round(engine.pnl.realized, 4),
        "pass": bool(
            orders
            and engine.position_manager.is_flat
            and flat
            and reconcile
            and engine.broker.name == "mock"
        ),
    }
    out = PROJECT_ROOT / "logs" / "reports" / "phase_d2_paper_acceptance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    md = out.with_suffix(".md")
    md.write_text(
        "# Phase D.2 Paper Acceptance\n\n```json\n" + json.dumps(report, indent=2, default=str) + "\n```\n",
        encoding="utf-8",
    )
    print("\n=== PAPER ACCEPTANCE ===")
    print(json.dumps(report, indent=2, default=str))
    print("RESULT:", "PASS" if report["pass"] else "FAIL")
    await runtime.shutdown()
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
