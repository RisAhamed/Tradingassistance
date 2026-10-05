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
from datetime import datetime, timezone
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


async def run(minutes: float | None, seconds: float | None, report_path: str | None) -> int:
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
    duration_seconds = seconds if seconds is not None else (
        minutes * 60.0 if minutes is not None else soak.duration_minutes * 60.0
    )
    configure_logging(config, env, project_root=PROJECT_ROOT)

    runtime = build_runtime(config, env)
    engine = runtime.engine
    assert engine.execution_enabled is False, "EXECUTION GATE MUST BE CLOSED"

    collector = SoakCollector()
    collector.attach(runtime.bus)
    extra: Counter = Counter()
    engine.bus.subscribe_all(lambda event: extra.update([event.type.value]))

    print("\n=== PHASE C1 SOAK (execution DISABLED, read-only) ===")
    log_path = PROJECT_ROOT / config.logging.file.path
    print(f"symbol={config.trading.symbol} seconds={duration_seconds} bars={config.market_data.bars.timeframe}")
    print(f"LOG_DIRECTORY = {log_path.parent.resolve()}")
    print(f"EXECUTION_ENABLED = {engine.execution_enabled}")

    await runtime.startup()
    collector.mark_live_baseline(engine)
    started = time.monotonic()
    try:
        await asyncio.sleep(duration_seconds)
        duration = time.monotonic() - started
        result = collector.verify(
            config=config,
            engine=engine,
            duration_seconds=duration,
            execution_disabled=True,
        )
        matrix = build_readiness_matrix(engine)
    finally:
        await runtime.shutdown()
    report = {
        "source": "REAL_ALPACA",
        "phase": "D.1.2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 2),
        "execution_enabled": engine.execution_enabled,
        "orders_created": len(engine.oms.all_orders()),
        "orders_submitted": collector.get(EventType.ORDER_SUBMITTED),
        "position": "flat" if engine.position_manager.is_flat else "open",
        "result": result.as_dict(),
        "readiness": matrix,
        "handoff": engine.warmup_status(),
        "streams": engine.stream_state(),
        "recovery": dict(engine.recovery),
        "provider_rejected_count": getattr(engine.provider, "rejected_count", None),
        "candles_total": {tf: len(engine.store.candles(engine.symbol, tf)) for tf in engine.timeframes},
        "live_candles": result.live_candles,
    }
    output_path = Path(report_path) if report_path else PROJECT_ROOT / "logs" / "reports" / "phase_d1_2_soak.json"
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    markdown_path = output_path.with_suffix(".md")
    markdown_path.write_text(
        "# Phase D.1.2 Real Alpaca Soak\n\n"
        f"- Source: `{report['source']}`\n- Duration: `{report['duration_seconds']}s`\n"
        f"- Execution enabled: `{report['execution_enabled']}`\n"
        f"- Orders created/submitted: `{report['orders_created']}/{report['orders_submitted']}`\n"
        f"- Position: `{report['position']}`\n\n"
        "## Result\n\n```json\n" + json.dumps(result.as_dict(), indent=2, default=str) + "\n```\n\n"
        "## Readiness\n\n" + render_markdown(matrix) + "\n",
        encoding="utf-8",
    )

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
        "live_candles": result.live_candles,
        "reconnects": result.reconnects,
        "recovery_events": result.recovery_events,
        "report_json": str(output_path),
        "report_markdown": str(markdown_path),
    }, indent=2, default=str))

    ok = result.passed and not matrix["failed"]
    print("\nSOAK RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase C1 live bar soak test")
    parser.add_argument("--minutes", type=float, default=None, help="override testing.soak.duration_minutes")
    parser.add_argument("--seconds", type=float, default=None, help="override soak duration in seconds")
    parser.add_argument("--report", type=str, default=None, help="JSON report path, relative to the repository by default")
    args = parser.parse_args()
    if args.minutes is not None and args.seconds is not None:
        parser.error("use either --minutes or --seconds, not both")
    raise SystemExit(asyncio.run(run(args.minutes, args.seconds, args.report)))


if __name__ == "__main__":
    main()