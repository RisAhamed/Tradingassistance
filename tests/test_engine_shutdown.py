"""Graceful-shutdown safety (TASK 1) and single-source-of-truth (TASK 4) tests.

* ``engine.stop()`` must flatten an open position BEFORE the broker is
  disconnected whenever execution is enabled, so a SIGINT/SIGTERM/normal exit
  never leaves an unprotected position open.
* ``paper_trade.yml`` must run the standard entry point and must not contain any
  command that overrides the checked-in configuration.
"""
from __future__ import annotations

import pytest
import yaml

from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.clock import utcnow
from app.core.logging import configure_logging, reset_logging_state
from app.domain.enums import Side
from app.domain.orders import Fill
from app.runtime import build_runtime

SYMBOL = "BTC/USD"


def _build_runtime(*, execution: bool):
    reset_logging_state()
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.execution.enabled = execution
    config.market_data.max_future_skew_seconds = 86400 * 30
    configure_logging(config, env, project_root=PROJECT_ROOT)
    return build_runtime(config, env)


def _open_position(engine) -> None:
    engine.position_manager.apply_fill(
        Fill(
            fill_id="f-open",
            order_id="o-open",
            timestamp=utcnow(),
            symbol=SYMBOL,
            side=Side.BUY,
            quantity=0.001,
            price=30000.0,
        )
    )


class _FlattenSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, *, force: bool = False, session_closeout: bool = True) -> bool:
        self.calls.append({"force": force, "session_closeout": session_closeout})
        return True


@pytest.mark.asyncio
async def test_stop_flattens_open_position_when_execution_enabled(monkeypatch):
    runtime = _build_runtime(execution=True)
    engine = runtime.engine
    _open_position(engine)
    assert engine.position_manager.is_flat is False

    spy = _FlattenSpy()
    monkeypatch.setattr(engine, "flatten", spy)

    await engine.stop()

    assert spy.calls == [{"force": False, "session_closeout": True}]


@pytest.mark.asyncio
async def test_stop_does_not_flatten_when_already_flat(monkeypatch):
    runtime = _build_runtime(execution=True)
    engine = runtime.engine
    assert engine.position_manager.is_flat is True

    spy = _FlattenSpy()
    monkeypatch.setattr(engine, "flatten", spy)

    await engine.stop()

    assert spy.calls == []


@pytest.mark.asyncio
async def test_stop_does_not_flatten_when_execution_disabled(monkeypatch):
    runtime = _build_runtime(execution=False)
    engine = runtime.engine
    _open_position(engine)
    assert engine.position_manager.is_flat is False

    spy = _FlattenSpy()
    monkeypatch.setattr(engine, "flatten", spy)

    await engine.stop()

    assert spy.calls == []


def _paper_trade_run_blocks() -> list[str]:
    path = PROJECT_ROOT / ".github" / "workflows" / "paper_trade.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    blocks: list[str] = []
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            if "run" in step:
                blocks.append(step["run"])
    return blocks


def test_paper_trade_workflow_uses_the_standard_entrypoint():
    blocks = _paper_trade_run_blocks()
    assert any("python -m app" in block for block in blocks), (
        "paper_trade.yml must run the standard entry point (python -m app)"
    )


def test_paper_trade_workflow_does_not_override_config():
    forbidden = (
        "config.yaml",
        "configs/config",
        "execution.enabled",
        "execution_enabled",
        "sed -i",
        "python -c",
        "> configs",
    )
    for block in _paper_trade_run_blocks():
        lowered = block.lower()
        for token in forbidden:
            assert token not in lowered, (
                f"paper_trade.yml must not override configuration (found {token!r})"
            )
