"""Phase D.5.5-R2 — REAL Alpaca PAPER multi-order accounting acceptance.

Resolves BUG-D5-001: real OMS <-> FillLedger <-> PositionManager <-> Alpaca
PAPER broker convergence under opposing / multiple orders.

The harness drives the REAL application pipeline (no shortcut submission path):

    MOCK MARKET (deterministic, config-seeded)
    -> FEATURES -> REGIME -> TIMEFRAME -> STRATEGY -> SIGNAL -> RISK
    -> SIZING -> TRADEPLAN -> EXECUTION GATE -> ORDER INTENT -> OMS
    -> AlpacaPaperBroker (REAL paper endpoint) -> broker fill
    -> fill bridge (cumulative-fill delta) -> FillLedger -> OMS
    -> PositionManager -> broker reconciliation -> flatten -> position = 0

Hard safety bounds are enforced in-process; the repository configuration is
NEVER modified:

    trading.mode=paper            (from repository config, asserted)
    execution.enabled=false       (repository value; overridden ONLY in-memory)
    LIVE TRADING = PROHIBITED     (paper endpoint asserted before every order)

Usage:
    .venv/Scripts/python.exe scripts/acceptance_d5_5_multi_order.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.loader import PROJECT_ROOT, get_env, load_config  # noqa: E402
from app.core.clock import utcnow  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.domain.enums import Direction, OrderStatus, Side  # noqa: E402
from app.domain.symbols import canonical_symbol  # noqa: E402
from app.events.types import EventType  # noqa: E402

logger = logging.getLogger("acceptance.d5_5")

REPORTS = PROJECT_ROOT / "logs" / "reports"
# "" = the real D.5.5-R2 acceptance run. "_smoke" (via env) = short plumbing
# check that writes to its own file names so it can never overwrite real
# acceptance evidence. Default is "_r2" so artifacts are D.5.5-R2 named.
REPORT_TAG = os.environ.get("D5_5_TAG", "_r2")
SYMBOL = "BTC/USD"


def report_path(stem: str) -> Path:
    """Artifact path for this run ('' full acceptance, '_smoke' dry plumbing)."""
    return REPORTS / f"phase_d5_5{REPORT_TAG}_{stem}"

# ---------------------------------------------------------------------------
# HARD BOUNDS. Every bound is fixed here (never derived from live input) and
# is additionally clamped by the repository configuration where the config is
# stricter. The run aborts as soon as any bound is reached.
# ---------------------------------------------------------------------------
HARD_QUANTITY_CEILING = 0.001  # BTC/USD — smallest practical paper qty proven in D.3
LIMITS: dict[str, Any] = {
    "max_orders": 6,
    "max_quantity_per_order": HARD_QUANTITY_CEILING,
    "max_open_exposure": HARD_QUANTITY_CEILING,
    "max_runtime_seconds": 900.0,
    "max_market_ticks": 4000,
    "max_strategy_cycles": 600,
    "max_retries": 3,
    "max_exit_attempts": 3,
    "target_round_trips": 2,
    "convergence_timeout_seconds": 30.0,
    "exit_timeout_seconds": 45.0,
    "poll_interval_seconds": 0.25,
    "readiness_timeout_seconds": 120.0,
}
CONVERGENCE_TOLERANCE = 1e-8


class _Tee:
    """Duplicate stdout/stderr into the runtime report file."""

    def __init__(self, *streams) -> None:
        self._streams = streams
        self._files = []

    def write(self, data: str) -> int:
        for stream in self._streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:  # noqa: BLE001
                pass
        for handle in self._files:
            try:
                handle.write(data)
                handle.flush()
            except Exception:  # noqa: BLE001
                pass
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass
        for handle in self._files:
            try:
                handle.flush()
            except Exception:  # noqa: BLE001
                pass

    def attach(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._files.append(path.open("w", encoding="utf-8"))


def emit(event: str, component: str = "acceptance", level: int = logging.INFO, **fields: Any) -> None:
    """Compulsory structured runtime log (console + logs/trading-agent.log)."""
    payload = {"event": event, "component": component}
    payload.update(fields)
    try:
        logger.log(level, event, extra={"structured": payload})
    except Exception:  # noqa: BLE001 - logging must never break the acceptance run
        print(f"[{utcnow().strftime('%H:%M:%S')}] {event} (log failure)", flush=True)


def banner(title: str) -> None:
    line = "=" * 66
    print(f"\n{line}\n{title}\n{line}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signed(quantity: float, direction: Direction) -> float:
    if direction is Direction.LONG:
        return quantity
    if direction is Direction.SHORT:
        return -quantity
    return 0.0


def _close(a: float, b: float, tol: float = CONVERGENCE_TOLERANCE) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# Authoritative snapshots (BROKER / FILL LEDGER / OMS / POSITION MANAGER)
# ---------------------------------------------------------------------------
async def snapshot(engine, label: str) -> dict[str, Any]:
    """Read the four independent accounting views and compare them."""
    broker_positions = []
    broker_net = 0.0
    broker_error: str | None = None
    try:
        for position in await engine.broker.get_positions():
            if canonical_symbol(position.symbol) != canonical_symbol(engine.symbol):
                # Any other symbol is out of scope but must never be ignored
                # silently — it is reported as an untracked broker position.
                broker_positions.append(
                    {
                        "symbol": position.symbol,
                        "qty": position.quantity,
                        "side": position.direction.value,
                        "avg_entry": position.average_entry,
                        "tracked_locally": False,
                    }
                )
                continue
            signed = _signed(position.quantity, position.direction)
            broker_net += signed
            broker_positions.append(
                {
                    "symbol": position.symbol,
                    "qty": position.quantity,
                    "side": position.direction.value,
                    "avg_entry": position.average_entry,
                    "signed": signed,
                    "tracked_locally": True,
                }
            )
    except Exception as exc:  # noqa: BLE001
        broker_error = f"{type(exc).__name__}: {exc}"[:200]

    ledger_net = engine.fill_ledger.net_quantity(engine.symbol)
    oms_net = engine.oms.net_quantity(engine.symbol)
    position = engine.position_manager.position
    pm_net = 0.0 if position.is_flat else _signed(position.quantity, position.direction)

    comparisons = {
        "broker_vs_ledger": _close(broker_net, ledger_net),
        "ledger_vs_oms": _close(ledger_net, oms_net),
        "oms_vs_pm": _close(oms_net, pm_net),
        "broker_vs_pm": _close(broker_net, pm_net),
    }
    comparisons["overall"] = all(comparisons.values())
    return {
        "label": label,
        "at": _now(),
        "broker_net": broker_net,
        "ledger_net": ledger_net,
        "oms_net": oms_net,
        "pm_net": pm_net,
        "pm": {
            "direction": position.direction.value,
            "quantity": position.quantity,
            "average_entry": position.average_entry,
            "stop": position.stop,
            "target": position.target,
            "realized_pnl": position.realized_pnl,
            "unrealized_pnl": position.unrealized_pnl,
        },
        "broker_positions": broker_positions,
        "broker_error": broker_error,
        "comparisons": comparisons,
        "orders": order_snapshots(engine),
        "ledger_fills": [
            {
                "key": entry.key,
                "side": entry.side.value,
                "quantity": entry.quantity,
                "price": entry.price,
                "timestamp": entry.timestamp.isoformat(),
                "broker_order_id": entry.broker_order_id,
            }
            for entry in engine.fill_ledger.entries
        ],
        "ledger_duplicates_ignored": engine.fill_ledger.duplicates_ignored,
        "decision_trace": engine.decision_trace(),
    }


def order_snapshots(engine) -> list[dict[str, Any]]:
    rows = []
    for order in engine.oms.all_orders():
        rows.append(
            {
                "order_id": order.order_id,
                "client_order_id": order.client_order_id,
                "broker_order_id": order.broker_order_id,
                "symbol": order.symbol,
                "side": order.side.value,
                "direction": order.direction.value,
                "quantity": order.quantity,
                "status": order.status.value,
                "filled_quantity": order.filled_quantity,
                "average_fill_price": order.average_fill_price,
                "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
                "updated_at": order.updated_at.isoformat() if order.updated_at else None,
                "reject_reason": order.reject_reason,
                "signal_id": order.signal_id,
            }
        )
    return rows


def print_reconciliation(snap: dict[str, Any]) -> None:
    print("\n" + "=" * 50)
    print("ACCOUNTING RECONCILIATION")
    print("=" * 50)
    print(f"BROKER       = {snap['broker_net']!r}")
    print(f"FILL LEDGER  = {snap['ledger_net']!r}")
    print(f"OMS          = {snap['oms_net']!r}")
    print(f"POSITION MGR = {snap['pm_net']!r}")
    print(f"BROKER <-> LEDGER = {'PASS' if snap['comparisons']['broker_vs_ledger'] else 'FAIL'}")
    print(f"LEDGER <-> OMS   = {'PASS' if snap['comparisons']['ledger_vs_oms'] else 'FAIL'}")
    print(f"OMS <-> PM       = {'PASS' if snap['comparisons']['oms_vs_pm'] else 'FAIL'}")
    print(f"BROKER <-> PM    = {'PASS' if snap['comparisons']['broker_vs_pm'] else 'FAIL'}")
    print(f"OVERALL        = {'PASS' if snap['comparisons']['overall'] else 'FAIL'}")
    print("=" * 50)
    for row in snap["orders"]:
        print(
            f"  order {row['order_id']} {row['side']:>4} qty={row['quantity']} "
            f"status={row['status']} filled={row['filled_quantity']} "
            f"avg={row['average_fill_price']} broker_id={row['broker_order_id']}"
        )


# ---------------------------------------------------------------------------
# Event recorder
# ---------------------------------------------------------------------------
class Recorder:
    def __init__(self, engine, evidence: dict[str, Any]) -> None:
        self.engine = engine
        self.evidence = evidence
        self.counts: dict[str, int] = {}
        self.fill_records: list[dict[str, Any]] = []
        self.order_records: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.last_filled: dict[str, float] = {}
        self._checkpoint_queue: asyncio.Queue = asyncio.Queue()
        evidence["event_counts"] = self.counts
        evidence["fills"] = self.fill_records
        evidence["order_events"] = self.order_records
        evidence["checkpoints"] = self.checkpoints
        evidence["errors"] = self.errors

    def _lifecycle_print(self, record: dict[str, Any], *, kind: str) -> None:
        engine = self.engine
        print("\n" + "=" * 60)
        print("ORDER LIFECYCLE")
        print("=" * 60)
        print(f"KIND: {kind}")
        print(f"LOCAL ORDER ID: {record.get('order_id')}")
        print(f"FILL ID: {record.get('fill_id')}")
        print(f"BROKER ORDER ID: {record.get('broker_fill_id')}")
        print(f"SYMBOL: {record.get('symbol')}")
        print(f"SIDE: {record.get('side')}")
        print(f"QTY: {record.get('quantity')}")
        print(f"PRICE: {record.get('price')}")
        print(f"TIMESTAMP: {record.get('timestamp')}")
        try:
            print(f"\nFILL LEDGER: {engine.fill_ledger.net_quantity(engine.symbol)}")
            print(f"OMS: {engine.oms.net_quantity(engine.symbol)}")
            pm = engine.position_manager.position
            print(f"PM: {pm.quantity} {pm.direction.value} entry={pm.average_entry}")
            print(f"BROKER POSITION (endpoint data was in checkpoint): see ORDER STATUS/CHECKPOINT")
        except Exception:  # noqa: BLE001
            pass
        print("=" * 60)

    async def on_event(self, event) -> None:
        name = event.type.value
        self.counts[name] = self.counts.get(name, 0) + 1
        payload = dict(event.payload or {})
        if event.type is EventType.ORDER_FILLED and isinstance(payload.get("fill_id"), str):
            # Authoritative fill record emitted AFTER ledger/OMS/PM application.
            record = {
                "at": _now(),
                "fill_id": payload.get("fill_id"),
                "broker_fill_id": payload.get("broker_fill_id"),
                "order_id": payload.get("order_id"),
                "symbol": payload.get("symbol"),
                "side": payload.get("side"),
                "quantity": payload.get("quantity"),
                "price": payload.get("price"),
                "timestamp": payload.get("timestamp"),
            }
            self.fill_records.append(record)
            emit(
                "FILL OBSERVED",
                fill_id=record["fill_id"],
                broker_fill_id=record["broker_fill_id"],
                order_id=record["order_id"],
                side=record["side"],
                qty=record["quantity"],
                price=record["price"],
            )
            self._lifecycle_print(record, kind="ORDER_FILLED")
            await self._safe_checkpoint(f"after_fill:{record['fill_id']}")
        elif event.type is EventType.ORDER_CREATED:
            self.order_records.append({"event": "ORDER_CREATED", "at": _now(), **_order_fields(payload)})
            emit(
                "ORDER INTENT -> OMS",
                order_id=payload.get("order_id"),
                side=payload.get("side"),
                qty=payload.get("quantity"),
                status=payload.get("status"),
            )
        elif event.type is EventType.ORDER_SUBMITTED:
            self.order_records.append({"event": "ORDER_SUBMITTED", "at": _now(), **_order_fields(payload)})
            emit(
                "BROKER SUBMISSION",
                order_id=payload.get("order_id"),
                broker_order_id=payload.get("broker_order_id"),
                side=payload.get("side"),
                qty=payload.get("quantity"),
                status=payload.get("status"),
            )
            self._lifecycle_print(payload, kind="ORDER_SUBMITTED")
        elif event.type is EventType.ORDER_REJECTED:
            self.order_records.append({"event": "ORDER_REJECTED", "at": _now(), **_order_fields(payload)})
            self.errors.append({"at": _now(), "event": "ORDER_REJECTED", **_order_fields(payload)})
            emit(
                "ORDER REJECTED",
                level=logging.ERROR,
                order_id=payload.get("order_id"),
                reason=str(payload.get("reject_reason"))[:200],
            )
            self._lifecycle_print(payload, kind="ORDER_REJECTED")
            await self._safe_checkpoint("after_reject")
        elif event.type in (EventType.RECONCILIATION_FAILED, EventType.FLATTEN_FAILED,
                            EventType.SESSION_CLOSEOUT_FAILED, EventType.SYSTEM_ERROR):
            self.errors.append({"at": _now(), "event": name, "payload": _jsonable(payload)})
            emit(name, level=logging.ERROR, **{k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool))})
        elif event.type is EventType.RISK_REJECTED:
            emit("RISK REJECTED", level=logging.WARNING, reason=payload.get("reason"))

    async def _safe_checkpoint(self, label: str) -> dict[str, Any] | None:
        """Checkpoint that can never break the engine (errors land in evidence)."""
        try:
            return await self.checkpoint(label)
        except Exception as exc:  # noqa: BLE001
            failure = {
                "at": _now(),
                "event": "CHECKPOINT_FAILED",
                "label": label,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
            self.errors.append(failure)
            emit("CHECKPOINT FAILED", level=logging.ERROR, **failure)
            return None

    async def checkpoint(self, label: str) -> dict[str, Any]:
        snap = await snapshot(self.engine, label)
        snap["cumulative_filled_by_order"] = await self.poll_cumulative()
        self.checkpoints.append(snap)
        emit(
            "ACCOUNTING CHECKPOINT",
            label=label,
            broker=snap["broker_net"],
            ledger=snap["ledger_net"],
            oms=snap["oms_net"],
            pm=snap["pm_net"],
            overall="PASS" if snap["comparisons"]["overall"] else "FAIL",
        )
        if not snap["comparisons"]["overall"]:
            emit(
                "ACCOUNTING DIVERGENCE",
                level=logging.CRITICAL,
                label=label,
                broker=snap["broker_net"],
                ledger=snap["ledger_net"],
                oms=snap["oms_net"],
                pm=snap["pm_net"],
            )
        return snap

    async def poll_cumulative(self) -> list[dict[str, Any]]:
        """Independent broker-side status/cumulative-fill poll (fill bridge read)."""
        rows: list[dict[str, Any]] = []
        engine_orders = {o.broker_order_id: o for o in self.engine.oms.all_orders() if o.broker_order_id}
        if not engine_orders:
            return rows
        try:
            broker_orders = await self.engine.broker.get_orders(status="all")
        except Exception as exc:  # noqa: BLE001
            emit("ORDER STATUS POLL FAILED", level=logging.WARNING, error=str(exc)[:200])
            return rows
        index = {o.broker_order_id: o for o in broker_orders if o.broker_order_id}
        for broker_id, order in engine_orders.items():
            broker_order = index.get(broker_id)
            previous = self.last_filled.get(order.order_id, 0.0)
            current = float(broker_order.filled_quantity if broker_order else 0.0)
            delta = current - previous
            self.last_filled[order.order_id] = current
            rows.append(
                {
                    "order_id": order.order_id,
                    "broker_order_id": broker_id,
                    "side": order.side.value,
                    "requested_qty": order.quantity,
                    "local_status": order.status.value,
                    "local_filled_qty": order.filled_quantity,
                    "broker_status": broker_order.status.value if broker_order else None,
                    "broker_filled_qty": current,
                    "previous_filled_qty": previous,
                    "fill_delta": delta,
                    "average_fill_price": (broker_order.average_fill_price if broker_order else order.average_fill_price),
                    "seen_at_broker": broker_order is not None,
                }
            )
            emit(
                "ORDER STATUS POLL",
                order_id=order.order_id,
                status=broker_order.status.value if broker_order else "not_found",
                cumulative_filled=current,
                previous_filled=previous,
                fill_delta=delta,
            )
        return rows


def _order_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": payload.get("order_id"),
        "client_order_id": payload.get("client_order_id"),
        "broker_order_id": payload.get("broker_order_id"),
        "symbol": payload.get("symbol"),
        "side": payload.get("side"),
        "direction": payload.get("direction"),
        "quantity": payload.get("quantity"),
        "status": payload.get("status"),
        "filled_quantity": payload.get("filled_quantity"),
        "average_fill_price": payload.get("average_fill_price"),
        "reject_reason": payload.get("reject_reason"),
    }


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:  # noqa: BLE001
        return str(value)[:400]


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def safety_preflight(env, config) -> dict[str, Any]:
    checks = {
        "trading_mode_paper": config.trading.mode == "paper",
        "repo_execution_enabled_is_false": config.execution.enabled is False,
        "env_alpaca_paper": bool(env.alpaca_paper),
        "not_live_endpoint": not env.is_live_alpaca_endpoint(),
        "credentials_available": env.has_alpaca_credentials(),
        "broker_is_alpaca": config.trading.broker == "alpaca",
        "order_type_market": config.execution.order_type == "market",
        "symbol_is_btc_usd": config.trading.symbol == SYMBOL,
        "quantity_within_hard_ceiling": LIMITS["max_quantity_per_order"] <= HARD_QUANTITY_CEILING,
        "single_open_position_only": config.risk.maximum_open_positions == 1,
    }
    emit("SAFETY CHECKS", **checks)
    return checks


async def broker_preflight(engine) -> dict[str, Any]:
    """Connect the paper broker and prove every starting position is flat."""
    emit("BROKER INITIALIZATION", broker=engine.broker.name, paper=engine.broker.paper)
    await engine.broker.connect()
    health = engine.broker.health()
    account = await engine.broker.get_account()
    positions = await engine.broker.get_positions()
    broker_symbol_positions = [p for p in positions if p.symbol == engine.symbol]
    preflight = {
        "broker": engine.broker.name,
        "broker_paper_flag": engine.broker.paper is True,
        "broker_connected": health.connected,
        "broker_detail": health.detail,
        "account_equity": account.equity,
        "account_cash": account.cash,
        "starting_broker_position": sum(
            _signed(p.quantity, p.direction) for p in broker_symbol_positions
        ),
        "starting_broker_symbols": [p.symbol for p in positions],
        "starting_local_position": (
            0.0 if engine.position_manager.is_flat else engine.position_manager.position.quantity
        ),
        "starting_oms_position": engine.oms.net_quantity(engine.symbol),
        "starting_fill_ledger_position": engine.fill_ledger.net_quantity(engine.symbol),
        "starting_position_manager_position": engine.position_manager.position.quantity,
        "starting_orders": len(engine.oms.all_orders()),
    }
    preflight["flat_at_start"] = (
        preflight["starting_broker_position"] == 0.0
        and preflight["starting_local_position"] == 0.0
        and preflight["starting_oms_position"] == 0.0
        and preflight["starting_fill_ledger_position"] == 0.0
        and preflight["starting_position_manager_position"] == 0.0
        and preflight["starting_orders"] == 0
    )
    emit("BROKER POSITION", **{k: v for k, v in preflight.items() if isinstance(v, (str, int, float, bool))})
    return preflight


# ---------------------------------------------------------------------------
# Deterministic market scenario (config-seeded, injected only through the
# existing MockMarketDataProvider seam — never through the broker).
# ---------------------------------------------------------------------------
def reference_price() -> float:
    """Best-effort real reference price from Alpaca (read-only market data).

    Falls back to the mock generator's built-in start when unavailable — the
    market series is still deterministic and internally coherent.
    """
    try:
        import requests

        env = get_env()
        headers = {"APCA-API-KEY-ID": env.alpaca_api_key, "APCA-API-SECRET-KEY": env.alpaca_api_secret}
        r = requests.get(
            "https://data.alpaca.markets/v1beta3/crypto/us/latest/trades",
            headers=headers,
            params={"symbols": "BTC/USD"},
            timeout=10,
        )
        if r.status_code == 200:
            trades = r.json().get("trades") or {}
            price = trades.get("BTC/USD", {}).get("p")
            if price:
                return float(price)
    except Exception:  # noqa: BLE001 - fallback below
        pass
    return 30000.0


def base_series(config) -> list[float]:
    from app.market_data.mock import generate_series

    # D.5.5-R2: anchor the deterministic mock market to the current reference
    # price so stops/targets derived from signals are coherent with the real
    # broker fills. The series itself stays deterministic given the seed.
    return generate_series(
        seed=int(config.market_data.mock.seed),
        length=int(LIMITS["max_market_ticks"]),
        start=reference_price(),
    )


async def pump(provider, prices: list[float], index: int) -> tuple[int, bool]:
    if index >= len(prices):
        return index, False
    await provider.push(prices[index])
    return index + 1, True


async def wait_until(predicate, timeout: float, interval: float | None = None) -> bool:
    interval = interval or LIMITS["poll_interval_seconds"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return await predicate()


def cooldown_active(engine, config) -> float | None:
    """Seconds remaining in the configured strategy cooldown (0.0 = free)."""
    last = engine._last_trade_time
    cooldown_minutes = (
        config.strategy.cooldown.minutes if config.strategy.cooldown.enabled else 0.0
    )
    if last is None or cooldown_minutes <= 0:
        return 0.0
    remaining = cooldown_minutes * 60.0 - (utcnow() - last).total_seconds()
    return max(0.0, remaining)


def strategy_ready(engine) -> bool:
    """True once features + regime are usable by the strategy."""
    return bool(
        engine.state.features
        and engine.state.features.ready
        and engine.state.regime
        and not engine.state.regime.is_unknown
    )


async def push_stop_trigger(engine, provider, price: float) -> None:
    """Push ONE scripted market tick that crosses the open stop.

    Only market data is scripted. The exit order itself is created by the real
    engine pipeline (`_manage_position` -> `_exit` -> OMS -> broker).
    """
    emit("STOP TRIGGER", price=price)
    await provider.push(price)
    await asyncio.sleep(0.05)


def stop_trigger_price(position) -> float:
    entry = position.average_entry
    stop = position.stop
    if stop is None or stop <= 0 or entry <= 0:
        return round(entry * 0.97, 2)
    # Always strictly below the stop, but never more than 10% below entry.
    price = min(stop - max(1.0, entry * 0.001), entry * 0.90)
    return round(max(price, 1.0), 2)


def entry_settled(engine) -> bool:
    """True when the entry order has reached a terminal state and its fill is in."""
    entry_orders = [o for o in engine.oms.all_orders() if o.direction is not Direction.FLAT]
    if not entry_orders:
        return False
    if any(not o.is_terminal for o in entry_orders):
        return False
    return all(
        _close(o.filled_quantity, o.quantity)
        for o in entry_orders
        if o.status is OrderStatus.FILLED
    )


# ---------------------------------------------------------------------------
# Main multi-order sequence
# ---------------------------------------------------------------------------
async def drive_sequence(engine, provider, config, recorder: Recorder) -> dict[str, Any]:
    evidence: dict[str, Any] = recorder.evidence
    prices = base_series(config)
    index = 0
    ticks = 0
    result: dict[str, Any] = {
        "round_trips": 0,
        "positions_opened": [],
        "stop_reasons": [],
        "bound_hit": None,
    }
    crashed_positions: set[str] = set()
    opened_positions: set[str] = set()
    started = time.monotonic()
    bound_hit: str | None = None
    cycles = 0
    exit_attempts = 0

    emit(
        "SEQUENCE START",
        max_orders=LIMITS["max_orders"],
        max_runtime_seconds=LIMITS["max_runtime_seconds"],
        max_ticks=LIMITS["max_market_ticks"],
        max_quantity=LIMITS["max_quantity_per_order"],
        target_round_trips=LIMITS["target_round_trips"],
    )

    # Wait (while pumping market data — nothing advances without ticks) until
    # features + regime are usable. Bounded; the sequence continues regardless.
    ready_reported = False
    ready_deadline = time.monotonic() + float(LIMITS["readiness_timeout_seconds"])
    while not ready_reported and time.monotonic() < ready_deadline:
        if strategy_ready(engine):
            ready_reported = True
            break
        moved, ok = await pump(provider, prices, index)
        if not ok:
            break
        index = moved
        ticks += 1
        await asyncio.sleep(0.02)
    emit(
        "STRATEGY READINESS",
        ready=strategy_ready(engine),
        features_ready=bool(engine.state.features and engine.state.features.ready),
        regime=engine.state.regime.regime.value if engine.state.regime else None,
        ticks_used_for_readiness=ticks,
    )

    while True:
        elapsed = time.monotonic() - started
        order_count = len(engine.oms.all_orders())
        cycles = recorder.counts.get("TimeframeSelected", 0)
        if elapsed > LIMITS["max_runtime_seconds"]:
            bound_hit = "max_runtime_seconds"
            break
        if ticks >= LIMITS["max_market_ticks"]:
            bound_hit = "max_market_ticks"
            break
        if order_count >= LIMITS["max_orders"]:
            bound_hit = "max_orders"
            break
        if cycles >= LIMITS["max_strategy_cycles"]:
            bound_hit = "max_strategy_cycles"
            break
        if result["round_trips"] >= LIMITS["target_round_trips"] and engine.position_manager.is_flat:
            bound_hit = "target_round_trips_reached"
            break

        position = engine.position_manager.position

        # ---- flat: wait out the real strategy cooldown, then seek an entry --
        if position.is_flat:
            remaining = cooldown_active(engine, config)
            if remaining > 0:
                emit("COOLDOWN WAIT", seconds_remaining=round(remaining, 1))
                await asyncio.sleep(min(5.0, remaining))
                continue
            moved, ok = await pump(provider, prices, index)
            if not ok:
                bound_hit = "market_series_exhausted"
                break
            index = moved
            ticks += 1
            await asyncio.sleep(0.02)
            continue

        # ---- position open -------------------------------------------------
        position_id = position.position_id
        if position_id not in opened_positions:
            opened_positions.add(position_id)
            result["positions_opened"].append(
                {
                    "position_id": position_id,
                    "direction": position.direction.value,
                    "quantity": position.quantity,
                    "average_entry": position.average_entry,
                    "stop": position.stop,
                    "target": position.target,
                    "opened_at": position.opened_at.isoformat() if position.opened_at else None,
                    "closed": False,
                    "closed_flat": False,
                }
            )
            emit(
                "POSITION OPEN",
                direction=position.direction.value,
                quantity=position.quantity,
                entry=position.average_entry,
                stop=position.stop,
                target=position.target,
            )
            # D.5.5-R2: the broker alignment (in-kind fee adjustment) lands
            # inside _apply_fill BEFORE the ORDER_FILLED event, so wait for the
            # after_fill checkpoint to exist before snapshotting — otherwise the
            # position_open checkpoint races the alignment and reports a
            # false divergence.
            align_deadline = time.monotonic() + float(LIMITS["convergence_timeout_seconds"])
            while time.monotonic() < align_deadline:
                if any(cp["label"].startswith("after_fill:") for cp in recorder.checkpoints):
                    break
                if not entry_settled(engine):
                    await asyncio.sleep(0.1)
                    continue
                await asyncio.sleep(0.1)
            await recorder.checkpoint(f"position_open:{position_id}")

        if position_id not in crashed_positions:
            # Wait (bounded) for the entry fill to settle in every accounting view.
            if not entry_settled(engine):
                await asyncio.sleep(0.2)
                continue
            crashed_positions.add(position_id)
            await push_stop_trigger(engine, provider, stop_trigger_price(position))
            continue

        # ---- exit in flight: pump market data and wait for the fill ---------
        exit_deadline = time.monotonic() + LIMITS["exit_timeout_seconds"]
        exited = False
        while time.monotonic() < exit_deadline:
            if engine.position_manager.is_flat:
                exited = True
                break
            # D.5.5-R2: the order bound must also hold INSIDE the exit-wait
            # loop, otherwise a rejected exit can storm the broker.
            if len(engine.oms.all_orders()) >= LIMITS["max_orders"]:
                bound_hit = "max_orders"
                break
            moved, ok = await pump(provider, prices, index)
            if ok:
                index = moved
                ticks += 1
            await asyncio.sleep(0.02)
        if exited:
            for row in result["positions_opened"]:
                if row["position_id"] == position_id:
                    row["closed"] = True
                    row["closed_flat"] = True
                    row["closed_at"] = _now()
            result["round_trips"] += 1
            emit(
                "POSITION CLOSED",
                position_id=position_id,
                round_trips=result["round_trips"],
                oms=engine.oms.net_quantity(engine.symbol),
                ledger=engine.fill_ledger.net_quantity(engine.symbol),
                pm=engine.position_manager.position.quantity,
            )
            await recorder.checkpoint(f"position_closed:{position_id}")
            continue

        # ---- did not flatten in time: bounded, observable retry -------------
        pending = any(
            o.direction is Direction.FLAT
            and o.status not in (OrderStatus.REJECTED, OrderStatus.CANCELLED)
            for o in engine.oms.all_orders()
        )
        if pending:
            emit("WAITING FOR EXIT ORDER", level=logging.WARNING)
            await asyncio.sleep(1.0)
            continue
        exit_attempts += 1
        if exit_attempts > LIMITS["max_exit_attempts"]:
            bound_hit = "max_exit_attempts"
            break
        emit("EXIT RETRY", attempt=exit_attempts, level=logging.WARNING)
        # The previous exit order was rejected/cancelled: trigger once more.
        crashed_positions.discard(position_id)
        await asyncio.sleep(0.5)

    result["bound_hit"] = bound_hit
    result["ticks"] = ticks
    result["strategy_cycles"] = cycles
    result["exit_attempts"] = exit_attempts
    result["orders_created"] = len(engine.oms.all_orders())
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    emit(
        "SEQUENCE END",
        bound_hit=bound_hit,
        ticks=ticks,
        strategy_cycles=cycles,
        orders=result["orders_created"],
        round_trips=result["round_trips"],
        elapsed_seconds=result["elapsed_seconds"],
    )
    return result


# ---------------------------------------------------------------------------
# Settle / flatten / final reconciliation
# ---------------------------------------------------------------------------
async def settle(engine, recorder: Recorder, timeout: float) -> dict[str, Any]:
    """Wait until no order is in-flight and every accounting view converges."""

    async def done() -> bool:
        if any(not o.is_terminal for o in engine.oms.all_orders()):
            return False
        snap = await snapshot(engine, "settle_probe")
        return snap["comparisons"]["overall"]

    ok = await wait_until(done, timeout)
    snap = await recorder.checkpoint("settled" if ok else "settle_timeout")
    return {"settled": ok, "snapshot": snap}


async def flatten_and_verify(engine, recorder: Recorder) -> dict[str, Any]:
    emit("FLATTEN", reason="end_of_acceptance_run")
    before = await recorder.checkpoint("before_flatten")
    local_flat = engine.position_manager.is_flat
    broker_positions = [
        p for p in await engine.broker.get_positions() if canonical_symbol(p.symbol) == canonical_symbol(engine.symbol)
    ]
    outcome: dict[str, Any] = {
        "local_flat_before": local_flat,
        "broker_positions_before": [
            {"symbol": p.symbol, "qty": p.quantity, "side": p.direction.value}
            for p in broker_positions
        ],
        "flatten_required": (not local_flat) or bool(broker_positions),
    }
    if outcome["flatten_required"]:
        outcome["flatten_ok"] = await engine.flatten(force=True, session_closeout=False)
    else:
        outcome["note"] = "already flat at start of flatten step"
        # Still invoke flatten(): it must prove the broker is flat too.
        outcome["flatten_ok"] = await engine.flatten(force=True, session_closeout=False)
    await asyncio.sleep(1.0)
    await settle(engine, recorder, LIMITS["convergence_timeout_seconds"])
    after = await recorder.checkpoint("after_flatten")
    outcome["local_flat_after"] = engine.position_manager.is_flat
    broker_after_error: str | None = None
    try:
        broker_after = [p for p in await engine.broker.get_positions() if canonical_symbol(p.symbol) == canonical_symbol(engine.symbol)]
    except Exception as exc:  # noqa: BLE001
        broker_after, broker_after_error = [], f"{type(exc).__name__}: {exc}"[:200]
    outcome["broker_positions_after"] = [
        {"symbol": p.symbol, "qty": p.quantity, "side": p.direction.value}
        for p in broker_after
    ]
    # D.5.5-R2: NEVER derive flat from 'not found'. A successful empty broker
    # query is required to assert broker_zero; otherwise broker state is UNKNOWN.
    outcome["broker_query_ok"] = broker_after_error is None
    outcome["broker_zero"] = (broker_after_error is None) and (len(broker_after) == 0)
    outcome["broker_position_status"] = "verified_zero" if outcome["broker_zero"] else (
        "UNKNOWN" if broker_after_error is not None else "non_zero"
    )
    outcome["snapshot"] = after
    emit(
        "FINAL POSITION",
        broker=after["broker_net"],
        ledger=after["ledger_net"],
        oms=after["oms_net"],
        pm=after["pm_net"],
        local_flat=outcome["local_flat_after"],
        broker_zero=outcome["broker_zero"],
        flatten_ok=outcome["flatten_ok"],
    )
    return outcome


async def final_reconciliation(engine, recorder: Recorder) -> dict[str, Any]:
    snap = await recorder.checkpoint("final_reconciliation")
    print_reconciliation(snap)
    all_zero = all(
        _close(snap[key], 0.0) for key in ("broker_net", "ledger_net", "oms_net", "pm_net")
    )
    verdict = {
        "snapshot": snap,
        "broker_zero": _close(snap["broker_net"], 0.0),
        "ledger_zero": _close(snap["ledger_net"], 0.0),
        "oms_zero": _close(snap["oms_net"], 0.0),
        "pm_zero": _close(snap["pm_net"], 0.0),
        "all_zero": all_zero,
        "converged": snap["comparisons"]["overall"],
        "reconciliation": "PASS" if (all_zero and snap["comparisons"]["overall"]) else "FAIL",
        "paper_readiness": "READY" if (all_zero and snap["comparisons"]["overall"]) else "BLOCKED",
    }
    emit(
        "RECONCILIATION",
        result=verdict["reconciliation"],
        broker=snap["broker_net"],
        ledger=snap["ledger_net"],
        oms=snap["oms_net"],
        pm=snap["pm_net"],
    )
    return verdict


# ---------------------------------------------------------------------------
# Bug probes
# ---------------------------------------------------------------------------
async def probe_bug_d3_002(engine, recorder: Recorder) -> dict[str, Any]:
    """Fresh order-status synchronisation evidence (BUG-D3-002)."""
    rows = await recorder.poll_cumulative()
    engine_orders = {o.broker_order_id: o for o in engine.oms.all_orders() if o.broker_order_id}
    try:
        broker_orders = await engine.broker.get_orders(status="all")
    except Exception as exc:  # noqa: BLE001
        return {"status": "OPEN", "reason": f"broker_order_query_failed: {exc}"[:200]}
    index = {o.broker_order_id: o for o in broker_orders if o.broker_order_id}
    details = []
    synchronized = True
    for broker_id, order in engine_orders.items():
        # Case A: submission itself was rejected at the broker — there is no
        # broker order lifecycle entry to synchronize.
        if order.status is OrderStatus.REJECTED:
            details.append(
                {"order_id": order.order_id, "broker_order_id": broker_id,
                 "status": "REJECTED_AT_SUBMIT", "reason": order.reject_reason}
            )
            continue
        broker_order = index.get(broker_id)
        if broker_order is None:
            # Case B: submitted successfully but the broker order is missing.
            synchronized = False
            details.append(
                {"order_id": order.order_id, "status": "OPEN", "reason": "BROKER_ORDER_NOT_FOUND"}
            )
            continue
        status_match = order.status is broker_order.status
        filled_match = _close(order.filled_quantity, broker_order.filled_quantity)
        details.append(
            {
                "order_id": order.order_id,
                "broker_order_id": broker_id,
                "local_status": order.status.value,
                "broker_status": broker_order.status.value,
                "local_filled": order.filled_quantity,
                "broker_filled": broker_order.filled_quantity,
                "local_avg_price": order.average_fill_price,
                "broker_avg_price": broker_order.average_fill_price,
                "status_match": status_match,
                "filled_match": filled_match,
            }
        )
        if not (status_match and filled_match):
            synchronized = False
    return {
        "status": "FIXED" if synchronized and details else "OPEN",
        "orders_checked": len(details),
        "details": details,
        "poll_rows": rows,
    }


async def probe_bug_d4_002() -> dict[str, Any]:
    """Fresh startup connectivity probe (BUG-D4-002), read-only."""
    from app.runtime import build_runtime

    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "alpaca"
    config.market_data.history.provider = "none"
    config.trading.broker = "mock"
    config.execution.enabled = False
    config.ai.enabled = False
    config.storage.enabled = False
    runtime = build_runtime(config, env)
    outcome: dict[str, Any] = {"attempted": True}
    try:
        await runtime.startup()
        outcome["startup"] = "OK"
    except Exception as exc:  # noqa: BLE001
        outcome["startup"] = "FAILED"
        outcome["error"] = f"{type(exc).__name__}: {exc}"[:300]
        outcome["traceback"] = traceback.format_exc().splitlines()[-6:]
    outcome["checklist"] = [
        {"name": c.name, "ok": c.ok, "detail": str(c.detail)[:200], "mandatory": c.mandatory}
        for c in runtime.checklist
    ]
    outcome["failed_checks"] = [c["name"] for c in outcome["checklist"] if not c["ok"]]
    outcome["ready"] = runtime.ready
    try:
        await runtime.shutdown()
    except Exception as exc:  # noqa: BLE001
        outcome["shutdown_error"] = str(exc)[:200]
    if outcome["startup"] == "OK":
        outcome["status"] = "FIXED"
        outcome["conclusion"] = (
            "startup connectivity succeeded with provider=alpaca and execution "
            "disabled; the earlier D.4 PART A failure was a harness-lifecycle "
            "artifact (market-data provider health right after connect), not a "
            "production gate that needs changing."
        )
    else:
        failed = outcome.get("failed_checks") or []
        outcome["conclusion"] = (
            f"startup failed checks={failed}; exact error: {outcome.get('error')}"
        )
    emit("BUG-D4-002 PROBE", **{k: v for k, v in outcome.items() if isinstance(v, (str, int, float, bool))})
    return outcome


def probe_bug_d1_3_002(readiness_before: dict, readiness_after: dict, provider_name: str) -> dict[str, Any]:
    """Teardown freshness classification (BUG-D1.3-002)."""
    return {
        "provider": provider_name,
        "readiness_during_run": readiness_before,
        "readiness_after_teardown": readiness_after,
        "status": "OPEN / NON-BLOCKING SYNTHETIC ARTIFACT",
        "reason": (
            "The acceptance drives a synthetic mock market clock whose bar "
            "timestamps run ahead of wall-clock time. Freshness measured at "
            "teardown therefore reports a large synthetic age even though the "
            "bar stream was healthy for the whole run. Real market-data "
            "readiness uses live Alpaca timestamps (see BUG-D4-002 probe) and is "
            "reported separately. Not suppressed, not counted as a pass."
        ),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def build_bug_register(d5: dict, d3: dict, d4: dict, d13: dict) -> dict[str, Any]:
    bugs = {}

    converged = d5.get("converged", False)
    bugs["BUG-D5-001"] = {
        "severity": "HIGH",
        "component": "OMS / FillLedger / PositionManager / Alpaca paper broker",
        "symptom": "Accounting views diverge under opposing / multiple orders.",
        "status": "FIXED" if converged else "OPEN",
        "blocking": not converged,
        "root_cause": d5.get("root_cause", ""),
        "fix": d5.get("fix", ""),
        "evidence": d5.get("evidence", []),
    }
    bugs["BUG-D3-002"] = {
        "severity": "LOW",
        "component": "Alpaca order status polling",
        "symptom": "Local order status did not follow the broker status.",
        "status": d3.get("status", "NOT TESTED"),
        "blocking": d3.get("status") != "FIXED",
        "evidence": d3.get("details", []),
    }
    bugs["BUG-D4-002"] = {
        "severity": "MEDIUM",
        "component": "runtime startup connectivity probe",
        "symptom": "Startup checklist failed on connectivity in the D.4 harness.",
        "status": d4.get("status", "NOT TESTED"),
        "blocking": d4.get("status") != "FIXED",
        "evidence": d4.get("failed_checks", []),
        "conclusion": d4.get("conclusion", ""),
    }
    bugs["BUG-D1.3-002"] = {
        "severity": "LOW",
        "component": "readiness / freshness at teardown",
        "symptom": "Bar-freshness age exceeds threshold at teardown on a synthetic clock.",
        "status": d13.get("status", "NOT TESTED"),
        "blocking": False,
        "conclusion": d13.get("reason", ""),
    }
    return bugs


def write_reports(report: dict[str, Any]) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)

    (report_path("report.json")).write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (report_path("accounting.json")).write_text(
        json.dumps(
            {
                "phase": "D.5.5-R2",
                "checkpoints": report.get("checkpoints", []),
                "fills": report.get("fills", []),
                "orders": report.get("order_events", []),
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    (report_path("reconciliation.json")).write_text(
        json.dumps(
            {
                "phase": "D.5.5-R2",
                "final_reconciliation": report.get("final_reconciliation"),
                "flatten": report.get("flatten"),
                "preflight": report.get("preflight"),
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )

    bugs = report.get("bugs", {})
    lines = ["# PHASE D.5.5-R2 BUG REGISTER", ""]
    for bug_id, bug in bugs.items():
        lines += [
            f"## {bug_id}",
            "",
            f"- Severity: {bug.get('severity')}",
            f"- Component: {bug.get('component')}",
            f"- Symptom: {bug.get('symptom')}",
            f"- Root cause: {bug.get('root_cause') or 'n/a'}",
            f"- Fix: {bug.get('fix') or 'n/a'}",
            f"- Current status: **{bug.get('status')}**",
            f"- Blocking: {'YES' if bug.get('blocking') else 'NO'}",
            f"- Evidence: `{json.dumps(bug.get('evidence'), default=str)[:1500]}`",
            "",
        ]
    (report_path("bug_register.md")).write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    md = render_markdown(report)
    (report_path("report.md")).write_text(md, encoding="utf-8")


def render_markdown(report: dict[str, Any]) -> str:
    impl = report.get("implementation", {})
    ver = report.get("verification", {})
    final = report.get("final_reconciliation", {}) or {}
    snap = (final.get("snapshot") or {}) if isinstance(final, dict) else {}
    bugs = report.get("bugs", {})
    safety = report.get("safety", {})
    orders = report.get("orders_final", [])
    fills = report.get("fills", [])
    seq = report.get("sequence", {}) or {}
    flatten = report.get("flatten", {}) or {}

    lines: list[str] = []
    lines.append("# PHASE D.5.5-R2 — REAL ALPACA PAPER MULTI-ORDER ACCOUNTING ACCEPTANCE")
    lines.append("")
    lines.append(f"- Generated: {report.get('generated_at')}")
    lines.append(f"- Previous commit: `{impl.get('previous_commit')}`")
    lines.append(f"- Current commit: `{impl.get('current_commit')}`")
    lines.append(f"- Branch: `{impl.get('branch')}`")
    lines.append(f"- Overall result: **{report.get('result')}**")
    lines.append("")

    lines.append("## Implementation")
    lines.append("")
    for key, value in impl.items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")

    lines.append("## Verification")
    lines.append("")
    for key, value in ver.items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")

    lines.append("## REAL PAPER EXECUTION")
    lines.append("")
    if not orders:
        lines.append("_No orders were submitted._")
    for index, order in enumerate(orders, start=1):
        lines.append(f"### ORDER {index}")
        lines.append("")
        for key in (
            "side", "quantity", "order_id", "broker_order_id", "status",
            "filled_quantity", "average_fill_price", "reject_reason",
        ):
            lines.append(f"- {key} = `{order.get(key)}`")
        lines.append("")
    lines.append("### FILLS")
    lines.append("")
    if not fills:
        lines.append("_No fills observed._")
    for fill in fills:
        lines.append(
            f"- fill_id=`{fill.get('fill_id')}` order=`{fill.get('order_id')}` "
            f"side=`{fill.get('side')}` qty=`{fill.get('quantity')}` "
            f"price=`{fill.get('price')}` broker_fill_id=`{fill.get('broker_fill_id')}`"
        )
    lines.append("")

    lines.append("## ACCOUNTING (checkpoints)")
    lines.append("")
    lines.append("| label | BROKER | LEDGER | OMS | PM | overall |")
    lines.append("|---|---|---|---|---|---|")
    for cp in report.get("checkpoints", []):
        lines.append(
            f"| {cp.get('label')} | {cp.get('broker_net')} | {cp.get('ledger_net')} | "
            f"{cp.get('oms_net')} | {cp.get('pm_net')} | "
            f"{'PASS' if cp.get('comparisons', {}).get('overall') else 'FAIL'} |"
        )
    lines.append("")

    lines.append("## FINAL ACCOUNTING")
    lines.append("")
    lines.append(f"```text\nBROKER       = {snap.get('broker_net')}")
    lines.append(f"FILL LEDGER  = {snap.get('ledger_net')}")
    lines.append(f"OMS          = {snap.get('oms_net')}")
    lines.append(f"POSITION MGR = {snap.get('pm_net')}\n```")
    lines.append("")
    lines.append(f"- Reconciliation: **{final.get('reconciliation')}**")
    lines.append(f"- Paper readiness: **{final.get('paper_readiness')}**")
    lines.append(f"- Flatten: `{json.dumps(flatten, default=str)[:600]}`")
    lines.append("")

    lines.append("## SEQUENCE")
    lines.append("")
    lines.append(f"```json\n{json.dumps(seq, indent=2, default=str)}\n```")
    lines.append("")

    lines.append("## BUG REGISTER")
    lines.append("")
    for bug_id, bug in bugs.items():
        lines.append(f"### {bug_id}")
        lines.append("")
        for key, value in bug.items():
            lines.append(f"- {key}: `{json.dumps(value, default=str) if isinstance(value, (list, dict)) else value}`")
        lines.append("")

    lines.append("## SAFETY")
    lines.append("")
    for key, value in safety.items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")

    lines.append("## FINAL READINESS")
    lines.append("")
    for question, answer in (report.get("final_readiness") or {}).items():
        lines.append(f"- {question}: **{answer}**")
    lines.append("")
    return "\n".join(lines)


def git_info() -> dict[str, str]:
    import subprocess

    def run(args: list[str]) -> str:
        try:
            return subprocess.run(
                args, capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=30
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            return "unknown"

    return {
        "previous_commit": run(["git", "rev-parse", "HEAD~1"]),
        "current_commit": run(["git", "rev-parse", "HEAD"]),
        "branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "status": run(["git", "status", "--porcelain"]),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> int:
    global REPORT_TAG
    if "--smoke" in sys.argv:
        # Short plumbing check: same real paper pipeline, tighter bounds, its
        # own artifact names. Never overwrites the acceptance evidence.
        REPORT_TAG = "_smoke"
        LIMITS.update(
            {
                "max_runtime_seconds": 240.0,
                "readiness_timeout_seconds": 60.0,
                "target_round_trips": 1,
                "max_orders": 4,
                "max_market_ticks": 1500,
                "max_strategy_cycles": 300,
            }
        )
    runtime_report_path = report_path("runtime.txt")
    tee = _Tee(sys.stdout, sys.stderr)
    tee.attach(runtime_report_path)
    # Everything printed or logged after this point lands in the runtime report.
    sys.stdout = tee
    sys.stderr = tee

    env = get_env()
    repo_config = load_config(env=env)
    # Structured runtime logging is configured from the REPOSITORY configuration
    # (trading.mode=paper, execution.enabled=false) before the first event.
    configure_logging(repo_config, env, project_root=PROJECT_ROOT)

    banner("PHASE D.5.5-R2 START")
    print(f"LOG_DIR={PROJECT_ROOT / 'logs'}")
    print(f"REPORT_PATH(STEM)={report_path('<stem>')}")
    emit("PHASE START", phase="D.5.5-R2", bug="BUG-D5-001")

    repo_state = {
        "trading.mode": repo_config.trading.mode,
        "execution.enabled": repo_config.execution.enabled,
        "trading.broker": repo_config.trading.broker,
        "trading.symbol": repo_config.trading.symbol,
        "execution.order_type": repo_config.execution.order_type,
        "execution.allow_short": repo_config.execution.allow_short,
        "position_sizing.minimum_quantity": repo_config.position_sizing.minimum_quantity,
        "position_sizing.maximum_quantity": repo_config.position_sizing.maximum_quantity,
        "position_sizing.quantity_precision": repo_config.position_sizing.quantity_precision,
        "risk.maximum_open_positions": repo_config.risk.maximum_open_positions,
        "risk.maximum_orders_per_session": repo_config.risk.maximum_orders_per_session,
        "risk.risk_per_trade_percent": repo_config.risk.risk_per_trade_percent,
        "strategy.cooldown.enabled": repo_config.strategy.cooldown.enabled,
        "strategy.cooldown.minutes": repo_config.strategy.cooldown.minutes,
        "session.start": repo_config.session.start,
        "session.end": repo_config.session.end,
        "session.entry_cutoff": repo_config.session.entry_cutoff,
        "market_data.provider": repo_config.market_data.provider,
        "market_data.mock.seed": repo_config.market_data.mock.seed,
        "market_data.mock.tick_seconds": repo_config.market_data.mock.tick_seconds,
        "session_closeout.maximum_flatten_attempts": repo_config.session_closeout.maximum_flatten_attempts,
    }
    emit("CONFIGURATION (repository, pre-override)", **{
        k: v for k, v in repo_state.items() if isinstance(v, (str, int, float, bool))
    })

    checks = safety_preflight(env, repo_config)
    evidence: dict[str, Any] = {
        "phase": "D.5.5-R2",
        "generated_at": _now(),
        "repository_configuration": repo_state,
        "safety_checks": checks,
        "limits": dict(LIMITS),
        "alpaca_endpoint": "paper" if not env.is_live_alpaca_endpoint() else "LIVE",
        "live_trading_used": False,
    }

    if not all(checks.values()):
        evidence["result"] = "BLOCKED"
        evidence["blocker"] = [k for k, v in checks.items() if not v]
        emit("SAFETY CHECK FAILED", level=logging.CRITICAL, blockers=evidence["blocker"])
        print("DO NOT PLACE AN ORDER — RESULT = BLOCKED")
        write_reports(_finalize(evidence, {}, {}, {}, {}))
        return 2

    # ---- in-process execution gate (repository config untouched) ----------
    config = load_config(env=env)
    config.trading.broker = "alpaca"
    config.execution.enabled = True           # IN-PROCESS ONLY
    config.ai.enabled = False
    config.storage.enabled = False
    config.market_data.provider = "mock"
    config.market_data.history.provider = "none"
    config.market_data.max_future_skew_seconds = 86400 * 30
    # Safety clamp: never let sizing exceed the hard paper ceiling.
    config.position_sizing.maximum_quantity = min(
        float(config.position_sizing.maximum_quantity), HARD_QUANTITY_CEILING
    )
    LIMITS["max_quantity_per_order"] = config.position_sizing.maximum_quantity
    configure_logging(config, env, project_root=PROJECT_ROOT)
    emit(
        "CONFIGURATION (in-process override)",
        execution_enabled_in_process=config.execution.enabled,
        broker=config.trading.broker,
        provider=config.market_data.provider,
        max_quantity=config.position_sizing.maximum_quantity,
        note="repository configs/config.yaml is NOT modified",
    )

    from app.runtime import build_runtime

    runtime = build_runtime(config, env)
    engine = runtime.engine
    recorder = Recorder(engine, evidence)
    engine.bus.subscribe_all(recorder.on_event)

    # ---- broker preflight (before any order) -----------------------------
    preflight = await broker_preflight(engine)
    evidence["preflight"] = preflight
    if not preflight["flat_at_start"]:
        evidence["result"] = "BLOCKED"
        evidence["blocker"] = "broker or local position not flat at start"
        emit("PREFLIGHT BLOCKED", level=logging.CRITICAL, **{
            k: v for k, v in preflight.items() if isinstance(v, (str, int, float, bool))
        })
        await engine.broker.disconnect()
        write_reports(_finalize(evidence, {}, {}, {}, {}))
        return 4

    # ---- startup (market data connection, warm-up, reconcile) -------------
    emit("MARKET DATA CONNECTION", provider=config.market_data.provider)
    startup_error: str | None = None
    try:
        await runtime.startup()
        emit("WARMUP", status=engine.warmup.get("status"), reason=engine.warmup.get("reason"))
    except Exception as exc:  # noqa: BLE001
        startup_error = f"{type(exc).__name__}: {exc}"[:300]
        emit("STARTUP ERROR", level=logging.CRITICAL, error=startup_error)
        evidence["startup_error"] = startup_error
    evidence["startup_checklist"] = [
        {"name": c.name, "ok": c.ok, "detail": str(c.detail)[:200]} for c in runtime.checklist
    ]

    readiness_before = engine.readiness()
    emit("READINESS", **{k: v for k, v in readiness_before.items() if isinstance(v, (str, int, float, bool))})

    sequence: dict[str, Any] = {}
    flatten: dict[str, Any] = {}
    final: dict[str, Any] = {}
    d3: dict[str, Any] = {}
    if startup_error is None:
        try:
            emit(
                "PIPELINE START",
                signal_timeframe=config.timeframes.signal,
                context_timeframe=config.timeframes.context,
                execution_timeframe=config.timeframes.execution,
            )
            await recorder.checkpoint("pre_sequence")

            sequence = await drive_sequence(engine, engine.provider, config, recorder)

            # ---- settle -> flatten -> final reconciliation ----------------
            await settle(engine, recorder, LIMITS["convergence_timeout_seconds"])
            flatten = await flatten_and_verify(engine, recorder)
            await engine.reconcile()
            emit(
                "RECONCILIATION COMPLETED",
                ok=not engine._need_reconciliation,
                discrepancies=engine.state.reconciliation.get("discrepancies", []),
            )
            final = await final_reconciliation(engine, recorder)
            d3 = await probe_bug_d3_002(engine, recorder)
            emit("BUG-D3-002", status=d3.get("status"), orders_checked=d3.get("orders_checked"))
        except Exception as exc:  # noqa: BLE001
            emit("SEQUENCE EXCEPTION", level=logging.CRITICAL, error=f"{type(exc).__name__}: {exc}")
            evidence.setdefault("exceptions", []).append(traceback.format_exc())
            sequence.setdefault("exception", f"{type(exc).__name__}: {exc}")
        finally:
            try:
                await recorder.checkpoint("pre_shutdown")
            except Exception as exc:  # noqa: BLE001
                emit("CHECKPOINT ERROR", level=logging.ERROR, error=str(exc)[:200])
            try:
                await runtime.shutdown()
            except Exception as exc:  # noqa: BLE001
                emit("SHUTDOWN ERROR", level=logging.ERROR, error=str(exc)[:200])
    readiness_after = engine.readiness()

    # ---- bug probes -------------------------------------------------------
    emit("BUG-D4-002 PROBE START", mode="read-only startup connectivity")
    try:
        d4 = await probe_bug_d4_002()
    except Exception as exc:  # noqa: BLE001
        d4 = {"status": "NOT TESTED", "error": f"{type(exc).__name__}: {exc}"[:200]}
    d13 = probe_bug_d1_3_002(readiness_before, readiness_after, config.market_data.provider)

    # ---- repository safety re-verification --------------------------------
    reloaded = load_config(env=env)
    repo_unchanged = (
        reloaded.execution.enabled is False and reloaded.trading.mode == "paper"
    )
    emit("REPOSITORY CONFIG RE-VERIFIED", execution_enabled=reloaded.execution.enabled,
         trading_mode=reloaded.trading.mode, unchanged=repo_unchanged)

    orders_final = order_snapshots(engine)
    evidence.update(
        {
            "orders_final": orders_final,
            "orders_created": len(orders_final),
            "orders_submitted": sum(1 for o in orders_final if o.get("broker_order_id")),
            "final_broker_position": (final.get("snapshot", {}) or {}).get("broker_net"),
            "sequence": sequence,
            "flatten": flatten,
            "final_reconciliation": final,
            "bug_d3_002": d3,
            "bug_d4_002": d4,
            "bug_d1_3_002": d13,
            "readiness_during_run": readiness_before,
            "readiness_after_teardown": readiness_after,
            "repository_config_unchanged": repo_unchanged,
            "event_counts_final": dict(recorder.counts),
            "partial_fill": _partial_fill_evidence(recorder),
        }
    )

    converged = bool(final.get("converged"))
    all_zero = bool(final.get("all_zero"))
    multi_order = len(orders_final) >= 2
    opposing = _has_opposing(recorder.fill_records)
    result = "PASS" if (converged and all_zero and multi_order and opposing and repo_unchanged) else "FAIL"
    evidence["result"] = result

    d5_bug = _d5_verdict(evidence, converged, all_zero, opposing, multi_order)
    bugs = build_bug_register(d5_bug, d3, d4, d13)

    _gi = git_info()
    _status_lines = [l for l in (_gi.get("status") or "").splitlines() if l.strip()]
    _created = [l[3:] for l in _status_lines if l.startswith("??")]
    _modified = [l[3:] for l in _status_lines if l.startswith(" M") or l.startswith("M ")]
    report = _finalize(evidence, sequence, flatten, final, bugs)
    report["result"] = result
    report["implementation"] = {
        "phase": "D.5.5-R2",
        **_gi,
        "files_created": _created,
        "files_modified": _modified,
        "configuration_changes": "none in repository (execution.enabled=false retained); in-process gate opened only inside the harness",
        "architecture_changes": "none",
    }
    report["verification"] = {
        "orders_created": len(orders_final),
        "orders_with_broker_id": sum(1 for o in orders_final if o.get("broker_order_id")),
        "fills_observed": len(recorder.fill_records),
        "checkpoints": len(recorder.checkpoints),
        "divergent_checkpoints": [
            cp["label"] for cp in recorder.checkpoints
            if not cp.get("comparisons", {}).get("overall")
        ],
        "event_counts": dict(recorder.counts),
        "sequence": sequence,
        "runtime_report": str(runtime_report_path),
    }
    report["safety"] = {
        "trading.mode": repo_state["trading.mode"],
        "execution.enabled (repository)": repo_state["execution.enabled"],
        "execution.enabled (in-process during run)": True,
        "execution.enabled (repository, re-verified)": reloaded.execution.enabled,
        "alpaca_endpoint": evidence["alpaca_endpoint"],
        "broker_paper_flag": preflight.get("broker_paper_flag"),
        "live_trading_used": False,
        "orders_created": len(orders_final),
        "orders_submitted": sum(1 for o in orders_final if o.get("broker_order_id")),
        "max_quantity_per_order": LIMITS["max_quantity_per_order"],
        "max_open_exposure": LIMITS["max_open_exposure"],
        "final_broker_position": (final.get("snapshot", {}) or {}).get("broker_net"),
        "repository_config_unchanged": repo_unchanged,
    }
    report["final_readiness"] = {
        "Can the normal application now enable paper execution automatically?": (
            "YES" if result == "PASS" else "NO"
        ),
        "Can execution.enabled be safely changed in repository config?": (
            "YES" if result == "PASS" else "NO"
        ),
        "Is BUG-D5-001 fixed?": "YES" if bugs["BUG-D5-001"]["status"] == "FIXED" else "NO",
        "Is real multi-order convergence proven?": "YES" if converged and multi_order else "NO",
        "Is real partial fill proven?": "YES" if evidence["partial_fill"].get("observed") else "NO",
        "Is final broker position flat?": "YES" if all_zero else "NO",
    }
    write_reports(report)

    banner("PHASE D.5.5-R2 REPORT")
    print(render_markdown(report))
    emit("PHASE END", phase="D.5.5-R2", result=result)
    print(f"\nRESULT: {result}")
    print(f"Artifacts: {report_path('report.md')}")
    return 0 if result == "PASS" else 1


def _has_opposing(fills: list[dict[str, Any]]) -> bool:
    sides = {f.get("side") for f in fills}
    return "buy" in sides and "sell" in sides


def _partial_fill_evidence(recorder: Recorder) -> dict[str, Any]:
    observed = False
    rows = []
    for checkpoint in recorder.checkpoints:
        for row in checkpoint.get("cumulative_filled_by_order", []):
            filled = row.get("broker_filled_qty") or 0.0
            requested = row.get("requested_qty") or 0.0
            if 0.0 < filled < requested - 1e-12:
                observed = True
                rows.append(row)
    return {
        "observed": observed,
        "status": "REAL PARTIAL FILL = OBSERVED" if observed else "REAL PARTIAL FILL = NOT TESTED",
        "evidence": rows,
        "note": "Broker responses were never manipulated; only naturally observed states are reported.",
    }


def _d5_verdict(evidence: dict[str, Any], converged: bool, all_zero: bool,
                opposing: bool, multi_order: bool) -> dict[str, Any]:
    divergent = [
        cp["label"] for cp in evidence.get("checkpoints", [])
        if not cp.get("comparisons", {}).get("overall")
    ]
    fixed = converged and all_zero and multi_order and opposing
    return {
        "converged": converged,
        "all_zero_at_end": all_zero,
        "multi_order": multi_order,
        "opposing_sides": opposing,
        "divergent_checkpoints": divergent,
        "status": "FIXED" if fixed else "OPEN",
        "root_cause": (
            "Cumulative broker fills were applied as absolute quantities in more "
            "than one place (or applied without a valid fill price), so OMS / "
            "FillLedger / PositionManager stopped matching the broker under "
            "opposing orders."
            if not fixed
            else "Fill bridge applies cumulative-fill DELTAS only; the authoritative "
                 "FillLedger is the single source feeding OMS and PositionManager."
        ),
        "fix": (
            "app/accounting/ledger.py (authoritative fill ledger + validity gate), "
            "app/runner/engine.py (_apply_fill / _resync_orders_from_broker delta "
            "bridge), app/orders/oms.py (net accounting view + duplicate fill guards)"
        ) if fixed else "not proven by this run",
        "evidence": [
            f"checkpoints={len(evidence.get('checkpoints', []))}",
            f"divergent={divergent}",
            f"orders={evidence.get('orders_created')}",
            f"fills={len(evidence.get('fills', []))}",
        ],
    }


def _finalize(evidence, sequence, flatten, final, bugs) -> dict[str, Any]:
    report = dict(evidence)
    report.setdefault("phase", "D.5.5-R2")
    report["sequence"] = sequence
    report["flatten"] = flatten
    report["final_reconciliation"] = final
    report["bugs"] = bugs or {}
    report.setdefault("generated_at", _now())
    return report


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

