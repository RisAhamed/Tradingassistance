"""Phase I: proposal lifecycle — overlay, permissions, apply, endpoints."""
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ai_supervisor.tools import build_gateway
from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.errors import ToolPermissionError
from app.optimization import apply as apply_mod
from app.runtime import build_runtime

CONFIG_YAML = PROJECT_ROOT / "configs" / "config.yaml"
OVERLAY_YAML = PROJECT_ROOT / "configs" / "applied_overrides.yaml"


@pytest.fixture(autouse=True)
def _config_yaml_untouched():
    before = hashlib.sha256(CONFIG_YAML.read_bytes()).hexdigest()
    yield
    assert hashlib.sha256(CONFIG_YAML.read_bytes()).hexdigest() == before, \
        "configs/config.yaml must never change in Phase I tests"
    if OVERLAY_YAML.exists():
        OVERLAY_YAML.unlink()


def _config(tmp_path=None, **overrides):
    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.execution.enabled = False
    config.market_data.max_future_skew_seconds = 86400 * 30
    if tmp_path is not None:
        base = Path(tmp_path) / "proposals"
        config.optimization.output_dir = str(base)
        config.optimization.pending_dir = str(base / "pending")
        config.optimization.applied_dir = str(base / "applied")
        config.optimization.rejected_dir = str(base / "rejected")
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _engine(tmp_path=None, **overrides):
    return build_runtime(_config(tmp_path, **overrides), get_env()).engine


def _tuner_proposal(params=None):
    return {
        "run_id": "abc123",
        "generated_at": "2026-10-08T00:00:00+00:00",
        "origin": "tuner",
        "author": None,
        "git_commit": "x",
        "data_hash": "y",
        "reference_start": None,
        "space_file": "s.yaml",
        "window_config": {},
        "scoring_config": {},
        "baseline": {"params": {}, "score": 0.0, "metrics": []},
        "candidates": [],
        "recommended": {"params": params or {"strategy.breakout.lookback_periods": 10},
                        "score": 1.0, "reason": "test"},
        "reproducibility": {},
    }


def _write_pending(engine, proposal):
    pending = Path(engine.config.optimization.pending_dir)
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / f"{proposal['run_id']}.json"
    path.write_text(json.dumps(proposal), encoding="utf-8")
    return proposal["run_id"]


# --- 1. overlay loader ----------------------------------------------------------
def test_no_overlay_behaves_like_today(tmp_path):
    engine = _engine(tmp_path)
    assert engine.config.risk.risk_per_trade_percent == 0.5


def test_overlay_merges_and_invalid_overlay_fails(tmp_path):
    engine = _engine(tmp_path)
    OVERLAY_YAML.write_text("risk:\n  risk_per_trade_percent: 0.25\n", encoding="utf-8")
    try:
        merged = load_config(env=get_env())
        assert merged.risk.risk_per_trade_percent == 0.25
    finally:
        OVERLAY_YAML.unlink(missing_ok=True)
    OVERLAY_YAML.write_text("risk:\n  risk_per_trade_percent: [nope]\n", encoding="utf-8")
    try:
        with pytest.raises(Exception, match="invalid configuration"):
            load_config(env=get_env())
    finally:
        OVERLAY_YAML.unlink(missing_ok=True)


# --- 3/4. propose tool ------------------------------------------------------------
@pytest.mark.asyncio
async def test_propose_writes_pending_and_validates(tmp_path):
    engine = _engine(tmp_path)
    gateway = build_gateway(engine)
    out = await gateway.call(
        "propose_strategy_change",
        {"summary": "s", "params": {"strategy.breakout.lookback_periods": 10},
         "rationale": "r"},
        requester="ai")
    assert out["origin"] == "ai"
    pending = list(Path(engine.config.optimization.pending_dir).glob("*.json"))
    assert len(pending) == 1
    stored = json.loads(pending[0].read_text(encoding="utf-8"))
    assert stored["origin"] == "ai" and stored["params"] == {"strategy.breakout.lookback_periods": 10}
    assert not OVERLAY_YAML.exists()
    assert (PROJECT_ROOT / "configs" / "config.yaml").exists()  # untouched (hashed in teardown)
    with pytest.raises(ToolPermissionError):
        await gateway.call(
            "propose_strategy_change",
            {"summary": "s", "params": {"strategy.nope.x": 1}, "rationale": "r"},
            requester="ai")
    with pytest.raises(ToolPermissionError):
        await gateway.call(
            "propose_strategy_change",
            {"summary": "s", "params": {"risk.risk_per_trade_percent": -1}, "rationale": "r"},
            requester="ai")


