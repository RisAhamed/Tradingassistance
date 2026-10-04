"""Headless paper-trading runner:  ``python -m app.runner.paper``.

Runs the deterministic engine against the configured provider/broker (real
market data + paper broker by default). With a mock provider it replays the
scripted series, then performs a verified flatten.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.logging import configure_logging
from app.market_data.mock import MockMarketDataProvider
from app.runtime import build_runtime

logger = logging.getLogger("app.runner.paper")


async def run(ticks: int | None = None, duration: float | None = None) -> int:
    env = get_env()
    config = load_config(env=env)
    configure_logging(config, env, project_root=PROJECT_ROOT)

    runtime = build_runtime(config, env)
    await runtime.startup()

    exit_code = 0
    try:
        provider = runtime.engine.provider
        if isinstance(provider, MockMarketDataProvider):
            count = 0
            started = time.monotonic()
            while not provider.exhausted:
                if not await provider.pump_one():
                    break
                count += 1
                await asyncio.sleep(0.01)
                if ticks is not None and count >= ticks:
                    break
                if duration is not None and (time.monotonic() - started) >= duration:
                    break
            logger.info(
                "mock series replayed",
                extra={"structured": {"event": "PAPER_RUN_COMPLETE", "component": "runner", "ticks": count}},
            )
            # End-of-session invariant: flatten and verify POSITION = 0.
            flat = await runtime.engine.flatten(session_closeout=True)
            if not flat:
                exit_code = 1
        else:
            logger.info(
                "streaming live (Ctrl+C to stop)",
                extra={"structured": {"event": "PAPER_RUN_START", "component": "runner", "provider": provider.name}},
            )
            started = time.monotonic()
            while True:
                await asyncio.sleep(1.0)
                if duration is not None and (time.monotonic() - started) >= duration:
                    break
            if not runtime.engine.position_manager.is_flat:
                flat = await runtime.engine.flatten(session_closeout=True)
                if not flat:
                    exit_code = 1
    finally:
        await runtime.shutdown()
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper trading runner")
    parser.add_argument("--ticks", type=int, default=None, help="max mock ticks to replay")
    parser.add_argument("--duration", type=float, default=None, help="max wall-clock seconds")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(ticks=args.ticks, duration=args.duration)))


if __name__ == "__main__":
    main()
