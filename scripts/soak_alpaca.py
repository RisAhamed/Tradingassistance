"""Long-duration live 1-minute bar soak test (Phase C1 reliability).

Read-only: execution stays disabled and the Alpaca trading client is never built.

Usage:
    .venv/Scripts/python.exe scripts/soak_alpaca.py --minutes 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.logging import configure_logging
from app.events.types import EventType
from app.runtime import build_runtime
from app.testing import SoakCollector, build_readiness_matrix, render_markdown

# Events the soak measures (README/report requirement).
MEASURED = (
    EventType.BAR_RECEIVED, EventType.BAR_REJECTED, EventType.BAR_DUPLICATE,
    EventType.BAR_OUT_OF_ORDER, EventType.BAR_GAP_DETECTED, EventType.DATA_GAP,
    EventType.FEATURES_UPDATED, EventType.REGIME_CHANGED, EventType.STRATEGY_EVALUATED,
    EventType.SIGNAL_GENERATED, EventType.RISK_EVALUATED, EventType.ENTRY_BLOCKED,
    EventType.EXECUTION_BLOCKED, EventType.RECOVERY_STARTED, EventType.RECOVERY_COMPLETED,
    EventType.RECOVERY_FAILED, EventType.ALPACA_DISCONNECTED, EventType.ALPACA_RECONNECTED,
    EventType.READINESS_CHANGED, EventType.SYSTEM_ERROR,
)


async def run(minutes: float | None) -> int:
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "alpaca"
    config.trading.broker = "mock"           # never construct a trading client
    config.execution.enabled = False         # EXECUTION OFF
    config.ai.enabled = False
    config.ai.permissions.allow_flatten = False
    config.market_data.history.provider = "alpaca"
    config.market_data.history.required = True
    soak = config.testing.soak
    minutes = minutes if minutes is not None else soak.duration_minutes
    configure_logging(config, env, project_root=PROJECT_ROOT)

    runtime = build_runtime(config, env)
    engine = runtime.engine
    assert engine.execution_enabled is False, "EXECUTION GATE MUST BE CLOSED"

    collector = SoakCollector()
    collector.attach(runtime.bus)
    extra: Counter = Counter()
    engine.bus.subscribe_all(lambda event: extra.update([event.type.value]))

    print("\n=== PHASE C1 SOAK (execution DISABLED, read-only) ===")
    print(f"symbol={config.trading.symbol} minutes={minutes} bars={config.market_data.bars.timeframe}")

    await runtime.startup()
    started = time.monotonic()
    try:
        await asyncio.sleep(minutes * 60)
    finally:
        await runtime.shutdown()

    result = collector.verify(
        config=config,
        engine=engine,
        duration_seconds=time.monotonic() - started,
        execution_disabled=True,
    )
    matrix = build_readiness_matrix(engine)

    print("\n--- MEASURED COUNTS ---")
    for event in MEASURED:
        print(f"  {event.value:<28} {collector.get(event)}")
    print("\n--- RESULT ---")
    print(json.dumps(result.as_dict(), indent=2, default=str))
    print("\n--- READINESS MATRIX ---")
    print(render_markdown(matrix))

    print("\n--- RUNTIME STATE ---")
    print(json.dumps({
        "execution_enabled": engine.execution_enabled,
        "alpaca_orders_submitted": len(engine.oms.all_orders()),
        "current_position": "flat" if engine.position_manager.is_flat else "open",
        "bar_status": engine.stream_state()["bars"],
        "regime": engine.readiness()["regime"],
        "readiness": engine.readiness(),
    }, indent=2, default=str))

    ok = result.passed and not matrix["failed"]
    print("\nSOAK RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase C1 live bar soak test")
    parser.add_argument("--minutes", type=float, default=None, help="override testing.soak.duration_minutes")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.minutes)))


if __name__ == "__main__":
    main()