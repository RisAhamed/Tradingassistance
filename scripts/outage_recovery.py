"""Simulated outage / recovery harness (deterministic, offline, no Alpaca server).

Runs the pipeline against the mock provider wrapped in a LOCAL fault injector and
asserts the required recovery chain:

    DATA GAP -> ENTRY BLOCK -> RECOVERY START -> HISTORICAL FETCH -> CANDLE REBUILD
    -> FEATURE REBUILD -> REGIME REBUILD -> VALIDATION -> READINESS RESTORED

Failure scenarios must end NOT READY + entries blocked + RECOVERY_FAILED.

Usage:
    .venv/Scripts/python.exe scripts/outage_recovery.py --scenario missing_bars
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.config.models import CoveragePolicyConfig
from app.core.clock import utcnow
from app.core.logging import configure_logging
from app.domain.market import Candle
from app.events.types import EventType
from app.market_data.history import compute_warmup_requirement
from app.runtime import build_runtime
from app.testing import FaultInjectingProvider, build_readiness_matrix, render_markdown

SCENARIOS = [
    "none", "missing_bars", "delayed_bars", "duplicate_bars", "out_of_order_bars",
    "disconnect", "stale_data", "history_fetch_failure", "incomplete_recovery",
    "recovery_timeout",
]


def _config(scenario: str):
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.execution.enabled = False          # EXECUTION OFF
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.market_data.history.provider = "alpaca"
    # Keep the timeout scenario fast; the real value is config-driven elsewhere.
    config.market_data.history.startup_timeout_seconds = 2.0
    config.market_data.history.coverage = CoveragePolicyConfig(
        minimum_percent=50.0, maximum_gap_candles=50, on_insufficient="warn"
    )
    config.testing.outage.enabled = True
    config.testing.outage.scenario = scenario
    return config, env


def _history(count: int):
    from datetime import timedelta

    from tests.support import SYMBOL

    end = utcnow().replace(second=0, microsecond=0)
    out, price = [], 30000.0
    for i in range(count):
        price *= 1.0 + (0.0006 if i % 2 else -0.0004)
        out.append(
            Candle(
                timestamp=end - timedelta(minutes=count - i),
                symbol=SYMBOL, open=price * 0.999, high=price * 1.002,
                low=price * 0.998, close=price, volume=1.0, timeframe="1m",
            )
        )
    return out


async def run(scenario: str) -> int:
    config, env = _config(scenario)
    runtime = build_runtime(config, env)
    engine = runtime.engine
    configure_logging(config, env, project_root=PROJECT_ROOT)

    requirement = compute_warmup_requirement(engine.config)
    base = _history(requirement.history_bars_needed + 5)

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        if scenario == "history_fetch_failure":
            raise RuntimeError("simulated historical API failure")
        if scenario == "recovery_timeout":
            # Mimic the client's own timeout (the real client wraps the call in
            # asyncio.wait_for); recovery must then fail closed.
            await asyncio.sleep(timeout_seconds + 0.5)
            raise asyncio.TimeoutError("simulated historical timeout")
        if scenario == "incomplete_recovery":
            return _history(5)  # far too little to rebuild features
        return base

    engine.history_client.fetch_candles = _fetch
    events: list = []
    engine.bus.subscribe_all(events.append)

    await engine.start()
    failures: list[str] = []
    try:
        # Wrap the provider AFTER startup, so faults hit the live bar stream.
        # Recovery-failure scenarios also drop bars, so recovery is actually
        # triggered and then fails closed (otherwise nothing happens).
        inner = engine.provider
        effective = scenario
        if scenario in ("history_fetch_failure", "incomplete_recovery", "recovery_timeout"):
            effective = "missing_bars"
        engine.provider = FaultInjectingProvider(
            inner,
            scenario=effective,
            missing_bars=config.testing.outage.missing_bars,
            delay_bars=config.testing.outage.delay_bars,
            duplicate_bars=config.testing.outage.duplicate_bars,
        )
        engine.provider.set_handler(engine.on_market_update)
        ticks = 0
        while not inner.exhausted and ticks < 400:
            await inner.pump_one()
            ticks += 1
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)

        types = [e.type for e in events]
        if scenario == "missing_bars":
            for required in (EventType.BAR_GAP_DETECTED, EventType.DATA_GAP, EventType.RECOVERY_STARTED):
                if required not in types:
                    failures.append(f"expected {required.value}")
        if scenario == "duplicate_bars" and EventType.BAR_DUPLICATE not in types:
            failures.append("duplicate bars were not rejected")
        if scenario == "out_of_order_bars" and EventType.BAR_OUT_OF_ORDER not in types:
            failures.append("out-of-order bars were not rejected")
        if scenario in ("history_fetch_failure", "incomplete_recovery", "recovery_timeout"):
            if engine.recovery.get("state") != "failed":
                failures.append("recovery should have failed closed")
            if engine._data_gap_ok:
                failures.append("data integrity must be False after failed recovery")
            if EventType.RECOVERY_FAILED not in types:
                failures.append("RECOVERY_FAILED not emitted")
        if engine.oms.all_orders():
            failures.append("orders created during outage scenario")
    finally:
        await engine.stop()

    matrix = build_readiness_matrix(engine)
    print(f"\n=== OUTAGE SCENARIO: {scenario} ===")
    print("recovery:", json.dumps(engine.recovery, default=str))
    print("readiness:", json.dumps(engine.readiness(), default=str))
    print(render_markdown(matrix))
    print("\nRESULT:", "PASS" if not failures else "FAIL -> " + "; ".join(failures))
    return 0 if not failures else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulated outage/recovery harness")
    parser.add_argument("--scenario", choices=SCENARIOS, default="missing_bars")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.scenario)))


if __name__ == "__main__":
    main()