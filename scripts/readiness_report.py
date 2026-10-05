"""Emit the formal readiness matrix (machine-readable JSON + markdown).

Usage:
    .venv/Scripts/python.exe scripts/readiness_report.py --out logs/readiness.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.logging import configure_logging
from app.events.types import EventType
from app.market_data.history import compute_warmup_requirement
from app.runtime import build_runtime
from app.testing import build_readiness_matrix, render_markdown


def _history(count: int):
    from datetime import timedelta

    from app.core.clock import utcnow
    from app.domain.market import Candle
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


async def build(seed_history: bool) -> dict:
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"      # offline report driver
    config.trading.broker = "mock"
    config.execution.enabled = False
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.market_data.history.provider = "alpaca" if seed_history else "none"
    configure_logging(config, env, project_root=PROJECT_ROOT)

    runtime = build_runtime(config, env)
    engine = runtime.engine
    if seed_history:
        requirement = compute_warmup_requirement(engine.config)
        history_bars = _history(requirement.history_bars_needed + 5)

        async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
            return history_bars

        engine.history_client.fetch_candles = _fetch
    await engine.start()
    try:
        provider = engine.provider
        ticks = 0
        while not provider.exhausted and ticks < 400:
            await provider.pump_one()
            ticks += 1
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        await engine._publish_readiness_change()
        matrix = build_readiness_matrix(engine)
        matrix["event_types_present"] = sorted(e.value for e in EventType)
        return matrix
    finally:
        await engine.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Readiness matrix report")
    parser.add_argument("--out", default="logs/readiness.json")
    parser.add_argument("--no-history", action="store_true", help="skip seeded history")
    args = parser.parse_args()
    matrix = asyncio.run(build(seed_history=not args.no_history))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(matrix, indent=2, default=str), encoding="utf-8")
    markdown = render_markdown(matrix)
    Path(args.out).with_suffix(".md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\nwritten: {out} and {out.with_suffix('.md')}")


if __name__ == "__main__":
    main()