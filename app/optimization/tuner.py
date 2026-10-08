"""Walk-forward parameter tuner (Phase H).

Offline only: runs the existing :class:`BacktestEngine` over rolling
train/validation windows for every candidate in a space file, scores the
validation metrics, and emits ONE ranked JSON proposal under
``logs/proposals/``. Never touches live trading, ``configs/config.yaml``,
the network, or :class:`TradingEngine`.

Split rule (walk-forward geometry): fold ``i`` (0-based) trains on
``data[i*step : i*step+train_bars]`` and validates on the immediately
following ``validation_bars``
(``data[i*step+train_bars : i*step+train_bars+validation_bars]``). A fold
is kept only when both slices are complete. With
``step_bars == validation_bars`` consecutive validation slices are
adjacent and never overlap; with ``step_bars > validation_bars`` a gap
is left between them; ``step_bars < validation_bars`` would overlap and
is therefore rejected at load time.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.backtesting.data import generate_history, load_csv
from app.backtesting.engine import BacktestEngine
from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.config.models import AppConfig
from app.domain.market import Candle
from app.optimization.proposals import build_proposal, write_proposal
from app.optimization.scoring import score_candidate, score_window

_EPS = 1e-9


# -- space file ---------------------------------------------------------------
def load_space(space_path: str | Path, config: AppConfig) -> dict[str, Any]:
    """Parse + validate a space file. Returns ``{name, params}``.

    Every dotted path must resolve to a settable leaf on ``AppConfig``;
    unknown paths and empty value lists fail loudly. No implicit search:
    only values listed here are ever swept.
    """
    path = Path(space_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    name = raw.get("name") or path.stem
    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError(f"space file '{path}' must define a non-empty 'parameters' mapping")
    validated: dict[str, list[Any]] = {}
    for dotted, values in parameters.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"space file '{path}': '{dotted}' must list at least one value")
        _resolve_leaf(config, dotted)  # raises on unknown paths
        validated[str(dotted)] = list(values)
    return {"name": name, "params": validated}


def _resolve_leaf(config: AppConfig, dotted: str) -> tuple[Any, str]:
    """Walk ``dotted`` on the config; return (parent, leaf). Raise on miss."""
    node: Any = config
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not hasattr(node, part):
            raise ValueError(f"unknown parameter '{dotted}' (no '{part}' on {type(node).__name__})")
        node = getattr(node, part)
    leaf = parts[-1]
    if not hasattr(node, leaf):
        raise ValueError(f"unknown parameter '{dotted}' (no '{leaf}' on {type(node).__name__})")
    return node, leaf


def apply_params(config: AppConfig, params: dict[str, Any]) -> AppConfig:
    """Return a deep copy of ``config`` with dotted overrides applied."""
    copied = config.model_copy(deep=True)
    for dotted, value in params.items():
        node, leaf = _resolve_leaf(copied, dotted)
        setattr(node, leaf, value)
    return copied


def baseline_params(space: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    """Current yaml values for every swept leaf (always a candidate)."""
    out: dict[str, Any] = {}
    for dotted in space["params"]:
        node, leaf = _resolve_leaf(config, dotted)
        out[dotted] = getattr(node, leaf)
    return out


# -- walk-forward split ----------------------------------------------------------
def split_windows(
    candles: list[Candle], *, train_bars: int, validation_bars: int, step_bars: int
) -> list[tuple[list[Candle], list[Candle]]]:
    """Rolling (train, validation) folds per the module split rule."""
    if step_bars < validation_bars:
        raise ValueError(
            f"step_bars ({step_bars}) < validation_bars ({validation_bars}) "
            "would overlap validation slices; refusing"
        )
    folds: list[tuple[list[Candle], list[Candle]]] = []
    start = 0
    while True:
        train = candles[start:start + train_bars]
        val = candles[start + train_bars:start + train_bars + validation_bars]
        if len(train) < train_bars or len(val) < validation_bars:
            break
        folds.append((train, val))
        start += step_bars
    return folds


# -- execution ------------------------------------------------------------------
def run_window(candles: list[Candle], config: AppConfig) -> dict[str, Any]:
    """Run one BacktestEngine window; return its metrics dict."""
    result = BacktestEngine(config).run(candles)
    return dict(result.to_dict().get("metrics", {}))


def evaluate_candidate(
    params: dict[str, Any],
    folds: list[tuple[list[Candle], list[Candle]]],
    base_config: AppConfig,
    weights: dict[str, float],
) -> dict[str, Any]:
    """Score one candidate across folds (train + validation each)."""
    config = apply_params(base_config, params)
    per_window: list[dict[str, Any]] = []
    fold_scores: list[float] = []
    for train_candles, val_candles in folds:
        train_metrics = run_window(train_candles, config)
        val_metrics = run_window(val_candles, config)
        train_score = score_window(train_metrics, weights)
        val_score = score_window(val_metrics, weights)
        denom = max(abs(train_score), abs(val_score), _EPS)
        fold_score = val_score - float(weights.get("stability", 0.0)) * abs(train_score - val_score) / denom
        per_window.append({
            "train_metrics": train_metrics,
            "validation_metrics": val_metrics,
            "train_score": train_score,
            "validation_score": val_score,
            "fold_score": fold_score,
        })
        fold_scores.append(fold_score)
    aggregate = score_candidate(fold_scores)
    return {"params": dict(params), "per_window": per_window, **aggregate, "score": aggregate["combined"]}


def data_hash(candles: list[Candle]) -> str:
    body = "\n".join(
        f"{c.timestamp.isoformat()}|{c.open}|{c.high}|{c.low}|{c.close}|{c.volume}"
        for c in candles
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=str(PROJECT_ROOT), timeout=15,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - offline-safe fallback
        return "unknown"


def _run_window_task(payload: dict[str, Any]) -> dict[str, Any]:
    """Multiprocessing worker: pure function of (candles, config, split)."""
    from app.config.models import AppConfig as _AppConfig
    from app.domain.market import Candle as _Candle

    config = _AppConfig.model_validate(payload["config"])
    train = [_Candle(**c) for c in payload["train"]]
    val = [_Candle(**c) for c in payload["val"]]
    train_metrics = run_window(train, config)
    val_metrics = run_window(val, config)
    return {"train_metrics": train_metrics, "validation_metrics": val_metrics}


def run_tuner(
    *,
    candles: list[Candle],
    space: dict[str, Any],
    base_config: AppConfig,
    window: dict[str, int],
    weights: dict[str, float],
    command: str,
    reference_start: str | None,
    space_file: str,
    space_content: str,
    output_dir: str | Path,
    top: int = 5,
    workers: int = 1,
) -> tuple[dict[str, Any], Path]:
    """Evaluate every candidate; write + return the ranked proposal."""
    folds = split_windows(
        candles,
        train_bars=int(window["train_bars"]),
        validation_bars=int(window["validation_bars"]),
        step_bars=int(window["step_bars"]),
    )
    if not folds:
        raise ValueError("data too short for even one complete train/validation fold")
    combos = [dict(zip(space["params"].keys(), combo, strict=False))
              for combo in itertools.product(*space["params"].values())]
    baseline = baseline_params(space, base_config)

    if workers and workers > 1:
        from concurrent.futures import ProcessPoolExecutor

        tasks: list[dict[str, Any]] = []
        order: list[tuple[int, int]] = []
        payload_configs = [apply_params(base_config, dict(baseline))]
        payload_configs.extend(apply_params(base_config, dict(c)) for c in combos)
        for ci, cfg in enumerate(payload_configs):
            dump = cfg.model_dump(mode="json")
            for fi, (train, val) in enumerate(folds):
                tasks.append({
                    "config": dump,
                    "train": [c.model_dump(mode="json") for c in train],
                    "val": [c.model_dump(mode="json") for c in val],
                })
                order.append((ci, fi))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_run_window_task, tasks))
        by_candidate: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        for (ci, fi), res in zip(order, results, strict=True):
            by_candidate.setdefault(ci, []).append((fi, res))
        evaluated: list[dict[str, Any]] = []
        for ci, param_set in enumerate([baseline, *combos]):
            fold_rows = [row for _, row in sorted(by_candidate[ci], key=lambda t: t[0])]
            per_window, fold_scores = [], []
            for row in fold_rows:
                train_score = score_window(row["train_metrics"], weights)
                val_score = score_window(row["validation_metrics"], weights)
                denom = max(abs(train_score), abs(val_score), _EPS)
                fold_score = val_score - float(weights.get("stability", 0.0)) * abs(train_score - val_score) / denom
                per_window.append({
                    "train_metrics": row["train_metrics"],
                    "validation_metrics": row["validation_metrics"],
                    "train_score": train_score,
                    "validation_score": val_score,
                    "fold_score": fold_score,
                })
                fold_scores.append(fold_score)
            aggregate = score_candidate(fold_scores)
            evaluated.append({"params": dict(param_set), "per_window": per_window,
                              **aggregate, "score": aggregate["combined"]})
    else:
        evaluated = [evaluate_candidate(dict(baseline), folds, base_config, weights)]
        evaluated.extend(evaluate_candidate(dict(c), folds, base_config, weights) for c in combos)

    baseline_result, candidates = evaluated[0], evaluated[1:]
    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0] if candidates else baseline_result
    beaten = best["score"] > baseline_result["score"]
    recommended = {
        "params": best["params"],
        "score": best["score"],
        "reason": (
            f"top walk-forward score {best['score']:.4f} vs baseline "
            f"{baseline_result['score']:.4f} ({'beats' if beaten else 'does not beat'} current config)"
        ),
    }
    scored_baseline = {
        "params": baseline_result["params"],
        "score": baseline_result["score"],
        "metrics": [w["validation_metrics"] for w in baseline_result["per_window"]],
    }
    for cand in candidates:
        cand["vs_baseline"] = {
            "score_delta": cand["score"] - baseline_result["score"],
            "beats_baseline": cand["score"] > baseline_result["score"],
        }
    proposal = build_proposal(
        data_hash=data_hash(candles),
        reference_start=reference_start,
        space_file=space_file,
        space_content=space_content,
        window_config=dict(window),
        scoring_config=dict(weights),
        baseline=scored_baseline,
        candidates=candidates,
        recommended=recommended,
        git_commit=git_commit(),
        config_hash=hashlib.sha256(
            json.dumps(base_config.model_dump(mode="json"), sort_keys=True, default=str).encode()
        ).hexdigest(),
        command=command,
    )
    path = write_proposal(proposal, output_dir)
    return proposal, path


# -- CLI -------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None):
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Offline walk-forward strategy tuner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--data", type=str, default=None, help="CSV with timestamp,open,high,low,close,volume")
    group.add_argument("--synthetic", type=int, default=None, help="synthetic candle count")
    parser.add_argument("--space", type=str, required=True, help="space name under spaces_dir (no extension needed)")
    parser.add_argument("--reference-start", type=str, default=None,
                        help="fixed ISO-8601 data anchor (overrides config)")
    parser.add_argument("--top", type=int, default=5, help="candidates to print")
    parser.add_argument("--workers", type=int, default=None,
                        help="parallel workers (default: cpu count, capped at 8)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate the space and print the candidate count only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import itertools
    import os
    import sys

    args = parse_args(argv)
    env = get_env()
    config = load_config(env=env)
    if config.trading.mode != "paper":
        print("REFUSED: tuner runs only with trading.mode=paper", file=sys.stderr)
        return 2
    config.optimization.enabled = True  # in-process only; yaml untouched

    spaces_dir = Path(config.optimization.spaces_dir)
    if not spaces_dir.is_absolute():
        spaces_dir = PROJECT_ROOT / spaces_dir
    space_path = spaces_dir / args.space if args.space.endswith(".yaml") else spaces_dir / f"{args.space}.yaml"
    space = load_space(space_path, config)

    combos = list(itertools.product(*space["params"].values()))
    if args.dry_run:
        print(f"space={space['name']} candidates={len(combos)}")
        for dotted, values in space["params"].items():
            print(f"  {dotted}: {values}")
        return 0

    timeframe = config.timeframes.signal
    if args.data:
        candles = load_csv(args.data, symbol=config.trading.symbol, timeframe=timeframe)
        reference_start: str | None = None
        source = args.data
    else:
        anchor = args.reference_start or config.backtesting.reference_start
        if not anchor:
            print("REFUSED: --synthetic requires a fixed anchor: pass --reference-start "
                  "or set backtesting.reference_start", file=sys.stderr)
            return 2
        reference_start = anchor
        start = datetime.fromisoformat(str(anchor).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        candles = generate_history(config.trading.symbol, timeframe=timeframe,
                                   length=args.synthetic, seed=7, start=start)
        source = f"synthetic(length={args.synthetic}, reference_start={anchor})"

    weights = dict(config.optimization.scoring.model_dump(mode="json"))
    window = dict(config.optimization.window.model_dump(mode="json"))
    workers = args.workers if args.workers else min(8, os.cpu_count() or 1)
    data_arg = f"--data {args.data}" if args.data else f"--synthetic {args.synthetic}"
    command = f"python -m app.optimization.tuner {data_arg} --space {args.space}"
    proposal, path = run_tuner(
        candles=candles, space=space, base_config=config, window=window,
        weights=weights, command=command, reference_start=reference_start,
        space_file=str(space_path), space_content=space_path.read_text(encoding="utf-8"),
        output_dir=PROJECT_ROOT / config.optimization.output_dir,
        top=args.top, workers=workers,
    )
    print(f"source={source} folds_config={window}")
    print(f"{'rank':<5}{'score':<10}{'params'}")
    for i, cand in enumerate(proposal["candidates"][: max(1, args.top)], start=1):
        params = ", ".join(f"{k.split('.')[-1]}={v}" for k, v in cand["params"].items())
        print(f"{i:<5}{cand['score']:<10.4f}{params}")
    base = proposal["baseline"]
    print(f"baseline score={base['score']:.4f} params={base['params']}")
    print(f"recommended: {proposal['recommended']['reason']}")
    print(f"proposal: {path}")
    return 0


if __name__ == "__main__":
    import sys as _sys

    raise SystemExit(main())
