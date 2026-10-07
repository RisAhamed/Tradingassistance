"""Controlled experiment lifecycle tests (first paper smoke experiment).

State machine, authorization validation, run-scoped arming/expiry, kill
tripwires, dashboard API surface. Mock only — no orders, no broker contact
beyond in-memory mock, execution never enabled for a trading session.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config.loader import get_env, load_config
from app.core.clock import utcnow
from app.experiment.manager import ExperimentError, ExperimentManager, ExperimentState
from app.main import create_app


def _config(**overrides):
    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.market_data.max_future_skew_seconds = 86400 * 30
    for key, value in overrides.items():
        parts = key.split(".")
        obj = config
        for part in parts[:-1]:
            obj = getattr(obj, part)
        setattr(obj, parts[-1], value)
    return config


def _manager(**overrides) -> ExperimentManager:
    config = _config(**overrides)
    manager = ExperimentManager(config, run_id="run-test-1")
    manager.mark_observing()
    manager.mark_ready_for_authorization()
    return manager


def _authorized(**overrides) -> ExperimentManager:
    manager = _manager(**overrides)
    manager.config.paper_safety.enabled = True
    return manager


# --- state machine ---------------------------------------------------------------
def test_initial_state_is_preparing():
    config = _config()
    manager = ExperimentManager(config, run_id="r")
    assert manager.state is ExperimentState.PREPARING
    assert manager.envelope is None


def test_observing_to_ready_transitions():
    config = _config()
    manager = ExperimentManager(config, run_id="r")
    manager.mark_observing()
    assert manager.state is ExperimentState.OBSERVING
    manager.mark_ready_for_authorization()
    assert manager.state is ExperimentState.READY_FOR_AUTHORIZATION


def test_arm_requires_authorized_state():
    manager = _manager()
    with pytest.raises(ExperimentError):
        manager.arm()


# --- authorization validation ----------------------------------------------------------
def test_authorize_freezes_immutable_envelope():
    manager = _authorized()
    envelope = manager.authorize(operator="op-human", window_minutes=30)
    assert manager.state is ExperimentState.AUTHORIZED
    assert envelope.operator == "op-human"
    assert envelope.window_minutes == 30
    assert envelope.caps["max_qty_per_order"] == 0.001
    assert envelope.trading_mode == "paper"
    with pytest.raises(Exception):
        envelope.operator = "mallory"  # frozen dataclass


def test_authorize_requires_operator_and_sane_window():
    for kwargs in ({"operator": "", "window_minutes": 30},
                   {"operator": "op", "window_minutes": 2},
                   {"operator": "op", "window_minutes": 500}):
        with pytest.raises(ExperimentError):
            _authorized().authorize(**kwargs)


def test_authorize_validates_cap_values_not_flag():
    manager = _manager()  # paper_safety disabled by default: values still validated
    manager.config.paper_safety.max_qty_per_order = 0.0
    with pytest.raises(ExperimentError, match="invalid cap"):
        manager.authorize(operator="op", window_minutes=30)


def test_arm_enables_caps_run_scoped_and_disarm_restores():
    manager = _manager()  # caps disabled: the arm path enables them in-memory
    manager.authorize(operator="op", window_minutes=30)
    assert manager.config.paper_safety.enabled is False
    manager.arm()
    assert manager.config.paper_safety.enabled is True
    manager.disarm(reason="test")
    assert manager.config.paper_safety.enabled is False
    assert manager.config.execution.enabled is False


def test_authorize_refuses_when_execution_already_on():
    manager = _authorized(**{"execution.enabled": True})
    with pytest.raises(ExperimentError, match="already enabled"):
        manager.authorize(operator="op", window_minutes=30)


def test_double_authorize_refused():
    manager = _authorized()
    manager.authorize(operator="op", window_minutes=30)
    with pytest.raises(ExperimentError):
        manager.authorize(operator="op2", window_minutes=30)


# --- arming / expiry --------------------------------------------------------------------------
def test_arm_enables_execution_run_scoped_only():
    manager = _authorized()
    manager.authorize(operator="op", window_minutes=30)
    assert manager.config.execution.enabled is False
    manager.arm()
    assert manager.state is ExperimentState.ARMED
    assert manager.config.execution.enabled is True


def test_expiry_disarms():
    manager = _authorized()
    manager.authorize(operator="op", window_minutes=5)
    manager.arm()
    future = utcnow() + timedelta(minutes=6)
    assert manager.check_expiry(future) is True
    manager.disarm(reason="test")
    assert manager.config.execution.enabled is False
    assert manager.check_expiry(utcnow()) is False  # terminal states ignore expiry


def test_complete_and_halt_are_terminal():
    manager = _authorized()
    manager.authorize(operator="op", window_minutes=30)
    manager.arm()
    manager.complete()
    assert manager.state is ExperimentState.COMPLETED
    assert manager.config.execution.enabled is False
    manager2 = _authorized()
    manager2.authorize(operator="op", window_minutes=30)
    manager2.halt("test_kill")
    assert manager2.state is ExperimentState.HALTED
    assert manager2.kill_reason == "test_kill"
    assert manager2.config.execution.enabled is False
    with pytest.raises(ExperimentError):
        manager2.arm()  # no re-arm after halt


# --- tripwires --------------------------------------------------------------------------
def test_tripwires_all_clear():
    manager = _authorized()
    assert manager.evaluate_tripwires({
        "paper_mode": True, "cap_breach": None, "reconciliation_ok": True,
        "has_exposure": False, "open_orders": 0, "duplicate_order": False,
        "ambiguous_unresolved": False, "stale_entry_attempted": False,
    }) is None


@pytest.mark.parametrize("snapshot,reason", [
    ({"paper_mode": False}, "paper_live_mismatch"),
    ({"paper_mode": True, "cap_breach": "order_over_cap:x",
      "reconciliation_ok": True, "has_exposure": False, "open_orders": 0,
      "duplicate_order": False, "ambiguous_unresolved": False,
      "stale_entry_attempted": False}, "cap_breach"),
    ({"paper_mode": True, "cap_breach": None, "reconciliation_ok": False,
      "has_exposure": True, "open_orders": 0,
      "duplicate_order": False, "ambiguous_unresolved": False,
      "stale_entry_attempted": False}, "unreconciled"),
    ({"paper_mode": True, "cap_breach": None, "reconciliation_ok": True,
      "has_exposure": False, "open_orders": 0,
      "duplicate_order": True, "ambiguous_unresolved": False,
      "stale_entry_attempted": False}, "duplicate"),
])
def test_tripwire_fires(snapshot, reason):
    manager = _authorized()
    assert manager.evaluate_tripwires(snapshot) is not None
    assert reason in manager.evaluate_tripwires(snapshot)


def test_broker_unverifiable_is_a_kill():
    manager = _authorized()
    assert manager.evaluate_tripwires({
        "paper_mode": True, "cap_breach": None, "reconciliation_ok": False,
        "has_exposure": False, "open_orders": 0, "duplicate_order": False,
        "ambiguous_unresolved": False, "stale_entry_attempted": False,
        "broker_unverifiable": True,
    }) == "broker_unverifiable"


def test_clean_reconciliation_with_exposure_is_not_a_kill():
    manager = _authorized()
    assert manager.evaluate_tripwires({
        "paper_mode": True, "cap_breach": None, "reconciliation_ok": True,
        "has_exposure": True, "open_orders": 1, "duplicate_order": False,
        "ambiguous_unresolved": False, "stale_entry_attempted": False,
    }) is None


# --- dashboard API ------------------------------------------------------------------
def _client():
    return TestClient(create_app(config=_config(), autostart=False))


def test_experiment_status_endpoint_reports_state():
    with _client() as client:
        body = client.get("/api/experiment/status").json()
        assert body["state"] == "preparing"
        assert body["execution_enabled"] is False
        assert body["envelope"] is None


def test_authorize_endpoint_validates_and_freezes_envelope():
    with _client() as client:
        # Fresh app never observed: state refusal comes first (fail closed).
        refused = client.post("/api/experiment/authorize",
                              json={"operator": "op", "window_minutes": 30})
        assert refused.status_code == 409
        assert "state preparing" in refused.text


def test_halt_endpoint_without_envelope_is_safe():
    with _client() as client:
        body = client.post("/api/experiment/halt",
                           json={"reason": "test", "operator": "human"}).json()
        assert body["halted"] is True
        assert body["state"] == "halted"
        assert client.get("/api/experiment/status").json()["execution_enabled"] is False


def test_dashboard_serves_experiment_panel():
    with _client() as client:
        html = client.get("/dashboard").text
        assert "Experiment" in html
        assert "/api/experiment/authorize" in html
        assert "HALT (kill switch)" in html


# --- engine integration (mock broker, in-memory arming only) ---
def _live_engine():
    from app.runtime import build_runtime

    config = _config()
    config.paper_safety.enabled = True
    return build_runtime(config, get_env()).engine


async def test_armed_tick_kills_on_unreconciled_exposure_and_flattens():
    from app.core.ids import new_order_id
    from app.domain.enums import Direction, OrderStatus, Side
    from app.domain.orders import Order

    engine = _live_engine()
    await engine.broker.connect()
    engine.broker.set_price("BTC/USD", 80000.0)
    try:
        manager = engine.experiment
        manager.mark_observing()
        manager.mark_ready_for_authorization()
        manager.authorize(operator="op-test", window_minutes=30)
        manager.arm()
        assert engine.execution_enabled is True
        # Inject broker-side exposure unknown locally.
        order = Order(order_id=new_order_id(), client_order_id="ext-k",
                      symbol="BTC/USD", side=Side.BUY, direction=Direction.LONG,
                      quantity=0.001, submitted_at=utcnow())
        assert (await engine.broker.submit_order(order)).status == OrderStatus.FILLED
        engine._need_reconciliation = True
        engine.state.reconciliation = {"ok": False, "discrepancies": ["test"]}
        await engine.tick(utcnow())
        assert manager.state.value == "halted"
        assert engine.execution_enabled is False
        assert await engine.broker.get_positions() == []
        assert engine.position_manager.is_flat
    finally:
        await engine.stop()


async def test_armed_tick_promotes_to_running_without_orders():
    engine = _live_engine()
    try:
        manager = engine.experiment
        manager.mark_observing()
        manager.mark_ready_for_authorization()
        manager.authorize(operator="op-test", window_minutes=30)
        manager.arm()
        n_orders_before = len(engine.broker._orders)
        await engine.tick(utcnow())
        assert manager.state.value in ("armed", "running")
        assert len(engine.broker._orders) == n_orders_before
    finally:
        await engine.stop()