@pytest.mark.asyncio
async def test_propose_denied_when_flag_off(tmp_path):
    engine = _engine(tmp_path)
    engine.config.ai.permissions.allow_propose_strategy_changes = False
    gateway = build_gateway(engine)
    with pytest.raises(ToolPermissionError):
        await gateway.call(
            "propose_strategy_change",
            {"summary": "s", "params": {"strategy.breakout.lookback_periods": 10}, "rationale": "r"},
            requester="ai")
    assert list(Path(engine.config.optimization.pending_dir).glob("*.json")) == []


# --- 5. AI apply refused by default --------------------------------------------------
@pytest.mark.asyncio
async def test_ai_apply_refused_by_default(tmp_path):
    engine = _engine(tmp_path)
    _write_pending(engine, _tuner_proposal())
    gateway = build_gateway(engine)
    with pytest.raises(ToolPermissionError):
        await gateway.call("apply_proposal", {"proposal_id": "abc123"}, requester="ai")
    assert not OVERLAY_YAML.exists()


# --- 6/8/9. human apply / backup / reject ----------------------------------------------
@pytest.mark.asyncio
async def test_human_apply_writes_overlay_moves_and_idempotent(tmp_path):
    engine = _engine(tmp_path)
    pid = _write_pending(engine, _tuner_proposal())
    out = await apply_mod.apply_proposal(engine, pid, actor="human:test")
    assert out == {"applied": True, "restart_required": True, "proposal_id": pid,
                   "halted_first": False, "overlay": str(OVERLAY_YAML)}
    assert OVERLAY_YAML.exists()
    applied = list(Path(engine.config.optimization.applied_dir).glob("*.json"))
    assert {p.name for p in applied} == {f"{pid}.json", f"{pid}.applied.json"}
    assert not list(Path(engine.config.optimization.pending_dir).glob("*.json"))
    again = await apply_mod.apply_proposal(engine, pid, actor="human:test")
    assert again == {"applied": False, "reason": "already_applied", "proposal_id": pid}

    pid2 = "def456"
    second = _tuner_proposal(params={"strategy.breakout.lookback_periods": 30})
    second["run_id"] = pid2
    _write_pending(engine, second)
    await apply_mod.apply_proposal(engine, pid2, actor="human:test")
    backups = list(Path(engine.config.optimization.applied_dir).glob("*_backup.yaml"))
    assert len(backups) == 1 and pid2 in backups[0].name


@pytest.mark.asyncio
async def test_reject_moves_file_and_changes_nothing(tmp_path):
    engine = _engine(tmp_path)
    pid = _write_pending(engine, _tuner_proposal())
    out = await apply_mod.reject_proposal(engine, pid, actor="human:test")
    assert out == {"rejected": True, "proposal_id": pid}
    assert list(Path(engine.config.optimization.rejected_dir).glob("*.json"))
    assert not OVERLAY_YAML.exists()


# --- 7. halt before write ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_halt_precedes_overlay_write(tmp_path):
    engine = _engine(tmp_path)
    engine.config.execution.enabled = True
    order: list[str] = []
    real_halt = engine.halt_experiment

    async def _spy(reason: str, *, operator: str = "system"):
        order.append("halt")
        await real_halt(reason, operator=operator)

    engine.halt_experiment = _spy  # type: ignore[method-assign]
    pid = _write_pending(engine, _tuner_proposal())
    out = await apply_mod.apply_proposal(engine, pid, actor="human:test")
    assert out["applied"] is True and out["halted_first"] is True
    assert order == ["halt"]
    assert OVERLAY_YAML.exists()


