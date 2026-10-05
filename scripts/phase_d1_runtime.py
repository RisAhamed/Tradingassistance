"""Phase D.1 runtime capture: decision trace, TradePlan lifecycle, persistence.

Drives the FULL pipeline with the deterministic mock provider. Execution stays
DISABLED for the whole run. Prints:
  * system start + execution gate state
  * the single authoritative freshness measurement
  * every decision-cycle stage (decision trace)
  * timeframe selection
  * TradePlan + lifecycle + invalidation
  * a real persistence / restart round trip (with --storage)

Usage:
    .venv/Scripts/python.exe scripts/phase_d1_runtime.py --storage
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.loader import get_env, load_config
from app.core.clock import utcnow
from app.core.logging import configure_logging, reset_logging_state
from app.decision.trade_plan import TradePlanBuilder
from app.domain.enums import Regime
from app.domain.trade_plan import TradePlanStatus
from app.runtime import build_runtime


def _config(storage_path=None):
    reset_logging_state()
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.market_data.max_future_skew_seconds = 86400 * 30
    config.trading.broker = "mock"
    config.execution.enabled = False        # EXECUTION OFF - never changed
    config.ai.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    if storage_path:
        config.storage.enabled = True
        config.storage.url = ""
        config.storage.sqlite_path = storage_path
    else:
        config.storage.enabled = False
    configure_logging(config, env)
    return config, env


def _rule(title):
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def _make_signal():
    from tests.support import make_signal
    return make_signal(timestamp=utcnow())


def _fresh_plan(config):
    from tests.support import make_features, make_regime
    return TradePlanBuilder(config.trade_plan).build_from_signal(
        _make_signal(),
        regime=make_regime(Regime.TRENDING_BULLISH, timestamp=utcnow()),
        features=make_features(timestamp=utcnow()),
    ).plan


async def _pump(engine, ticks=700):
    provider = engine.provider
    n = 0
    while not provider.exhausted and n < ticks:
        await provider.pump_one()
        n += 1
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    return n


async def run(with_storage):
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) if with_storage else None
    storage_path = tmp.name if tmp else None
    config, env = _config(storage_path)
    runtime = build_runtime(config, env)
    engine = runtime.engine
    await engine.start()
    failures = []

    _rule("1. SYSTEM START / EXECUTION GATE")
    print(json.dumps({
        "symbol": engine.symbol,
        "execution_enabled": engine.execution_enabled,
        "execution_status": engine.execution_status(),
        "storage_enabled": bool(config.storage.enabled),
    }, indent=2, default=str))

    ticks = await _pump(engine)
    print("\npumped {0} mock market updates".format(ticks))

    _rule("2. FRESHNESS - ONE AUTHORITATIVE MEASUREMENT")
    streams = engine.stream_state()
    print(json.dumps(streams, indent=2, default=str))
    bar = streams["bars"]
    policy = engine.freshness_policy.evaluate(
        source="bar", last_received=engine._last_bar_at, now=utcnow(),
        expected_interval_seconds=60,
    )
    agree = bar["threshold_seconds"] == policy.effective_threshold_seconds
    print("\nhealth/risk threshold agreement: {0}".format("OK" if agree else "MISMATCH"))
    if not agree:
        failures.append("stream_state threshold != risk policy threshold")

    _rule("3. DECISION TRACE (last cycle)")
    for entry in engine.decision_trace():
        print("[{0:>7}] {1:<20} decision={2:<14} reason={3}".format(
            entry["status"], entry["stage"], entry["decision"],
            str(entry["reason"])[:50]))

    _rule("4. TIMEFRAME SELECTION")
    print(json.dumps(engine.payload().get("timeframe_selection"), indent=2, default=str))

    _rule("5. TRADEPLAN")
    plan = engine._trade_plan
    if plan is None:
        print("no TradePlan produced")
        failures.append("no TradePlan produced by the pipeline")
    else:
        print(json.dumps(plan.model_dump(mode="json"), indent=2, default=str))

    _rule("6. TRADEPLAN LIFECYCLE + INVALIDATION")
    if plan is not None:
        from tests.support import make_regime
        engine._trade_plan = _fresh_plan(config)
        engine._last_bar_at = utcnow()
        engine.state.regime = make_regime(Regime.RANGE_BOUND, timestamp=utcnow())
        engine._timeframe_selection = None
        reason = engine.trade_plan_invalidity(engine._trade_plan, utcnow())
        print("invalidity reason:", reason)
        if reason != "REGIME_CHANGED":
            failures.append("expected REGIME_CHANGED, got {0}".format(reason))
        await engine._invalidate_trade_plan_if_needed(utcnow())
        print("after invalidation:", engine._trade_plan.status.value, "|",
              engine._trade_plan.invalidation_reason)
        print("lifecycle:", json.dumps(engine._trade_plan.lifecycle_history,
                                       indent=2, default=str))
        if engine._trade_plan.status != TradePlanStatus.INVALIDATED:
            failures.append("plan was not invalidated")

    _rule("7. EXECUTION GATE - MUST STAY CLOSED")
    print(json.dumps({
        "execution_enabled": engine.execution_enabled,
        "orders": len(engine.oms.all_orders()),
        "position_flat": engine.position_manager.is_flat,
        "readiness": engine.readiness(),
    }, indent=2, default=str))
    if engine.execution_enabled:
        failures.append("EXECUTION WAS ENABLED - safety violation")
    if engine.oms.all_orders():
        failures.append("orders were created - safety violation")

    storage_ok = bool(with_storage and engine.repository
                      and engine.repository.available)
    saved_plan = None
    if storage_ok:
        _rule("8. PERSISTENCE / RESTART ROUND TRIP")
        saved_plan = _fresh_plan(config)
        ok = await engine.repository.save_trade_plan(
            saved_plan.model_dump(mode="json"), session_id="ses_d1")
        print("saved:", ok, saved_plan.plan_id, saved_plan.status.value)
        if not ok:
            failures.append("save_trade_plan returned False")
    await engine.stop()

    if storage_ok:
        config2, env2 = _config(storage_path)
        runtime2 = build_runtime(config2, env2)
        engine2 = runtime2.engine
        await engine2.start()
        try:
            loaded = await engine2.repository.load_trade_plan(saved_plan.plan_id)
            keys = ("plan_id", "status", "direction", "stop_price", "position_quantity")
            print("payload reloaded after restart:",
                  json.dumps({k: (loaded or {}).get(k) for k in keys},
                             indent=2, default=str))
            # read the actual DB columns (payload keys differ from column names)
            async with engine2.repository._session_factory() as sess:
                from sqlalchemy import select
                from app.storage.models import TradePlanRow
                row = (await sess.execute(
                    select(TradePlanRow).where(TradePlanRow.plan_id == saved_plan.plan_id)
                )).scalar_one_or_none()
                cols = {c.name: getattr(row, c.name) for c in TradePlanRow.__table__.columns}
                print("DB columns after restart:")
                print(json.dumps({k: cols[k] for k in (
                    "plan_id", "symbol", "direction", "status", "entry_reference",
                    "stop_reference", "target_reference", "quantity", "risk_amount",
                    "regime", "strategy_name", "invalidation_reason",
                    "expected_holding_minutes", "maximum_holding_minutes")},
                    indent=2, default=str))
                if row is None:
                    failures.append("TradePlan row missing after restart")
                else:
                    if cols["stop_reference"] != saved_plan.stop_price:
                        failures.append("stop_reference column NULL/mismatched")
                    if cols["quantity"] != saved_plan.position_quantity:
                        failures.append("quantity column NULL/mismatched")
            if loaded is None:
                failures.append("TradePlan did NOT survive restart")
            elif loaded["status"] != saved_plan.status.value:
                failures.append("reloaded status mismatch")
            latest = await engine2.repository.load_latest_trade_plan()
            print("latest from storage:", (latest or {}).get("plan_id"))
            if latest is None:
                failures.append("load_latest_trade_plan returned None")
        finally:
            await engine2.stop()

    _rule("RESULT")
    print("PASS" if not failures else "FAIL -> " + "; ".join(failures))
    return 0 if not failures else 1


def main():
    parser = argparse.ArgumentParser(description="Phase D.1 runtime capture")
    parser.add_argument("--storage", action="store_true",
                        help="run the persistence/restart round trip")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.storage)))
if __name__ == "__main__":
    main()


