"""PHASE B REAL-DATA ACCEPTANCE TEST — Alpaca market data, ZERO order submission.

Read-only by construction:

* ``execution.enabled`` is forced to **false** (and verified before starting).
* the broker adapter is forced to the in-memory mock, so no Alpaca *trading*
  client is ever constructed;
* only the Alpaca **market-data** websocket is used.

Usage:
    .venv/Scripts/python.exe scripts/acceptance_alpaca.py --seconds 120

It prints a pass/fail checklist for every acceptance criterion.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.logging import configure_logging
from app.events.types import EventType
from app.runtime import build_runtime

WATCHED = [
    EventType.WARMUP_STARTED,
    EventType.WARMUP_REQUESTED,
    EventType.WARMUP_RECEIVED,
    EventType.WARMUP_CANDLES_BUILT,
    EventType.WARMUP_FEATURES_READY,
    EventType.WARMUP_REGIME_READY,
    EventType.WARMUP_COMPLETED,
    EventType.WARMUP_FAILED,
    EventType.LIVE_HANDOFF_STARTED,
    EventType.LIVE_HANDOFF_COMPLETED,
    EventType.DATA_GAP,
    EventType.BAR_STREAM_CONNECTED,
    EventType.BAR_STREAM_SUBSCRIBED,
    EventType.BAR_REJECTED,
    EventType.BAR_DUPLICATE,
    EventType.BAR_OUT_OF_ORDER,
    EventType.BAR_GAP_DETECTED,
    EventType.HISTORICAL_COVERAGE_CHECKED,
    EventType.HISTORICAL_COVERAGE_FAILED,
    EventType.RECOVERY_STARTED,
    EventType.RECOVERY_COMPLETED,
    EventType.RECOVERY_FAILED,
    EventType.DATA_GAP,
    EventType.ALPACA_CONNECTING,
    EventType.ALPACA_CONNECTED,
    EventType.ALPACA_SUBSCRIPTION_STARTED,
    EventType.ALPACA_DISCONNECTED,
    EventType.ALPACA_RECONNECTING,
    EventType.ALPACA_RECONNECTED,
    EventType.ALPACA_RECONNECT_FAILED,
    EventType.CANDLE_STARTED,
    EventType.CANDLE_COMPLETED,
    EventType.FEATURES_UPDATED,
    EventType.REGIME_CHANGED,
    EventType.SIGNAL_GENERATED,
    EventType.RISK_APPROVED,
    EventType.RISK_REJECTED,
    EventType.EXECUTION_DISABLED,
    EventType.EXECUTION_BLOCKED,
    EventType.MARKET_DATA_STALE,
]


async def run(seconds: int) -> int:
    env = get_env()
    config = load_config(env=env)

    # --- hard safety: read-only market data, no broker -------------------
    config.market_data.provider = "alpaca"
    config.trading.broker = "mock"          # never build an Alpaca trading client
    config.execution.enabled = False        # ORDER SUBMISSION IS OFF
    config.ai.enabled = False               # AI must not touch execution
    config.ai.permissions.allow_flatten = False
    # Phase C: warm up from real Alpaca history before the live stream starts.
    config.market_data.history.enabled = True
    config.market_data.history.provider = "alpaca"
    config.market_data.history.required = True
    config.logging.console.format = "structured"
    configure_logging(config, env, project_root=PROJECT_ROOT)

    runtime = build_runtime(config, env)
    engine = runtime.engine
    assert engine.execution_enabled is False, "EXECUTION GATE MUST BE CLOSED"

    counts: Counter = Counter()
    engine.bus.subscribe_all(lambda event: counts.update([event.type.value]))

    print("\n=== PHASE B ACCEPTANCE: Alpaca real market data, execution DISABLED ===")
    print(f"symbol={config.trading.symbol}  feed={config.market_data.feed}  seconds={seconds}")
    print("execution.enabled =", engine.execution_enabled, "| broker =", config.trading.broker)
    print("-" * 70)

    await runtime.startup()
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:  # pragma: no cover
        pass
    finally:
        await runtime.shutdown()

    snapshot = engine.state.latest_snapshot
    candles = {tf: len(engine.store.candles(engine.symbol, tf)) for tf in engine.timeframes}
    features = engine.state.features
    regime = engine.state.regime
    payload = engine.payload()

    # Warm-up progress: the regime needs `regime.min_candles` COMPLETED context
    # candles (15m by default), so a short run legitimately leaves it UNKNOWN.
    context_tf = config.timeframes.context
    context_have = candles.get(context_tf, 0)
    context_need = config.regime.min_candles
    warmup_remaining = max(0, context_need - context_have)

    warm = engine.warmup_status()
    streams = engine.stream_state()
    readiness = engine.readiness()
    provider_rejected = getattr(engine.provider, "rejected_count", None)
    bars_cfg = config.market_data.bars
    checks = [
        # --- Phase C1: historical warm-up + coverage -------------------------
        ("W1. historical data requested", counts.get(EventType.WARMUP_REQUESTED.value, 0) > 0),
        ("W2. historical data received", counts.get(EventType.WARMUP_RECEIVED.value, 0) > 0),
        ("W3. coverage analysed", counts.get(EventType.HISTORICAL_COVERAGE_CHECKED.value, 0) > 0),
        ("W4. warm-up completed", warm.get("status") == "completed"),
        ("W5. features ready from history", bool(warm.get("features_ready"))),
        ("W6. regime ready from history", bool(warm.get("regime_ready"))),
        # --- Phase C1: live 1-minute bar stream ------------------------------
        ("B1. bar stream connected", counts.get(EventType.BAR_STREAM_CONNECTED.value, 0) > 0),
        ("B2. bars subscribed", counts.get(EventType.BAR_STREAM_SUBSCRIBED.value, 0) > 0),
        ("B3. live bars arrived", engine._live_bar_count > 0),
        ("B4. bar freshness meaningful", streams["bars"]["fresh"] is not None),
        # --- live feed --------------------------------------------------------
        ("L1. Alpaca connects", counts.get(EventType.ALPACA_CONNECTED.value, 0) > 0),
        ("L2. subscription started", counts.get(EventType.ALPACA_SUBSCRIPTION_STARTED.value, 0) > 0),
        ("L3. live data arrived", snapshot is not None),
        ("L4. data normalized", bool(snapshot and snapshot.price and snapshot.price > 0)),
        ("L5. quotes observable separately", streams["quotes"]["last_at"] is not None),
        ("L6. handoff completed", warm.get("live_handoff") == "completed"),
        ("L7. no excessive data gap", bool(warm.get("gap", {}).get("within_tolerance", True))),
        ("L8. real timestamps not future-dated", all(
            stream.get("clock_state") != "FUTURE_TIMESTAMP"
            for stream in streams.values()
            if stream.get("last_at") is not None
        )),
        # --- safety -----------------------------------------------------------
        ("S1. execution disabled", payload["execution"]["status"] == "DISABLED"),
        ("S2. NO order created", len(engine.oms.all_orders()) == 0),
        ("S3. position flat", engine.position_manager.is_flat),
        ("S4. strategy readiness reported", "strategy_ready" in readiness),
    ]

    print("\n--- MARKET STATE ---")
    print(json.dumps(
        {
            "provider": engine.provider.name,
            "connected": engine.provider.health().connected,
            "bid": snapshot.bid if snapshot else None,
            "ask": snapshot.ask if snapshot else None,
            "last": snapshot.last if snapshot else None,
            "spread_percent": round(snapshot.spread_percent, 6) if snapshot else None,
            "candles": candles,
            "features_ready": features.ready if features else None,
            "features_missing": features.missing if features else None,
            "regime": regime.regime.value if regime else None,
            "regime_reason": regime.reason if regime else "not classified yet (warm-up)",
            "warmup": {
                "context_timeframe": context_tf,
                "context_candles": context_have,
                "context_candles_required": context_need,
                "candles_remaining": warmup_remaining,
                "approx_minutes_remaining": round(warmup_remaining * 15, 1),
            },
            "signals": len(engine.state.recent_signals),
            "risk_decision": engine._last_decision.reason.value if engine._last_decision else None,
            "orders": len(engine.oms.all_orders()),
            "freshness": streams,
            "handoff": {
                "live_bar_count": engine._live_bar_count,
                "last_historical_at": engine._last_historical_at,
                "last_bar_at": engine._last_bar_at,
                "live_handoff": warm.get("live_handoff"),
                "first_live_at": warm.get("first_live_at"),
            },
            "provider_rejected_count": provider_rejected,
            "warmup": {
                "status": warm.get("status"),
                "required_bars": warm.get("required_bars"),
                "requested_bars": warm.get("requested_bars"),
                "historical_candles": warm.get("historical_candles"),
                "candles_built": warm.get("historical_candles_built") or candles,
                "features_ready": warm.get("features_ready"),
                "regime_ready": warm.get("regime_ready"),
                "last_historical_at": warm.get("last_historical_at"),
                "first_live_at": warm.get("first_live_at"),
                "live_handoff": warm.get("live_handoff"),
                "gap": warm.get("gap"),
            },
        },
        indent=2,
        default=str,
    ))

    print("\n--- EVENT COUNTS ---")
    for event in WATCHED:
        value = counts.get(event.value, 0)
        if value:
            print(f"  {event.value:<28} {value}")

    print("\n--- ACCEPTANCE CHECKLIST ---")
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

    failed = [label for label, ok in checks if not ok]
    print("\nRESULT:", "PASS" if not failed else "FAIL -> " + ", ".join(failed))
    return 0 if not failed else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase B Alpaca real-data acceptance test")
    parser.add_argument("--seconds", type=int, default=120, help="how long to stream data")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.seconds)))


if __name__ == "__main__":
    main()