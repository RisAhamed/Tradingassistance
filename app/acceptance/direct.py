"""Direct mode: one bounded order through the live OMS funnel (Phase J)."""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from app.acceptance.preflight import run_preflight
from app.acceptance.reporting import config_hash, new_report
from app.core.clock import utcnow
from app.core.ids import new_order_id
from app.domain.enums import Direction, OrderStatus, OrderType, Side
from app.domain.orders import OrderIntent
from app.runtime import build_runtime

logger = logging.getLogger("app.acceptance.direct")

EXIT_PASS = 0
EXIT_PREFLIGHT = 2
EXIT_REJECTED = 3
EXIT_AMBIGUOUS = 4
EXIT_FLATTEN = 5
EXIT_ERROR = 6


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _poll_terminal(broker, order, *, timeout: float, interval: float) -> tuple[str, int, str | None]:
    """Poll broker.get_order until terminal. Returns (status, polls, last_error).

    Poll failures are recorded, never swallowed silently: a wall of errors
    is itself forensic evidence (rate limits, propagation delays).
    """
    deadline = time.monotonic() + timeout
    polls = 0
    last_error: str | None = None
    while time.monotonic() < deadline:
        polls += 1
        try:
            await broker.get_order(order)
            last_error = None
        except Exception as exc:  # noqa: BLE001 - transient poll failure, keep waiting
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        logger.info(
            "ORDER STATUS POLL",
            extra={"structured": {"event": "ORDER_STATUS_POLL", "component": "acceptance",
                                  "order_id": order.order_id,
                                  "broker_order_id": order.broker_order_id,
                                  "status": order.status.value,
                                  "filled_quantity": order.filled_quantity,
                                  "poll": polls, "error": last_error}},
        )
        if order.status.is_terminal:
            return order.status.value, polls, last_error
        await asyncio.sleep(max(0.05, interval))
    return "ambiguous", polls, last_error


async def _await_flat(broker, engine, symbol: str, *, timeout: float = 30.0) -> tuple[bool, bool]:
    """Poll until the broker shows no position for symbol. Returns (broker_zero, local_flat)."""
    deadline = time.monotonic() + timeout
    broker_zero = False
    while time.monotonic() < deadline:
        rest = [p for p in await broker.get_positions()
                if p.symbol.replace("/", "") == symbol.replace("/", "")]
        if not rest:
            broker_zero = True
            break
        await asyncio.sleep(1.0)
    return broker_zero, engine.position_manager.is_flat


async def _await_snapshot(engine, *, timeout: float):
    """Wait for one priced snapshot (real streams arrive on their own; the
    deterministic mock is pumped through the same handler seam)."""
    provider = engine.provider
    provider.set_handler(engine.on_market_update)
    await provider.connect([engine.symbol])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = engine.state.latest_snapshot
        if snap is not None and snap.price:
            return snap
        pump = getattr(provider, "pump_one", None)
        if callable(pump):
            try:
                await pump()
            except Exception:  # noqa: BLE001 - keep waiting for real data
                pass
        await asyncio.sleep(0.2)
    return None


