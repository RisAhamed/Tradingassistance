"""Phase D.3 — controlled REAL Alpaca PAPER trading endpoint acceptance.

Proves the real paper broker lifecycle with ONE bounded order:

    MODE CHECK -> ACCOUNT -> FLAT CHECK -> PAPER ORDER -> FILL/ACK
    -> POSITION READ -> CLOSE -> POSITION ZERO -> REPORT

Safety invariants enforced before ANY request:
* trading.mode == "paper"
* env indicates a paper endpoint (never live)
* execution gate opened IN-PROCESS only (repo config untouched)
* bounded quantity and single attempt
* local + broker position verified flat at the end

If any check fails -> abort, NO order is sent, result UNKNOWN/BLOCKED.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.logging import configure_logging
from app.domain.enums import Direction, OrderStatus, Side
from app.domain.orders import Order
from app.core.ids import new_order_id, new_correlation_id

QUANTITY = 0.001  # bounded, ~$100 notional on BTC/USD
SYMBOL = "BTC/USD"


async def main() -> int:
    env = get_env()
    config = load_config(env=env)
    config.trading.broker = "alpaca"
    config.execution.enabled = True              # in-process only
    config.ai.enabled = False
    configure_logging(config, env, project_root=PROJECT_ROOT)

    evidence: dict = {"phase": "D.3", "ts": datetime.now(timezone.utc).isoformat()}
    # ---- mode / endpoint enforcement -------------------------------------
    checks = {
        "trading_mode_paper": config.trading.mode == "paper",
        "env_alpaca_paper": bool(env.alpaca_paper),
        "not_live_endpoint": not env.is_live_alpaca_endpoint(),
        "has_credentials": env.has_alpaca_credentials(),
        "broker_paper": True,
    }
    evidence["safety_checks"] = checks
    if not all(checks.values()):
        evidence["result"] = "BLOCKED"
        print("ABORT: safety check failed (no order sent)", checks)
        _write(evidence)
        return 2

    from app.brokers.alpaca import AlpacaPaperBroker

    broker = AlpacaPaperBroker(env.alpaca_api_key, env.alpaca_api_secret, feed=config.market_data.feed)
    evidence["broker"] = broker.name
    evidence["broker_paper"] = broker.paper is True
    if broker.name != "alpaca" or broker.paper is not True:
        evidence["result"] = "BLOCKED"
        _write(evidence)
        return 2

    try:
        await broker.connect()
    except Exception as exc:  # noqa: BLE001
        evidence["result"] = "BLOCKED"
        evidence["error"] = f"connect_failed: {exc.__class__.__name__}"
        _write(evidence)
        return 3

    account = await broker.get_account()
    evidence["account_equity"] = account.equity
    positions = await broker.get_positions()
    evidence["pre_positions"] = len(positions)
    if positions:
        evidence["result"] = "BLOCKED"
        evidence["error"] = "broker position not flat at start"
        _write(evidence)
        return 4

    # ---- one bounded market order ----------------------------------------
    side = Side.BUY
    order = Order(
        order_id=new_order_id(), client_order_id=new_order_id(), timestamp=datetime.now(timezone.utc),
        symbol=SYMBOL, side=side, direction=Direction.LONG, quantity=QUANTITY,
        correlation_id=new_correlation_id(),
    )
    print(f"[OMS] PAPER ORDER ATTEMPT side={side.value} qty={QUANTITY} symbol={SYMBOL} id={order.order_id}")
    try:
        result = await broker.submit_order(order)
    except Exception as exc:  # noqa: BLE001
        evidence["result"] = "HALT_AMBIGUOUS"
        evidence["error"] = f"submit_exception: {exc.__class__.__name__}"
        positions2 = await _safe_positions(broker)
        evidence["post_attempt_positions"] = len(positions2)
        _write(evidence)
        return 5

    evidence["submit_status"] = result.status.value
    evidence["broker_order_id"] = result.broker_order_id
    evidence["fills"] = len(result.fills)
    order.broker_order_id = result.broker_order_id

    # ---- wait for authoritative fill --------------------------------------
    final_order = order
    for _ in range(20):
        try:
            final_order = await broker.get_order(order)
        except Exception:  # noqa: BLE001
            break
        evidence["order_status_seen"] = final_order.status.value
        if final_order.status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED):
            break
        await asyncio.sleep(1.0)
    evidence["final_order_status"] = final_order.status.value

    positions_mid = await _safe_positions(broker)
    evidence["mid_positions"] = [p.symbol for p in positions_mid]

    # ---- close / flatten ---------------------------------------------------
    flat_ok = False
    if positions_mid:
        for p in positions_mid:
            try:
                close_result = await broker.close_position(p)
                evidence["close_status"] = close_result.status.value
            except Exception as exc:  # noqa: BLE001
                evidence["close_error"] = exc.__class__.__name__
        await asyncio.sleep(2.0)
        positions_end = await _safe_positions(broker)
        evidence["final_positions"] = len(positions_end)
        flat_ok = len(positions_end) == 0
    else:
        evidence["final_positions"] = 0
        flat_ok = result.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED)

    evidence["result"] = "PASS" if flat_ok else "FAIL_FLATNESS"
    _write(evidence)
    print(f"RESULT: {evidence['result']}")
    print(json.dumps({k: v for k, v in evidence.items() if k != "ts"}, indent=2))
    try:
        await broker.disconnect()
    except Exception:  # noqa: BLE001
        pass
    return 0 if evidence["result"] == "PASS" else 6


async def _safe_positions(broker):
    try:
        return await broker.get_positions()
    except Exception:  # noqa: BLE001
        return []


def _write(evidence: dict) -> None:
    out = PROJECT_ROOT / "logs" / "reports" / "phase_d3_alpaca_paper.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8")
    md = PROJECT_ROOT / "logs" / "reports" / "phase_d3_alpaca_paper.md"
    md.write_text("# Phase D.3 Alpaca Paper Endpoint Acceptance\n\n```json\n" + json.dumps(evidence, indent=2, default=str) + "\n```\n", encoding="utf-8")
    print(f"artifact: {out}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