# --- 10/13. endpoints ----------------------------------------------------------------------
def _client(tmp_path):
    from app.main import create_app

    return TestClient(create_app(config=_config(tmp_path), autostart=False))


def test_apply_reject_require_confirm(tmp_path):
    with _client(tmp_path) as client:
        assert client.post("/api/proposals/x/apply").status_code == 400
        assert client.post("/api/proposals/x/reject").status_code == 400


def test_proposal_endpoints_round_trip(tmp_path):
    from app.main import create_app

    config = _config(tmp_path)
    with TestClient(create_app(config=config, autostart=False)) as client:
        assert client.get("/api/proposals").json() == {"proposals": []}
        assert client.get("/api/proposals/nope").status_code == 404


def test_review_endpoint_triggers_investigation(tmp_path):
    with _client(tmp_path) as client:
        out = client.post("/api/ai/review_optimization")
        assert out.status_code == 200
        assert out.json()["investigated"] is True


def test_dashboard_has_proposals_card(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/dashboard").text
        assert 'id="proposals"' in html


# --- 11. tool visibility ----------------------------------------------------------------------
def test_ai_tool_visibility_and_default_deny(tmp_path):
    engine = _engine(tmp_path)
    gateway = build_gateway(engine)
    names = gateway.names()
    assert "list_proposals" in names and "get_proposal" in names
    assert "apply_proposal" in names  # registered but denied below


@pytest.mark.asyncio
async def test_list_and_get_tools(tmp_path):
    engine = _engine(tmp_path)
    _write_pending(engine, _tuner_proposal())
    gateway = build_gateway(engine)
    listed = await gateway.call("list_proposals", {}, requester="ai")
    assert listed and listed[0]["proposal_id"] == "abc123"
    detail = await gateway.call("get_proposal", {"proposal_id": "abc123"}, requester="ai")
    assert detail["proposal"]["origin"] == "tuner"
    assert await gateway.call("get_proposal", {"proposal_id": "missing"}, requester="ai") is None


# --- 12. end-to-end ------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tiny_tuner_proposal_applies_and_reloads(tmp_path):
    from datetime import datetime, timezone

    from app.backtesting.data import generate_history
    from app.optimization.tuner import run_tuner

    config = _config(tmp_path)
    candles = generate_history("BTC/USD", timeframe="1m", length=400, seed=7,
                               start=datetime(2024, 1, 1, tzinfo=timezone.utc))
    space = {"name": "tiny",
             "params": {"strategy.breakout.lookback_periods": [10, 20]}}
    proposal, written_path = run_tuner(
        candles=candles, space=space, base_config=config,
        window={"train_bars": 200, "validation_bars": 100, "step_bars": 100},
        weights={"net_pnl": 0.35, "profit_factor": 0.25, "max_drawdown": 0.20,
                 "expectancy": 0.10, "stability": 0.10},
        command="test", reference_start="2024-01-01T00:00:00Z",
        space_file="tiny.yaml", space_content="{}",
        output_dir=config.optimization.output_dir, top=5, workers=1)
    # move the proposal into pending and apply through the shared routine
    pending = Path(config.optimization.pending_dir)
    pending.mkdir(parents=True, exist_ok=True)
    pid = proposal["run_id"]
    assert written_path.exists(), "tuner must have written the proposal"
    written_path.rename(pending / f"{pid}.json")
    engine = build_runtime(config, get_env()).engine
    out = await apply_mod.apply_proposal(engine, pid, actor="human:test")
    assert out["applied"] is True
    try:
        merged = load_config(env=get_env())
        expected = proposal["recommended"]["params"]["strategy.breakout.lookback_periods"]
        assert merged.strategy.breakout.lookback_periods == expected
    finally:
        OVERLAY_YAML.unlink(missing_ok=True)