async def run_direct(config, env, *, quantity: float, timeout: float,
                     max_orders: int = 1) -> tuple[int, dict[str, Any]]:
    """Execute the direct-mode lifecycle. Returns (exit_code, report)."""
    report = new_report("direct")
    report["symbol"] = config.trading.symbol
    report["config_hash"] = config_hash(config)
    ceiling = config.live_acceptance.max_order_quantity
    clamped = min(float(quantity), float(ceiling))
    report["clamped_quantity"] = clamped

    # Repository-state engine for the local preflight checks (gate closed).
    probe = build_runtime(config, env).engine
    checks = await run_preflight(config, env, probe)
    report["preflight"] = [c.as_dict() for c in checks]
    if config.trading.broker != "alpaca" or config.market_data.provider != "alpaca":
        report["preflight"].append({
            "name": "alpaca_configured", "ok": False,
            "detail": f"broker={config.trading.broker} provider={config.market_data.provider}",
            "timestamp": _stamp()})
    if not all(c["ok"] for c in report["preflight"]):
        report.update(exit_code=EXIT_PREFLIGHT, outcome="preflight_failed",
                      ended_at=_stamp(), error="preflight_failed")
        return EXIT_PREFLIGHT, report

    # In-process enablement ONLY (repository yaml untouched).
    config.execution.enabled = True
    config.paper_safety.enabled = True
    runtime = build_runtime(config, env)
    engine = runtime.engine
    report["execution_enabled_during_run"] = True
    assert engine.execution_enabled is True
    broker = engine.broker
    exit_code, outcome, error = EXIT_ERROR, "unexpected_error", None
    try:
        await broker.connect()
        account = await broker.get_account()
        starting_equity = account.equity
        report["starting_equity"] = starting_equity
        positions = await broker.get_positions()
        assert not positions, "preflight guaranteed flat broker"

        # The paper-cap notional gate needs a real price: take one live
        # snapshot through the normal market-data path first.
        snapshot = await _await_snapshot(engine, timeout=min(60.0, timeout))
        if snapshot is None:
            report["error"] = "no market snapshot for paper-cap pricing"
            return EXIT_ERROR, _finish(report, EXIT_ERROR, "unexpected_error", starting_equity)

        intent = OrderIntent(
            order_id=new_order_id(), client_order_id=new_order_id(),
            timestamp=utcnow(), symbol=config.trading.symbol,
            side=Side.BUY, direction=Direction.LONG, quantity=clamped,
            order_type=OrderType.MARKET, reason="live_acceptance_direct",
        )
        order = engine.oms.create(intent)
        submitted = await engine._submit(order)
        report["order"].update(
            order_id=order.order_id, broker_order_id=order.broker_order_id,
            side=order.side.value, quantity=order.quantity)
        if not submitted and order.status is OrderStatus.REJECTED:
            # Rejected: no fill possible, no retry, no flatten to attempt.
            report["order"].update(status="rejected", reject_reason=order.reject_reason)
            ok = await engine.reconcile()
            report["reconciliation"] = {"ok": ok,
                                        "discrepancies": engine.state.reconciliation.get("discrepancies", [])}
            report["flatten"] = {"attempts": 0, "final_broker_zero": True,
                                 "final_local_flat": True, "elapsed_seconds": 0.0}
            return EXIT_REJECTED, _finish(report, EXIT_REJECTED, "order_rejected", starting_equity)

        status, polls, last_poll_error = await _poll_terminal(
            broker, order, timeout=min(30.0, timeout),
            interval=config.execution.order_sync_interval_seconds)
        report["order"].update(
            status=status, filled_quantity=order.filled_quantity,
            average_fill_price=order.average_fill_price,
            fills_count=len(getattr(order, "fills", []) or []),
            broker_order_id=order.broker_order_id,
            polls=polls, last_poll_error=last_poll_error)
        if status == "rejected":
            report["order"]["reject_reason"] = order.reject_reason
            ok = await engine.reconcile()
            report["reconciliation"] = {"ok": ok,
                                        "discrepancies": engine.state.reconciliation.get("discrepancies", [])}
            report["flatten"] = {"attempts": 0, "final_broker_zero": True,
                                 "final_local_flat": True, "elapsed_seconds": 0.0}
            return EXIT_REJECTED, _finish(report, EXIT_REJECTED, "order_rejected", starting_equity)
        if status == "ambiguous":
            # Ambiguous submission: reconcile, NEVER resubmit. But cleanup
            # still applies — if the broker filled late, the resulting
            # exposure must be flattened through the single flatten path.
            ok = await engine.reconcile()
            report["reconciliation"] = {"ok": ok,
                                        "discrepancies": engine.state.reconciliation.get("discrepancies", [])}
            broker_positions = await broker.get_positions()
            mine = [p for p in broker_positions
                    if p.symbol.replace("/", "") == config.trading.symbol.replace("/", "")]
            if mine:
                t0 = time.monotonic()
                flat_ok = await engine.flatten(force=True, session_closeout=False)
                elapsed = time.monotonic() - t0
                broker_zero, local_flat = await _await_flat(
                    broker, engine, config.trading.symbol)
                report["flatten"] = {"attempts": config.live_acceptance.flatten_max_attempts,
                                     "final_broker_zero": broker_zero,
                                     "final_local_flat": local_flat,
                                     "elapsed_seconds": round(elapsed, 2)}
                ok2 = await engine.reconcile()
                report["reconciliation"] = {"ok": ok2,
                                            "discrepancies": engine.state.reconciliation.get("discrepancies", [])}
                report["late_fill_flattened"] = bool(broker_zero and local_flat and ok2)
            return EXIT_AMBIGUOUS, _finish(report, EXIT_AMBIGUOUS, "ambiguous_submission", starting_equity)

        # FILLED: read both positions, then close via the single flatten path.
        broker_positions = await broker.get_positions()
        mine = [p for p in broker_positions
                if p.symbol.replace("/", "") == config.trading.symbol.replace("/", "")]
        local = engine.position_manager.position
        report["position_after_fill"] = {
            "direction": None if local.is_flat else local.direction.value,
            "quantity": abs(local.quantity),
            "average_entry": local.average_entry if not local.is_flat else None,
        }
        report["_broker_after_fill"] = [
            {"symbol": p.symbol, "quantity": p.quantity,
             "direction": p.direction.value} for p in mine]

        t0 = time.monotonic()
        attempts_cfg = config.live_acceptance.flatten_max_attempts
        config.session_closeout.maximum_flatten_attempts = attempts_cfg
        flat_ok = await engine.flatten(force=True, session_closeout=False)
        elapsed = time.monotonic() - t0
        broker_zero, local_flat = await _await_flat(
            broker, engine, config.trading.symbol)
        report["flatten"] = {"attempts": attempts_cfg, "final_broker_zero": broker_zero,
                             "final_local_flat": local_flat, "elapsed_seconds": round(elapsed, 2)}
        ok = await engine.reconcile()
        report["reconciliation"] = {"ok": ok,
                                    "discrepancies": engine.state.reconciliation.get("discrepancies", [])}
        if broker_zero and local_flat and ok and flat_ok:
            report["final_equity"] = await _equity(broker)
            return EXIT_PASS, _finish(report, EXIT_PASS, "pass", starting_equity)
        report["final_equity"] = await _equity(broker)
        return EXIT_FLATTEN, _finish(report, EXIT_FLATTEN, "flatten_failed", starting_equity)
    except Exception:  # noqa: BLE001 - unexpected: capture, never swallow silently
        error = traceback.format_exc()
        report["error"] = error[-2000:]
        return EXIT_ERROR, _finish(report, EXIT_ERROR, "unexpected_error", None)
    finally:
        # Restoration on EVERY path: gates closed, engine stopped, broker off.
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


async def _equity(broker) -> float | None:
    try:
        return (await broker.get_account()).equity
    except Exception:  # noqa: BLE001
        return None


def _finish(report: dict[str, Any], code: int, outcome: str, starting_equity) -> dict[str, Any]:
    report.update(exit_code=code, outcome=outcome, ended_at=_stamp())
    return report
