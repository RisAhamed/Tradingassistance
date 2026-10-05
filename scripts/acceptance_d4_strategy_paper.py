"""Phase D.4 — strategy-driven execution acceptance.

PART A (real market data, no orders): real Alpaca provider drives the full
read-only pipeline (warm-up -> handoff -> features -> regime -> timeframe
-> strategy). No orders are possible here (gate closed in-process).

PART B (bridge test, REAL Alpaca paper broker): the SAME engine pipeline is
fed a deterministic mock market series. A REAL strategy signal therefore
produces a REAL Alpaca paper order. This proves:

    MOCK MARKET (strategy-proven) -> FEATURES -> REGIME -> TIMEFRAME
    -> STRATEGY -> SIGNAL -> RISK -> SIZING -> TRADE_PLAN -> GATE
    -> ORDER INTENT -> OMS -> ALPACA REAL PAPER BROKER -> FILL
    -> POSITION -> EXIT (controlled test exit) -> FLATTEN -> RECONCILE

This is explicitly a BRIDGE test: real strategy + real risk + real OMS +
real paper broker; deterministic inputs only. A run with fully live market
data + live signal remains "liveness opportunity" and is reported honestly.

Usage:
    .venv/Scripts/python.exe scripts/acceptance_d4_strategy_paper.py
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
from app.runtime import build_runtime


async def part_a() -> dict:
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "alpaca"
    config.trading.broker = "mock"
    config.execution.enabled = False
    config.ai.enabled = False
    config.storage.enabled = False
    config.market_data.history.provider = "alpaca"
    configure_logging(config, env, project_root=PROJECT_ROOT)
    runtime = build_runtime(config, env)
    engine = runtime.engine
    counts: dict[str, int] = {}
    print(f"LOG_DIRECTORY (in-process) -> {(PROJECT_ROOT / config.logging.file.path).parent}")
    print("PART A: real Alpaca market data, gate CLOSED")
    await runtime.startup()
    counts: dict[str, int] = {}
    engine.bus.subscribe_all(lambda e: counts.__setitem__(e.type.value, counts.get(e.type.value, 0) + 1))
    try:
        await asyncio.sleep(60.0)
    finally:
        rdy = engine.readiness()
        counts_now = dict(counts)
        await runtime.shutdown()
    return {"events": counts_now, "readiness": rdy}


async def part_b() -> dict:
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "alpaca"           # REAL paper endpoint
    config.execution.enabled = True            # in-process only
    config.ai.enabled = False
    config.storage.enabled = False
    config.market_data.max_future_skew_seconds = 86400 * 30
    config.market_data.history.provider = "none"
    # D.4: keep the bridge's quantity inside Alpaca's notional limit because
    # the mock-priced sizing cannot see the real BTC/USD price.
    config.backtesting.initial_capital = 10000.0
    config.position_sizing.maximum_quantity = 0.01
    configure_logging(config, env, project_root=PROJECT_ROOT)

    evidence: dict = {}
    # Preflight
    if config.trading.mode != "paper" or env.is_live_alpaca_endpoint() or not env.alpaca_paper:
        return {"result": "BLOCKED", "reason": "paper mode or paper endpoint failed"}
    runtime = build_runtime(config, env)
    engine = runtime.engine
    counts: dict[str, int] = {}
    engine.bus.subscribe_all(lambda e: counts.__setitem__(e.type.value, counts.get(e.type.value, 0) + 1))
    print("PART B: mock market + REAL Alpaca paper broker (strategy-driven)")
    await runtime.startup()
    provider = engine.provider
    position_ever_open = False
    try:
        ticks = 0
        while not provider.exhausted and ticks < 700:
            if not await provider.pump_one():
                break
            ticks += 1
            if not engine.position_manager.is_flat:
                position_ever_open = True
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        flatten = await engine.flatten(session_closeout=True)
        reconcile = await engine.reconcile()
        orders = engine.oms.all_orders()
        evidence = {
            "ticks": ticks,
            "event_counts": counts,
            "orders_created": len(orders),
            "position_ever_open": position_ever_open,
            "flatten_ok": flatten,
            "reconcile_ok": reconcile,
            "final_flat": engine.position_manager.is_flat,
            "broker": engine.broker.name,
            "alpaca_trading_requests": 1 if orders else 0,
            "session_state": engine.session.state.value,
        }
    finally:
        await runtime.shutdown()
    evidence["result"] = (
        "PASS"
        if evidence.get("final_flat")
        and evidence.get("reconcile_ok")
        and evidence.get("flatten_ok")
        and evidence.get("orders_created", 0) > 0
        and evidence.get("position_ever_open") is True
        and counts.get("OrderFilled", 0) > 0
        else "FAIL"
    )
    return evidence


async def main() -> int:
    a = {}
    try:
        a = await part_a()
    except Exception as exc:  # noqa: BLE001
        a = {"error": str(exc)[:300]}
    b = {}
    try:
        b = await part_b()
    except Exception as exc:  # noqa: BLE001
        b = {"error": str(exc)[:300], "result": "ERROR"}
    report = {"phase": "D.4", "part_a_real_market_data_gate_closed": a, "part_b_bridge_real_broker_strategy_driven": b}
    out = PROJECT_ROOT / "logs" / "reports" / "phase_d4_bridge_acceptance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print("\n=== D.4 BRIDGE ACCEPTANCE ===")
    print(json.dumps(report, indent=2, default=str))
    print("RESULT:", b.get("result"))
    return 0 if b.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
