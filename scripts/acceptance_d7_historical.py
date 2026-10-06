"""Phase D.7 — Real Historical Validation Acceptance.

Runs the UNCHANGED D.6 backtesting pipeline against REAL BTC/USD historical
market data (Alpaca crypto bars). NEVER contacts a trading/order API, NEVER
enables live trading, NEVER modifies strategy parameters. Verifies:
  - real data fetched (no synthetic substitution)
  - data quality validated (ordering, uniqueness, spacing, OHLC, prices)
  - replay completed on the D.6 engine
  - all trades accounted for (ledger == OMS == PM)
  - final position == 0
  - execution.enabled remains false, trading.mode remains paper
  - reproducibility on the persisted dataset
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtesting.analyzer import analyze
from app.backtesting.data import fetch_alpaca_bars
from app.backtesting.engine import BacktestEngine
from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.clock import timeframe_seconds, utcnow
from app.core.logging import configure_logging
from app.domain.market import Candle

logger = logging.getLogger("acceptance.d7")

REPORTS = PROJECT_ROOT / "logs" / "reports"
LOG_DIR = PROJECT_ROOT / "logs"

SYMBOL = "BTC/USD"
TIMEFRAME = "5m"
LOOKBACK_DAYS = 7
MIN_BARS = 100  # warm-up floor: EMA-50 + regime min-20 + range lookback-20


def emit(event: str, **fields: Any) -> None:
    payload = {"event": event, "component": "acceptance"}
    payload.update(fields)
    logger.info(event, extra={"structured": payload})


def banner(title: str) -> None:
    line = "=" * 66
    print(f"\n{line}\n{title}\n{line}")


def candle_to_dict(c: Candle) -> dict:
    return {
        "timestamp": c.timestamp.isoformat(),
        "symbol": c.symbol,
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": c.volume,
        "timeframe": c.timeframe,
    }


def candle_from_dict(row: dict) -> Candle:
    ts = datetime.fromisoformat(row["timestamp"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return Candle(
        timestamp=ts,
        symbol=row.get("symbol", SYMBOL),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row.get("volume", 0.0)),
        timeframe=row.get("timeframe", TIMEFRAME),
    )


def dataset_hash(candles: list[Candle]) -> str:
    digest = hashlib.sha256()
    for c in candles:
        digest.update(
            f"{c.timestamp.isoformat()}|{c.open}|{c.high}|{c.low}|{c.close}|{c.volume}\n".encode()
        )
    return digest.hexdigest()[:16]


def validate_data(candles: list[Candle], step: int, now: datetime) -> dict:
    """Validate real bars. Returns a quality report dict with fatal flags.

    FATAL defects (dataset rejected, no silent correction):
      unordered, duplicates, malformed/NaN, non-positive OHLC,
      high/low inconsistent, negative volume, future timestamps,
      insufficient bars.
    REPORTED but tolerated: missing 5m intervals (engine replays the bar
    sequence as-is and never fabricates fills).
    """
    report: dict[str, Any] = {
        "bars": len(candles),
        "expected_step_seconds": step,
        "first": candles[0].timestamp.isoformat() if candles else None,
        "last": candles[-1].timestamp.isoformat() if candles else None,
        "unordered": 0,
        "duplicates": 0,
        "malformed": 0,
        "non_positive_price": 0,
        "ohlc_inconsistent": 0,
        "negative_volume": 0,
        "future_bars": 0,
        "missing_intervals": 0,
        "missing_locations": [],
        "spacing_values": [],
        "fatal": False,
        "fatal_reasons": [],
    }
    if len(candles) < MIN_BARS:
        report["fatal"] = True
        report["fatal_reasons"].append(f"insufficient bars {len(candles)} < {MIN_BARS}")
        return report
    seen: set[str] = set()
    spacings: set[float] = set()
    prev_ts = None
    for i, c in enumerate(candles):
        try:
            o, h, l, cl = float(c.open), float(c.high), float(c.low), float(c.close)
            v = float(c.volume)
        except (TypeError, ValueError):
            report["malformed"] += 1
            continue
        if not all(x == x for x in (o, h, l, cl, v)):  # NaN check
            report["malformed"] += 1
        if min(o, h, l, cl) <= 0:
            report["non_positive_price"] += 1
        if h < max(o, cl) or l > min(o, cl):
            report["ohlc_inconsistent"] += 1
        if v < 0:
            report["negative_volume"] += 1
        if c.timestamp > now + timedelta(minutes=5):
            report["future_bars"] += 1
        key = c.timestamp.isoformat()
        if key in seen:
            report["duplicates"] += 1
        seen.add(key)
        if prev_ts is not None:
            if c.timestamp <= prev_ts:
                report["unordered"] += 1
            delta = (c.timestamp - prev_ts).total_seconds()
            spacings.add(delta)
            if delta != step:
                if delta > 0 and delta % step == 0:
                    report["missing_intervals"] += int(delta / step) - 1
                    report["missing_locations"].append(
                        {"after": prev_ts.isoformat(), "gap_seconds": delta}
                    )
                else:
                    report["missing_intervals"] += 1
                    report["missing_locations"].append(
                        {"after": prev_ts.isoformat(), "gap_seconds": delta}
                    )
        prev_ts = c.timestamp
    report["spacing_values"] = sorted(spacings)
    for field in (
        "unordered",
        "duplicates",
        "malformed",
        "non_positive_price",
        "ohlc_inconsistent",
        "negative_volume",
        "future_bars",
    ):
        if report[field]:
            report["fatal"] = True
            report["fatal_reasons"].append(f"{field}={report[field]}")
    report["missing_locations"] = report["missing_locations"][:20]
    return report


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=None, help="reuse a saved dataset JSON")
    args = parser.parse_args()

    env = get_env()
    config = load_config(env=env)
    configure_logging(config, env, project_root=PROJECT_ROOT)

    banner("PHASE D.7 START")
    print(f"LOG_DIR={LOG_DIR}")
    emit("PHASE START", phase="D.7")

    # ---- safety preflight (historical market-data API allowed; order API forbidden)
    checks = {
        "trading_mode_paper": config.trading.mode == "paper",
        "execution_enabled_false": config.execution.enabled is False,
        "backtest_enabled": config.backtesting.enabled,
        "no_live_endpoint": not env.is_live_alpaca_endpoint(),
    }
    emit("SAFETY CHECKS", **checks)
    if not all(checks.values()):
        emit("SAFETY CHECK FAILED", level=logging.CRITICAL,
             failed=[k for k, v in checks.items() if not v])
        print("DO NOT RUN — SAFETY CHECK FAILED")
        return 2

    # ---- real data ---------------------------------------------------------
    requested_start: datetime | None = None
    requested_end: datetime | None = None
    source = "alpaca_crypto_bars"
    if args.dataset:
        rows = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
        candles = [candle_from_dict(r) for r in rows]
        source = f"saved_dataset:{args.dataset}"
    else:
        requested_end = utcnow()
        requested_start = requested_end - timedelta(days=LOOKBACK_DAYS)
        t0 = time.monotonic()
        candles = await fetch_alpaca_bars(
            SYMBOL,
            api_key=env.alpaca_api_key,
            api_secret=env.alpaca_api_secret,
            timeframe=TIMEFRAME,
            start=requested_start,
            end=requested_end,
        )
        emit("DATA_FETCHED", source=source, bars=len(candles),
             elapsed_s=round(time.monotonic() - t0, 2))
    dhash = dataset_hash(candles)
    emit("DATA_LOADED", source=source, symbol=SYMBOL, bars=len(candles),
         timeframe=TIMEFRAME, data_hash=dhash,
         first=candles[0].timestamp.isoformat() if candles else None,
         last=candles[-1].timestamp.isoformat() if candles else None)

    # ---- data quality ------------------------------------------------------
    quality = validate_data(candles, timeframe_seconds(TIMEFRAME), utcnow())
    quality.update({
        "source": source,
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "requested_start": requested_start.isoformat() if requested_start else None,
        "requested_end": requested_end.isoformat() if requested_end else None,
        "data_hash": dhash,
        "warm_up_minimum_bars": MIN_BARS,
        "gap_policy": "missing intervals are reported, never filled",
    })
    emit("DATA QUALITY", fatal=quality["fatal"], bars=quality["bars"],
         missing=quality["missing_intervals"], reasons=quality["fatal_reasons"])

    REPORTS.mkdir(parents=True, exist_ok=True)
    quality_path = REPORTS / "phase_d7_data_quality.json"
    quality_path.write_text(json.dumps(quality, indent=2, default=str) + "\n", encoding="utf-8")

    # persist dataset for reproducibility
    dataset_path = REPORTS / f"phase_d7_dataset_{dhash}.json"
    if not dataset_path.exists():
        dataset_path.write_text(
            json.dumps([candle_to_dict(c) for c in candles], default=str) + "\n",
            encoding="utf-8",
        )

    if quality["fatal"]:
        emit("PHASE END", phase="D.7", result="DATA-QUALITY BLOCKED")
        print("RESULT: DATA-QUALITY BLOCKED (classification D)")
        return 3

    # ---- run backtest on UNCHANGED D.6 engine -------------------------------
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

    # ---- robustness --------------------------------------------------------
    trades = result.trades
    by_day = breakdowns["by_day"]
    profitable_days = sum(1 for r in by_day.values() if r["net_pnl"] > 0)
    losing_days = sum(1 for r in by_day.values() if r["net_pnl"] < 0)
    dataset_days = sorted({c.timestamp.date().isoformat() for c in candles})
    flat_days = len(dataset_days) - len(by_day)
    forced = sum(1 for t in trades if t.get("exit_reason") in ("forced_exit", "session_closeout"))
    robustness = {
        "dataset_days": len(dataset_days),
        "active_trading_days": len(by_day),
        "profitable_days": profitable_days,
        "losing_days": losing_days,
        "flat_days": flat_days,
        "trades_per_day": {k: v["trades"] for k, v in sorted(by_day.items())},
        "best_day": max(by_day.items(), key=lambda kv: kv[1]["net_pnl"])[0] if by_day else None,
        "worst_day": min(by_day.items(), key=lambda kv: kv[1]["net_pnl"])[0] if by_day else None,
        "regime_distribution": {k: v["trades"] for k, v in breakdowns["by_regime"].items()},
        "direction_distribution": {k: v["trades"] for k, v in breakdowns["by_direction"].items()},
        "forced_exit_count": forced,
        "forced_exit_pct": round(100 * forced / len(trades), 2) if trades else 0.0,
    }

    # ---- accounting verification ------------------------------------------
    accounting_ok = all(c["consistent"] for c in result.accounting_checks)
    final_pm = result.accounting_checks[-1]["pm"] if result.accounting_checks else 0.0
    # Flat check uses the engine's own invariant tolerance (1e-9): the
    # in-kind fee model can leave sub-satoshi dust; exact == 0.0 would
    # misreport a flat book as non-flat.
    flat_ok = abs(final_pm) < 1e-9
    # Phantom-record integrity: every closed trade must carry a trade_id.
    # Empty {} records mean an exit was attributed with no open trade, which
    # also double-counts realized PnL in closed_pnls and invalidates headline
    # metrics. Any phantom record fails the run (measurement integrity).
    valid_trades = [t for t in result.trades if t.get("trade_id")]
    phantom_records = len(result.trades) - len(valid_trades)
    emit("ACCOUNTING VERIFICATION", checks=len(result.accounting_checks),
         all_consistent=accounting_ok, final_pm=final_pm, flat_ok=flat_ok,
         valid_trades=len(valid_trades), phantom_records=phantom_records)

    # ---- reproducibility ----------------------------------------------------
    result2 = BacktestEngine(config).run(candles)
    reproducible = (
        result.run_id == result2.run_id
        and result.metrics.net_pnl == result2.metrics.net_pnl
    )
    emit("REPRODUCIBILITY", run_id=result.run_id, reproducible=reproducible)

    # ---- safety postflight --------------------------------------------------
    post = {
        "execution_enabled": config.execution.enabled,
        "trading_mode": config.trading.mode,
        "live_trading_used": False,
    }

    # ---- classification (honest rubric; single period can never prove edge) --
    n = result.metrics.trade_count
    net = result.metrics.net_pnl
    if phantom_records > 0:
        classification = "E"  # infrastructure failure: headline metrics invalid
    elif n == 0:
        classification = "B"
    elif net <= 0:
        classification = "C"
    else:
        classification = "A"

    # ---- reports -------------------------------------------------------------
    payload = {
        "phase": "D.7",
        "run_id": result.run_id,
        "git_commit": result.git_commit,
        "config_hash": result.config_hash,
        "data_fingerprint": result.data_fingerprint,
        "data_hash": dhash,
        "dataset_file": str(dataset_path),
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "data_source": source,
        "requested_start": requested_start.isoformat() if requested_start else None,
        "requested_end": requested_end.isoformat() if requested_end else None,
        "actual_start": quality["first"],
        "actual_end": quality["last"],
        "bars": len(candles),
        "strategy": {"name": "breakout_momentum", "version": "1.0"},
        "execution_model": {
            "spread_percent": config.backtesting.execution.spread_percent,
            "slippage_percent": config.backtesting.execution.slippage_percent,
            "fee_percent": config.backtesting.execution.fee_percent,
            "fee_model": config.backtesting.execution.fee_model,
        },
        "metrics": result.metrics.to_dict(),
        "signals": result.signals,
        "rejections": result.rejections,
        "entries": result.entries,
        "valid_trades": len(valid_trades),
        "phantom_records": phantom_records,
        "flat_ok": flat_ok,
        "trades": trades,
        "breakdowns": breakdowns,
        "robustness": robustness,
        "accounting": {
            "checks": len(result.accounting_checks),
            "all_consistent": accounting_ok,
            "final_pm": final_pm,
        },
        "reproducibility": reproducible,
        "safety": post,
        "classification": classification,
    }
    (REPORTS / f"phase_d7_result_{result.run_id}.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (REPORTS / "phase_d7_result.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )

    integrity_ok = phantom_records == 0 and len(valid_trades) == n
    status = "PASS" if (accounting_ok and reproducible and flat_ok and integrity_ok) else "FAIL"
    reason = None if status == "PASS" else (
        "phantom trade records detected — headline metrics invalid" if not integrity_ok
        else "accounting divergence or non-flat final position" if not (accounting_ok and flat_ok)
        else "run not reproducible"
    )
    emit("PHASE END", phase="D.7", result=status, classification=classification, reason=reason)
    banner(f"PHASE D.7 END: {status} (classification {classification})")
    print(f"trades={n} valid={len(valid_trades)} phantom={phantom_records} "
          f"net_pnl={net} win_rate={result.metrics.win_rate} "
          f"max_dd={result.metrics.max_drawdown} final_pm={final_pm}")
    if reason:
        print(f"reason: {reason}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
