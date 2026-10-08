"""Pipeline mode: wait for a real strategy signal, then the same close chain (Phase J)."""
from __future__ import annotations

import asyncio
import traceback
from typing import Any

from app.acceptance import direct as _direct
from app.acceptance.preflight import run_preflight
from app.acceptance.reporting import config_hash, new_report
from app.events.types import EventType
from app.runtime import build_runtime

EXIT_NO_SIGNAL = 7


async def run_pipeline(config, env, *, quantity: float, timeout: float,
                       max_orders: int = 1) -> tuple[int, dict[str, Any]]:
    """Wait for a live strategy order, then close/flatten/reconcile it."""
    report = new_report("pipeline")
    report["symbol"] = config.trading.symbol
    report["config_hash"] = config_hash(config)
    ceiling = config.live_acceptance.max_order_quantity
    report["clamped_quantity"] = min(float(quantity), float(ceiling))

    probe = build_runtime(config, env).engine
    checks = await run_preflight(config, env, probe)
    report["preflight"] = [c.as_dict() for c in checks]
    if config.trading.broker != "alpaca" or config.market_data.provider != "alpaca":
        report["preflight"].append({
            "name": "alpaca_configured", "ok": False,
            "detail": f"broker={config.trading.broker} provider={config.market_data.provider}",
            "timestamp": _direct._stamp()})
    if not all(c["ok"] for c in report["preflight"]):
        report.update(exit_code=_direct.EXIT_PREFLIGHT, outcome="preflight_failed",
                      ended_at=_direct._stamp(), error="preflight_failed")
        return _direct.EXIT_PREFLIGHT, report

    config.execution.enabled = True
    config.paper_safety.enabled = True
    runtime = build_runtime(config, env)
    engine = runtime.engine
    report["execution_enabled_during_run"] = True
    assert engine.execution_enabled is True
    broker = engine.broker
    exit_code, outcome = _direct.EXIT_ERROR, "unexpected_error"
    try:
        submitted: asyncio.Future = asyncio.get_running_loop().create_future()

        def _watch(event) -> None:
            if event.type is EventType.ORDER_SUBMITTED and not submitted.done():
                submitted.set_result(event)

        engine.bus.subscribe_all(_watch)
        await runtime.startup()
        try:
            event = await asyncio.wait_for(asyncio.shield(submitted), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            report.update(exit_code=EXIT_NO_SIGNAL, outcome="no_signal_within_timeout",
                          ended_at=_direct._stamp())
            return EXIT_NO_SIGNAL, _direct._finish(report, EXIT_NO_SIGNAL,
                                                   "no_signal_within_timeout", None)
        order_id = (event.payload or {}).get("order_id")
        orders = {o.order_id: o for o in engine.oms.all_orders()}
        order = orders.get(order_id) if order_id else None
        if order is None:
            report["error"] = f"submitted order {order_id} not found in OMS"
            return _direct.EXIT_ERROR, _direct._finish(
                report, _direct.EXIT_ERROR, "unexpected_error", None)
        report["order"].update(
            order_id=order.order_id, broker_order_id=order.broker_order_id,
            side=order.side.value, quantity=order.quantity)
        status, polls, last_poll_error = await _direct._poll_terminal(
            broker, order, timeout=30.0,
            interval=config.execution.order_sync_interval_seconds)
        report["order"].update(
            status=status, filled_quantity=order.filled_quantity,
            average_fill_price=order.average_fill_price,
            broker_order_id=order.broker_order_id,
            polls=polls, last_poll_error=last_poll_error)
        if status == "rejected":
            report["order"]["reject_reason"] = order.reject_reason
            ok = await engine.reconcile()
            report["reconciliation"] = {"ok": ok, "discrepancies": []}
            return _direct.EXIT_REJECTED, _direct._finish(
                report, _direct.EXIT_REJECTED, "order_rejected", None)
        if status == "ambiguous":
            ok = await engine.reconcile()
            report["reconciliation"] = {"ok": ok, "discrepancies": []}
            return _direct.EXIT_AMBIGUOUS, _direct._finish(
                report, _direct.EXIT_AMBIGUOUS, "ambiguous_submission", None)

        local = engine.position_manager.position
        report["position_after_fill"] = {
            "direction": None if local.is_flat else local.direction.value,
            "quantity": abs(local.quantity),
            "average_entry": local.average_entry if not local.is_flat else None,
        }
        import time as _time
        t0 = _time.monotonic()
        config.session_closeout.maximum_flatten_attempts = config.live_acceptance.flatten_max_attempts
        flat_ok = await engine.flatten(force=True, session_closeout=False)
        elapsed = _time.monotonic() - t0
        broker_zero, local_flat = await _direct._await_flat(
            broker, engine, config.trading.symbol)
        report["flatten"] = {"attempts": config.live_acceptance.flatten_max_attempts,
                             "final_broker_zero": broker_zero,
                             "final_local_flat": local_flat,
                             "elapsed_seconds": round(elapsed, 2)}
        ok = await engine.reconcile()
        report["reconciliation"] = {"ok": ok, "discrepancies": []}
        await engine.halt_experiment("pipeline_acceptance_complete", operator="acceptance")
        if broker_zero and local_flat and ok and flat_ok:
            exit_code, outcome = _direct.EXIT_PASS, "pass"
        else:
            exit_code, outcome = _direct.EXIT_FLATTEN, "flatten_failed"
        return exit_code, _direct._finish(report, exit_code, outcome, None)
    except Exception:  # noqa: BLE001
        report["error"] = traceback.format_exc()[-2000:]
        return _direct.EXIT_ERROR, _direct._finish(
            report, _direct.EXIT_ERROR, "unexpected_error", None)
    finally:
        config.execution.enabled = False
        config.paper_safety.enabled = False
        try:
            await engine.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await broker.disconnect()
        except Exception:  # noqa: BLE001
            pass
        report["execution_enabled_at_exit"] = False
