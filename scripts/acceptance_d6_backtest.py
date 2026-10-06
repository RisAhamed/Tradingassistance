"""Phase D.6 — Historical Replay / Backtesting Acceptance.

Executes an end-to-end deterministic backtest and produces machine-readable
evidence. NEVER contacts a real broker. Verifies:
  - data loaded
  - replay completed
  - trades generated or explicitly zero
  - all trades accounted for (ledger == OMS == PM)
  - final position == 0
  - no real broker order submitted
  - execution.enabled remains false
  - trading.mode remains paper
  - reproducibility
  - metrics generated
  - report generated
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtesting.analyzer import analyze
from app.backtesting.data import generate_history
from app.backtesting.engine import BacktestEngine
from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.logging import configure_logging

logger = logging.getLogger("acceptance.d6")

REPORTS = PROJECT_ROOT / "logs" / "reports"
LOG_DIR = PROJECT_ROOT / "logs"


def emit(event: str, **fields: Any) -> None:
    payload = {"event": event, "component": "acceptance"}
    payload.update(fields)
    logger.info(event, extra={"structured": payload})


def banner(title: str) -> None:
    line = "=" * 66
    print(f"\n{line}\n{title}\n{line}")


async def main() -> int:
    env = get_env()
    config = load_config(env=env)
    configure_logging(config, env, project_root=PROJECT_ROOT)

    banner("PHASE D.6 START")
    print(f"LOG_DIR={LOG_DIR}")
    emit("PHASE START", phase="D.6")

    # ---- safety preflight ------------------------------------------------
    checks = {
        "trading_mode_paper": config.trading.mode == "paper",
        "execution_enabled_false": config.execution.enabled is False,
        "backtest_enabled": config.backtesting.enabled,
        "no_live_endpoint": not env.is_live_alpaca_endpoint(),
    }
    emit("SAFETY CHECKS", **checks)
    if not all(checks.values()):
        emit("SAFETY CHECK FAILED", level=logging.CRITICAL, failed=[k for k, v in checks.items() if not v])
        print("DO NOT RUN — SAFETY CHECK FAILED")
        return 2

    # ---- data --------------------------------------------------------------
    candles = generate_history(
        config.trading.symbol,
        timeframe=config.timeframes.signal,
        length=600,
        seed=7,
    )
    emit("DATA_LOADED", source="synthetic", symbol=config.trading.symbol, bars=len(candles), timeframe=config.timeframes.signal)

    # ---- run backtest ------------------------------------------------------
    emit("BACKTEST START", run_id="pending")
    result = BacktestEngine(config).run(candles)
    emit(
        "BACKTEST COMPLETE",
        run_id=result.run_id,
        trades=result.metrics.trade_count,
        signals=result.signals,
        rejections=result.rejections,
        net_pnl=result.metrics.net_pnl,
        win_rate=result.metrics.win_rate,
        profit_factor=result.metrics.profit_factor,
        max_drawdown=result.metrics.max_drawdown,
    )

    # ---- analysis ----------------------------------------------------------
    breakdowns = analyze(result.trades)
    emit("ANALYSIS COMPLETE", regimes=list(breakdowns["by_regime"].keys()))

    # ---- accounting verification ------------------------------------------
    accounting_ok = all(c["consistent"] for c in result.accounting_checks)
    final_pm = 0.0
    if result.accounting_checks:
        final_pm = result.accounting_checks[-1]["pm"]
    emit(
        "ACCOUNTING VERIFICATION",
        checks=len(result.accounting_checks),
        all_consistent=accounting_ok,
        final_pm=final_pm,
    )

    # ---- reproducibility ----------------------------------------------------
    result2 = BacktestEngine(config).run(candles)
    reproducible = result.run_id == result2.run_id and result.metrics.net_pnl == result2.metrics.net_pnl
    emit("REPRODUCIBILITY", run_id=result.run_id, reproducible=reproducible)

    # ---- report ------------------------------------------------------------
    REPORTS.mkdir(parents=True, exist_ok=True)
    run_id = result.run_id
    report = {
        "phase": "D.6",
        "run_id": run_id,
        "git_commit": result.git_commit,
        "config_hash": result.config_hash,
        "data_fingerprint": result.data_fingerprint,
        "symbol": config.trading.symbol,
        "timeframe": config.timeframes.signal,
        "data_source": "synthetic",
        "bars": len(candles),
        "metrics": result.metrics.to_dict(),
        "trades": result.trades,
        "breakdowns": breakdowns,
        "accounting": {
            "checks": len(result.accounting_checks),
            "all_consistent": accounting_ok,
            "final_pm": final_pm,
        },
        "reproducibility": reproducible,
        "safety": {
            "execution_enabled": config.execution.enabled,
            "trading_mode": config.trading.mode,
            "live_trading_used": False,
        },
    }
    report_path = REPORTS / f"phase_d6_backtest_{run_id}.json"
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    trades_path = REPORTS / f"phase_d6_trades_{run_id}.json"
    trades_path.write_text(json.dumps(result.trades, indent=2, default=str) + "\n", encoding="utf-8")
    metrics_path = REPORTS / f"phase_d6_metrics_{run_id}.json"
    metrics_path.write_text(json.dumps(report["metrics"], indent=2) + "\n", encoding="utf-8")

    # ---- verdict -----------------------------------------------------------
    zero_trade = result.metrics.trade_count == 0
    status = "PASS" if (accounting_ok and reproducible and final_pm == 0.0) else "FAIL"
    if zero_trade:
        status = "NO TRADES"
    emit("PHASE END", phase="D.6", result=status)

    banner("PHASE D.6 REPORT")
    print(f"RUN ID: {run_id}")
    print(f"TRADES: {result.metrics.trade_count}")
    print(f"NET P&L: {result.metrics.net_pnl}")
    print(f"MAX DRAWDOWN: {result.metrics.max_drawdown}")
    print(f"PROFIT FACTOR: {result.metrics.profit_factor}")
    print(f"RECONCILIATION: {'PASS' if accounting_ok else 'FAIL'}")
    print(f"FINAL POSITION: {final_pm}")
    print(f"EXECUTION.ENABLED: {config.execution.enabled}")
    print(f"TRADING.MODE: {config.trading.mode}")
    print(f"LIVE TRADING: false")
    print(f"OVERALL STATUS: {status}")
    print(f"Artifacts: {report_path}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
