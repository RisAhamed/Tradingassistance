"""Phase H: offline walk-forward tuner (no live imports, deterministic)."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from app.backtesting.data import generate_history
from app.config.loader import get_env, load_config
from app.config.models import OptimizationScoringConfig
from app.optimization import scoring as scoring_mod
from app.optimization import tuner as tuner_mod
from app.optimization.proposals import build_proposal, validate_proposal, write_proposal
from app.optimization.scoring import score_candidate, score_window
from app.optimization.tuner import (
    apply_params,
    baseline_params,
    load_space,
    run_tuner,
    split_windows,
)

TINY_WINDOW = {"train_bars": 100, "validation_bars": 50, "step_bars": 50}
TINY_SPACE = {"name": "tiny", "params": {"strategy.breakout.lookback_periods": [10, 20]}}
WEIGHTS = {"net_pnl": 0.35, "profit_factor": 0.25, "max_drawdown": 0.20,
           "expectancy": 0.10, "stability": 0.10}


def _tiny_candles(n: int = 200):
    return generate_history("BTC/USD", timeframe="1m", length=n, seed=7,
                            start=datetime(2024, 1, 1, tzinfo=timezone.utc))


def _tiny_config():
    config = load_config(env=get_env())
    config.storage.enabled = False
    config.ai.enabled = False
    return config


# --- 1. space parser ------------------------------------------------------------
def test_space_rejects_unknown_parameter(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"name": "bad", "parameters": {"strategy.nope.x": [1]}}),
                   encoding="utf-8")
    with pytest.raises(ValueError, match="unknown parameter"):
        load_space(bad, _tiny_config())


def test_space_rejects_empty_values(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"name": "bad", "parameters": {"strategy.breakout.lookback_periods": []}}),
                   encoding="utf-8")
    with pytest.raises(ValueError, match="at least one value"):
        load_space(bad, _tiny_config())


# --- 2. weight validation ---------------------------------------------------------
def test_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1.0"):
        OptimizationScoringConfig.model_validate(
            {"net_pnl": 0.5, "profit_factor": 0.25, "max_drawdown": 0.20,
             "expectancy": 0.10, "stability": 0.10})


# --- 3. walk-forward split ----------------------------------------------------------
def test_split_windows_cover_without_val_overlap():
    candles = _tiny_candles(400)
    folds = split_windows(candles, train_bars=100, validation_bars=50, step_bars=50)
    assert len(folds) == 6  # starts 0,50,100,150,200,250 → last complete at 250
    for train, val in folds:
        assert len(train) == 100 and len(val) == 50
    for ( _t0, v0), (_t1, v1) in zip(folds, folds[1:]):
        assert v0[-1].timestamp < v1[0].timestamp  # adjacent, never overlapping
    assert folds[0][1][0].timestamp == candles[100].timestamp


def test_split_rejects_overlapping_step():
    with pytest.raises(ValueError, match="would overlap"):
        split_windows(_tiny_candles(400), train_bars=100, validation_bars=50, step_bars=25)


# --- 4. scoring purity ---------------------------------------------------------------
def test_scoring_pure_and_constant_has_no_penalty():
    m = {"net_pnl": 1000.0, "profit_factor": 2.0, "max_drawdown": 100.0, "expectancy": 50.0}
    assert score_window(m, WEIGHTS) == score_window(dict(m), WEIGHTS)
    agg = score_candidate([1.5, 1.5, 1.5])
    assert agg["std"] == 0.0 and agg["stability_penalty"] == 0.0
    assert agg["combined"] == agg["mean"] == 1.5


# --- 5. proposal schema ----------------------------------------------------------------
def test_proposal_schema_round_trip_and_location(tmp_path):
    proposal = build_proposal(
        data_hash="abc", reference_start="2024-01-01T00:00:00Z",
        space_file="x.yaml", space_content="{}", window_config=TINY_WINDOW,
        scoring_config=WEIGHTS,
        baseline={"params": {}, "score": 0.0, "metrics": []},
        candidates=[], recommended={"params": {}, "score": 0.0, "reason": "none"},
        git_commit="deadbeef", config_hash="feedface", command="cmd")
    assert validate_proposal(proposal) == []
    path = write_proposal(proposal, tmp_path)
    assert path.parent == tmp_path
    assert {p.name for p in tmp_path.iterdir()} == {path.name}
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == proposal["run_id"]


# --- 6. determinism -----------------------------------------------------------------------
def test_two_runs_are_byte_identical_modulo_generated_at(tmp_path):
    config = _tiny_config()
    candles = _tiny_candles()

    def _run():
        proposal, _ = run_tuner(
            candles=candles, space=dict(TINY_SPACE), base_config=config,
            window=dict(TINY_WINDOW), weights=dict(WEIGHTS), command="test",
            reference_start="2024-01-01T00:00:00Z", space_file="tiny.yaml",
            space_content="{}", output_dir=tmp_path, top=5, workers=1)
        proposal = dict(proposal)
        proposal["generated_at"] = "MASKED"
        return proposal

    first, second = _run(), _run()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- 7. live path untouched ------------------------------------------------------------------
def test_tuner_never_touches_the_live_engine():
    import ast

    for module in ("app.optimization.tuner", "app.optimization.scoring",
                   "app.optimization.proposals", "app.optimization.__main__"):
        source = Path(tuner_mod.__file__).parent.joinpath(
            module.split(".")[-1] + ".py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any("runner" in name or name == "app.runtime" for name in imported), module
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.');"
         "import app.optimization.tuner, app.optimization.__main__;"
         "assert 'app.runner.engine' not in sys.modules, 'live engine imported';"
         "print('live-engine-free')"],
        capture_output=True, text=True, cwd=str(Path.cwd()), timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "live-engine-free" in out.stdout


# --- 8. baseline inclusion ----------------------------------------------------------------------
def test_shipped_space_includes_current_yaml_values():
    from app.config.loader import PROJECT_ROOT

    config = _tiny_config()
    spaces_dir = Path(config.optimization.spaces_dir)
    if not spaces_dir.is_absolute():
        spaces_dir = PROJECT_ROOT / spaces_dir
    space = load_space(spaces_dir / "breakout_baseline.yaml", config)
    assert set(space["params"]) == {
        "strategy.breakout.lookback_periods",
        "strategy.breakout.buffer_percent",
        "strategy.momentum.rsi_min_long",
        "strategy.momentum.rsi_max_long",
    }
    assert len(space["params"]) == 4
    assert all(len(v) == 3 for v in space["params"].values())
    current = baseline_params(space, config)
    assert current["strategy.breakout.lookback_periods"] == 20
    assert current["strategy.breakout.buffer_percent"] == 0.05
    assert current["strategy.momentum.rsi_min_long"] == 55
    assert current["strategy.momentum.rsi_max_long"] == 75
