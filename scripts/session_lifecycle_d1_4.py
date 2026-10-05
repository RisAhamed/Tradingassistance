"""Phase D.1.4 session lifecycle harness (deterministic, mock provider/broker).

Runs the full lifecycle with a controlled mock broker (no Alpaca orders, no
Alpaca trading client):

    SESSION ACTIVE -> ENTRY CUTOFF -> CLOSEOUT -> FLATTEN -> RECONCILE
    -> BROKER POSITION ZERO -> SESSION CLOSED

and prints the structured event trail. Artifacts are written under logs/.

Usage:
    .venv/Scripts/python.exe scripts/session_lifecycle_d1_4.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.clock import utcnow
from app.core.logging import configure_logging
from app.events.types import EventType
from app.runtime import build_runtime
from tests.support import SYMBOL

WATCH = (
    EventType.SESSION_STATE_CHANGED,
    EventType.ENTRY_CUTOFF_REACHED,
    EventType.ENTRY_BLOCKED_SESSION_CUTOFF,
    EventType.MAX_HOLDING_REACHED,
    EventType.FLATTEN_STARTED,
    EventType.FLATTEN_PARTIAL_FILL,
    EventType.FLATTEN_COMPLETED,
    EventType.FLATTEN_FAILED,
    EventType.BROKER_POSITION_READ,
    EventType.BROKER_POSITION_ZERO,
    EventType.SESSION_FLAT,
    EventType.SESSION_CLOSED,
    EventType.RECONCILIATION_STARTED,
    EventType.RECONCILIATION_COMPLETED,
    EventType.RECONCILIATION_FAILED,
    EventType.SESSION_CLOSEOUT_FAILED,
)


async def run() -> int:
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.execution.enabled = True          # controlled MOCK broker only
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = True
    config.logging.file.enabled = True
    config.market_data.max_future_skew_seconds = 86400 * 30
    configure_logging(config, env, project_root=PROJECT_ROOT)

    runtime = build_runtime(config, env)
    engine = runtime.engine
    print(f"\n=== PHASE D.1.4 SESSION LIFECYCLE HARNESS (mock broker, execution gate OPEN for mock only) ===")
    print(f"LOG_DIRECTORY = {(PROJECT_ROOT / config.logging.file.path).parent.resolve()}")

    seen: list[str] = []
    engine.bus.subscribe_all(lambda e: seen.append(e.type.value))

    await engine.start(reconcile=False)
    try:
        engine.broker.set_price(SYMBOL, 30000.0)
        # Inject an open position into both the engine and the mock broker.
        from app.core.ids import new_fill_id, new_order_id
        from app.domain.enums import Side
        from app.domain.orders import Fill
        from app.portfolio.position_manager import PositionManager

        fill = Fill(
            fill_id=new_fill_id(), order_id=new_order_id(), timestamp=utcnow(),
            symbol=SYMBOL, side=Side.BUY, quantity=0.25, price=30000.0,
        )
        engine.position_manager.apply_fill(fill)
        book = engine.broker._positions.setdefault(SYMBOL, PositionManager(SYMBOL))
        book.apply_fill(fill)
        print("injected position: long 0.25 BTC/USD @ 30000")

        # Force the session clock to flatten-deadline territory.
        deadline = engine.session._at(utcnow(), engine.config.session.flatten_deadline_time)
        if deadline is not None:
            from app.domain.enums import SessionState

            engine.session.entries_allowed = False
            engine.session._transition(SessionState.CLOSEOUT, reason="harness_deadline", now=deadline)
        ok = await engine.flatten(session_closeout=True)
        print(f"flatten result: {ok}")
        recon = await engine.reconcile()
        print(f"reconcile result: {recon}")
        print("session state:", engine.session.state.value)
        print("local position flat:", engine.position_manager.is_flat)
        print("orders:", len(engine.oms.all_orders()))
    finally:
        await engine.stop()

    counts = {t: seen.count(t) for t in {e.value for e in WATCH}}
    print("\n--- SESSION EVENT COUNTS ---")
    for name, count in sorted(counts.items()):
        print(f"  {name:<38} {count}")
    out = PROJECT_ROOT / "logs" / "reports" / "phase_d1_4_runtime.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"flatten_result={ok if 'ok' in dir() else None}",
        f"reconcile_result={recon if 'recon' in dir() else None}",
        f"session_state={engine.session.state.value}",
        f"local_flat={engine.position_manager.is_flat}",
        f"orders={len(engine.oms.all_orders())}",
        "event_counts=" + json.dumps(counts, indent=2),
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nartifact: {out}")
    print("RUNTIME RESULT:", "PASS" if ok and recon and engine.position_manager.is_flat else "FAIL")
    return 0 if ok and recon and engine.position_manager.is_flat else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
