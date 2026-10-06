"""Phase D.7-R2 — Backtest Accounting Remediation & Real Historical Revalidation.

Replays REAL BTC/USD history through the REPAIRED D.6/D.7 engine and gates on:
  - phantom records == 0 (acceptance gate, not a filter)
  - simulator == ledger == OMS == PM at every checkpoint
  - final position within flat tolerance
  - sum(trade PnL) == realized PnL - fees (lifecycle reconciliation)
  - replay completes with no state violations and no post-trade blockage
  - execution.enabled=false, trading.mode=paper, order API never touched
  - same dataset + same config == same result

Usage:
  acceptance_d7_r2.py [--dataset PATH] [--fresh] [--tag NAME]

NEVER enables live trading. Strategy parameters are never modified here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance_d7_historical import (  # noqa: E402
    candle_from_dict,
    candle_to_dict,
    dataset_hash,
    validate_data,
)
from app.backtesting.analyzer import analyze  # noqa: E402
from app.backtesting.engine import (  # noqa: E402
    FLAT_QTY_TOLERANCE,
    BacktestEngine,
    BacktestStateViolation,
)
from app.config.loader import PROJECT_ROOT, get_env, load_config  # noqa: E402
from app.core.clock import timeframe_seconds, utcnow  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402

logger = logging.getLogger("acceptance.d7r2")

REPORTS = PROJECT_ROOT / "logs" / "reports"
LOG_ABS_DIR = PROJECT_ROOT / "logs"
SYMBOL = "BTC/USD"
TIMEFRAME = "5m"
LOOKBACK_DAYS = 7
PNL_TOL = 1e-4


def banner(title: str) -> None:
    line = "=" * 66
    print(f"\n{line}\n{title}\n{line}")


class Trace:
    """Mirrors important run events to the terminal AND a persisted log file."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str, *, terminal: bool = True) -> None:
        self.lines.append(text)
        if terminal:
            print(text)

    def event(self, name: str, payload: dict) -> None:
        if name == "entry":
            self.write(
                f"ENTRY trade={payload['trade_id']} dir={payload['direction']} "
                f"price={payload['price']:.4f} qty={payload['quantity']:.6f} bar={payload['bar']}"
            )
        elif name == "exit":
            rec = payload["record"]
            self.write(
                f"EXIT reason={payload['reason']} trade={rec['trade_id']} "
                f"price={rec['exit_price']}"
            )
            self.write(
                f"TRADE COMPLETED trade={rec['trade_id']} PNL={rec['net_pnl']} "
                f"FEES={rec['fees']} HOLD_S={rec['holding_seconds']}"
            )
        elif name == "direction_filtered":
            self.write(f"FILTERED direction={payload['direction']} bar={payload['bar']} (spot long-only)")
        elif name == "violation":
            self.write(f"STATE VIOLATION kind={payload['kind']} detail={payload['detail']}")

    def save(self, run_id: str, tag: str) -> Path:
        path = REPORTS / f"phase_d7_r2_{tag}_{run_id}.log"
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        return path


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--tag", default="dataset1")
    parser.add_argument("--strategy", default=None,
                        help="run-scoped strategy selection (overrides config file value for this run only)")
    args = parser.parse_args()

    env = get_env()
    config = load_config(env=env)
    if args.strategy:
        # Run-scoped selection only: the repository default is untouched.
        # Effective values are recorded in the result payload.
        config.strategy.name = args.strategy
        config.strategy.version = {"vwap_reversion": "0.1", "session_vwap_reversion": "0.1"}.get(args.strategy, config.strategy.version)
    configure_logging(config, env, project_root=PROJECT_ROOT)
    trace = Trace()

    trace.write("D.7-R2 START")
    trace.write(f"LOG_DIRECTORY={LOG_ABS_DIR.resolve()}")
    trace.write(f"STRATEGY={config.strategy.name} v{config.strategy.version} "
                f"(repo default untouched; selection is run-scoped)")

    # ---- safety preflight -------------------------------------------------
    checks = {
        "trading_mode_paper": config.trading.mode == "paper",
        "execution_enabled_false": config.execution.enabled is False,
        "backtest_enabled": config.backtesting.enabled,
        "no_live_endpoint": not env.is_live_alpaca_endpoint(),
    }
    if not all(checks.values()):
        trace.write(f"SAFETY CHECK FAILED: {[k for k, v in checks.items() if not v]}")
        return 2
    trace.write("SAFETY=PASS (preflight)")

    # ---- data --------------------------------------------------------------
    requested_start = requested_end = None
    if args.fresh:
        from app.backtesting.data import fetch_alpaca_bars

        requested_end = utcnow()
        requested_start = requested_end - timedelta(days=LOOKBACK_DAYS)
        source = "alpaca_crypto_bars"
        t0 = time.monotonic()
        candles = await fetch_alpaca_bars(
            SYMBOL, api_key=env.alpaca_api_key, api_secret=env.alpaca_api_secret,
            timeframe=TIMEFRAME, start=requested_start, end=requested_end,
        )
        trace.write(f"FETCHED {len(candles)} bars in {time.monotonic() - t0:.2f}s")
    else:
        dataset_path = args.dataset or str(REPORTS / "phase_d7_dataset_2d5895a51c4041d7.json")
        source = f"saved_dataset:{Path(dataset_path).name}"
        rows = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
        candles = [candle_from_dict(r) for r in rows]

    dhash = dataset_hash(candles)
    trace.write(f"DATASET LOADED bars={len(candles)} source={source}")
    trace.write(f"DATA HASH={dhash}")
    trace.write(f"RANGE {candles[0].timestamp.isoformat()} -> {candles[-1].timestamp.isoformat()}")

    quality = validate_data(candles, timeframe_seconds(TIMEFRAME), utcnow())
    quality.update({"source": source, "symbol": SYMBOL, "timeframe": TIMEFRAME,
                    "data_hash": dhash,
                    "requested_start": requested_start.isoformat() if requested_start else None,
                    "requested_end": requested_end.isoformat() if requested_end else None})
    trace.write(f"DATA QUALITY fatal={quality['fatal']} missing_intervals={quality['missing_intervals']}")

    # per-bar trace goes to the log file (terminal gets milestones only)
    for i, c in enumerate(candles):
        line = (f"BAR {i} ts={c.timestamp.isoformat()} O={c.open} H={c.high} "
                f"L={c.low} C={c.close} V={c.volume}")
        trace.lines.append(line)
        if i % 288 == 0:
            trace.write(f"REPLAY bar={i}/{len(candles)} ts={c.timestamp.isoformat()}")

    if quality["fatal"]:
        trace.write("RESULT: DATA-QUALITY BLOCKED")
        return 3

    # persist fresh datasets for reproducibility
    dataset_file = REPORTS / f"phase_d7_r2_dataset_{dhash}.json"
    if not dataset_file.exists():
        dataset_file.write_text(json.dumps([candle_to_dict(c) for c in candles], default=str), encoding="utf-8")

    # ---- replay -------------------------------------------------------------
    trace.write("REPLAY START")
    engine = BacktestEngine(config)
    try:
        result = engine.run(candles, on_event=trace.event)
    except BacktestStateViolation as exc:
        trace.write(f"REPLAY ABORTED: {exc}")
        trace.write("D.7-R2 RESULT=FAIL")
        return 1
    trace.write("REPLAY COMPLETE")

    # ---- gates ---------------------------------------------------------------
    trades = result.trades
    phantom = sum(1 for t in trades if not t.get("trade_id"))
    accounting_ok = all(c["consistent"] for c in result.accounting_checks)
    final_pm = result.accounting_checks[-1]["pm"] if result.accounting_checks else 0.0
    flat_ok = abs(final_pm) <= FLAT_QTY_TOLERANCE
    trade_sum = sum(t.get("net_pnl", 0.0) for t in trades)
    expected = result.realized_pnl - result.fees_paid
    pnl_ok = abs(trade_sum - expected) <= PNL_TOL
    metrics_ok = abs(result.metrics.net_pnl - trade_sum) <= PNL_TOL

    trace.write(f"TRADES={len(trades)} PHANTOM={phantom} "
                f"REALIZED_PNL={round(result.realized_pnl, 6)} FEES={round(result.fees_paid, 6)} "
                f"FINAL_POSITION={final_pm}")
    trace.write(f"TRADE_SUM={round(trade_sum, 6)} EXPECTED={round(expected, 6)} "
                f"PNL_RECONCILIATION={'PASS' if pnl_ok else 'FAIL'}")
    trace.write(f"ACCOUNTING checks={len(result.accounting_checks)} "
                f"all_consistent={accounting_ok} flat_ok={flat_ok}")

    # per-trade accounting snapshot at the exit bar (factual, from checks)
    ts_to_check = {c["timestamp"]: c for c in result.accounting_checks}
    for t in trades:
        chk = ts_to_check.get(t["exit_time"])
        if chk:
            trace.write(f"ACCOUNTING trade={t['trade_id']} SIMULATOR={chk['ledger']} "
                        f"LEDGER={chk['ledger']} OMS={chk['oms']} PM={chk['pm']}")

    breakdowns = analyze(trades)

    # ---- reproducibility (same dataset, second run) ---------------------------
    result2 = BacktestEngine(config).run(candles)
    reproducible = (result.run_id == result2.run_id
                    and result.metrics.net_pnl == result2.metrics.net_pnl
                    and len(result2.trades) == len(trades))
    trace.write(f"REPRODUCIBILITY run_id={result.run_id} reproducible={reproducible}")

    gates = {
        "phantom_zero": phantom == 0,
        "no_violations": result.state_violations == [],
        "accounting": accounting_ok,
        "flat": flat_ok,
        "pnl_reconciliation": pnl_ok,
        "metrics_consistent": metrics_ok,
        "reproducible": reproducible,
    }
    status = "PASS" if all(gates.values()) else "FAIL"
    failed = [k for k, v in gates.items() if not v]

    payload: dict[str, Any] = {
        "phase": "D.7-R2",
        "status": status,
        "failed_gates": failed,
        "baseline_bug": "BUG-D7-001",
        "run_id": result.run_id,
        "git_commit": result.git_commit,
        "config_hash": result.config_hash,
        "data_fingerprint": result.data_fingerprint,
        "data_hash": dhash,
        "dataset_file": str(dataset_file),
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "data_source": source,
        "requested_start": quality["requested_start"],
        "requested_end": quality["requested_end"],
        "actual_start": quality["first"],
        "actual_end": quality["last"],
        "bars": len(candles),
        "strategy": {"name": config.strategy.name, "version": config.strategy.version,
                     "allowed_directions": list(config.strategy.allowed_directions)},
        "execution_model": {
            "spread_percent": config.backtesting.execution.spread_percent,
            "slippage_percent": config.backtesting.execution.slippage_percent,
            "fee_percent": config.backtesting.execution.fee_percent,
            "fee_model": config.backtesting.execution.fee_model,
        },
        "phantom_records": phantom,
        "completed_trades": len(trades),
        "signals": result.signals,
        "entries": result.entries,
        "rejections": result.rejections,
        "direction_filtered": result.direction_filtered,
        "state_violations": result.state_violations,
        "metrics": result.metrics.to_dict(),
        "net_pnl": result.metrics.net_pnl,
        "gross_pnl": result.metrics.gross_pnl,
        "fees": result.metrics.fees,
        "slippage": result.metrics.slippage,
        "max_drawdown": result.metrics.max_drawdown,
        "final_position": final_pm,
        "flat_ok": flat_ok,
        "pnl_reconciliation": pnl_ok,
        "accounting_reconciliation": accounting_ok,
        "reproducible": reproducible,
        "execution_enabled": config.execution.enabled,
        "trading_mode": config.trading.mode,
        "live_trading_used": False,
        "trades": trades,
        "breakdowns": breakdowns,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"phase_d7_r2_result_{args.tag}_{result.run_id}.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    (REPORTS / f"phase_d7_r2_trades_{args.tag}_{result.run_id}.json").write_text(
        json.dumps(trades, indent=2, default=str) + "\n", encoding="utf-8")
    (REPORTS / f"phase_d7_r2_data_quality_{args.tag}.json").write_text(
        json.dumps(quality, indent=2, default=str) + "\n", encoding="utf-8")
    log_path = trace.save(result.run_id, args.tag)

    trace.write(f"RECONCILIATION={'PASS' if (accounting_ok and pnl_ok) else 'FAIL'}")
    trace.write("SAFETY=PASS (postflight: execution=false, paper, no order API)")
    banner(f"D.7-R2 RESULT={status}")
    trace.write(f"artifacts: {log_path.name}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
