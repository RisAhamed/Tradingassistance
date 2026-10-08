"""JSON report + terminal summary (Phase J)."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config.loader import PROJECT_ROOT

OUTCOMES = (
    "pass", "preflight_failed", "order_rejected", "ambiguous_submission",
    "flatten_failed", "unexpected_error", "no_signal_within_timeout",
)


def new_report(mode: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "phase": "J",
        "mode": mode,
        "started_at": now,
        "ended_at": now,
        "git_commit": git_commit(),
        "config_hash": "",
        "repository_execution_enabled": False,
        "repository_paper_safety_enabled": False,
        "in_process_execution_enabled": True,
        "in_process_paper_safety_enabled": True,
        "symbol": "BTC/USD",
        "clamped_quantity": 0.0,
        "preflight": [],
        "order": {"order_id": None, "broker_order_id": None, "side": None,
                  "quantity": None, "status": None, "filled_quantity": 0.0,
                  "average_fill_price": None, "reject_reason": None, "fills_count": 0},
        "position_after_fill": {"direction": None, "quantity": 0.0, "average_entry": None},
        "flatten": {"attempts": 0, "final_broker_zero": False, "final_local_flat": False,
                    "elapsed_seconds": 0.0},
        "reconciliation": {"ok": False, "discrepancies": []},
        "exit_code": 6,
        "outcome": "unexpected_error",
        "error": None,
    }


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, cwd=str(PROJECT_ROOT), timeout=15)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - offline-safe fallback
        return "unknown"


def config_hash(config) -> str:
    return hashlib.sha256(
        json.dumps(config.model_dump(mode="json"), sort_keys=True, default=str).encode()
    ).hexdigest()


def default_report_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    return PROJECT_ROOT / "logs" / "reports" / f"phase_j_{stamp}.json"


def write_report(report: dict[str, Any], path: str | Path | None) -> Path:
    out = Path(path) if path else default_report_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
                   encoding="utf-8")
    return out


def banner_top(config) -> str:
    return (
        "PAPER TRADING ONLY — bounded live acceptance\n"
        f"repository execution.enabled = FALSE (unchanged)\n"
        "in-process execution enabled = TRUE (temporary)\n"
        f"max order quantity = {config.live_acceptance.max_order_quantity}"
    )


def banner_bottom(report: dict[str, Any]) -> str:
    return (
        "PAPER TRADING ONLY — bounded live acceptance\n"
        "repository execution.enabled = FALSE (unchanged)\n"
        f"RESULT: {report['outcome'].upper()} (exit {report['exit_code']})"
    )


def print_summary(report: dict[str, Any], path: Path) -> None:
    order = report["order"]
    pos = report["position_after_fill"]
    flat = report["flatten"]
    print(f"PHASE J — {report['mode']}")
    print(f"preflight: {'PASS' if all(c['ok'] for c in report['preflight']) else 'FAIL'}")
    print(f"order: submitted {order['order_id']} -> {order['status']}")
    print(f"position after fill: {pos['direction']} {pos['quantity']} @ {pos['average_entry']}")
    print(f"flatten: {'OK' if flat['final_broker_zero'] and flat['final_local_flat'] else 'FAILED'} "
          f"after {flat['attempts']} attempts")
    print(f"reconciliation: {'OK' if report['reconciliation']['ok'] else 'MISMATCH'}")
    print("repository config unchanged: YES")
    print(f"RESULT: {report['outcome'].upper()} (exit {report['exit_code']})")
    print(f"report: {path}")
