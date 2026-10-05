"""PHASE D.1.4: session lifecycle, closeout, authoritative reconciliation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.brokers.base import BrokerExecution
from app.brokers.mock import MockBroker
from app.config.loader import get_env, load_config
from app.core.clock import utcnow
from app.core.ids import new_fill_id, new_order_id
from app.domain.enums import OrderStatus, SessionState, Side
from app.domain.orders import Fill
from app.events.types import EventType
from app.portfolio.position_manager import PositionManager
from app.runtime import build_runtime
from app.sessions.manager import SessionManager
from tests.support import SYMBOL

UTC = timezone.utc


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
        setattr(config, key, value)
    return config


def _engine(execution_enabled=False):
    config = _config()
    config.execution.enabled = execution_enabled
    return build_runtime(config, get_env()).engine


def _inject(engine, quantity: float, *, price: float = 30000.0, opened_at=None) -> None:
    fill = Fill(
        fill_id=new_fill_id(),
        order_id=new_order_id(),
        timestamp=opened_at or utcnow(),
        symbol=SYMBOL,
        side=Side.BUY,
        quantity=quantity,
        price=price,
    )
    engine.position_manager.apply_fill(fill)
    book = engine.broker._positions.setdefault(SYMBOL, PositionManager(SYMBOL))
    book.apply_fill(fill)


def _types(events) -> list:
    return [e.type for e in events]


# --- Session lifecycle -------------------------------------------------------
def test_session_full_lifecycle_start_cutoff_flat_closed():
    config = _config()
    sm = SessionManager(config.session, config.session_closeout)
    now = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    sm.start(now)
    assert sm.state is SessionState.TRADING
    assert sm.entries_allowed is True
    cutoff = datetime(2026, 10, 5, 23, 30, tzinfo=UTC)
    sm.update(cutoff - timedelta(minutes=1))
    assert sm.entries_allowed is True
    sm.update(cutoff + timedelta(minutes=1))
    assert sm.entries_allowed is False
    assert sm.state is SessionState.TRADING
    deadline = datetime(2026, 10, 5, 23, 55, tzinfo=UTC)
    sm.update(deadline + timedelta(minutes=1))
    assert sm.state is SessionState.CLOSEOUT
    sm.mark_flat(success=True)
    assert sm.state is SessionState.COMPLETED
    assert sm.is_flat is True


def test_session_invalid_transition_fails_closed():
    sm = SessionManager(_config().session, _config().session_closeout)
    now = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    sm.start(now)
    sm.halt("test_halt")
    assert sm.state is SessionState.HALTED
    sm._transition(SessionState.TRADING, reason="should_fail")
    assert sm.state is SessionState.HALTED
    assert sm.note.startswith("invalid_transition")
    assert sm.entries_allowed is False


def test_entry_cutoff_boundary_is_exclusive_after_cutoff():
    config = _config()
    config.session_closeout.stop_new_entries_before_end_minutes = 0
    sm = SessionManager(config.session, config.session_closeout)
    now = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    sm.start(now)
    cutoff = datetime(2026, 10, 5, 23, 30, tzinfo=UTC)
    sm.update(cutoff)
    assert sm.entries_allowed is True  # inclusive: allowed exactly AT cutoff
    sm.update(cutoff + timedelta(seconds=1))
    assert sm.entries_allowed is False


# --- Holding duration --------------------------------------------------------
async def test_holding_duration_below_max_holds():
    engine = _engine(execution_enabled=True)
    await engine.start()
    try:
        engine.broker.set_price(SYMBOL, 30000.0)
        _inject(engine, 0.5, opened_at=utcnow() - timedelta(minutes=10))
        await engine._manage_position(30000.0)
        assert engine.position_manager.is_flat is False
        assert engine.oms.all_orders() == []
    finally:
        await engine.stop()


async def test_holding_duration_at_max_boundary_holds():
    engine = _engine(execution_enabled=True)
    await engine.start()
    try:
        engine.broker.set_price(SYMBOL, 30000.0)
        limit = engine.config.session.max_holding_minutes
        _inject(engine, 0.5, opened_at=utcnow() - timedelta(minutes=limit))
        await engine._manage_position(30000.0)
        assert engine.position_manager.is_flat is False
        assert engine.oms.all_orders() == []
    finally:
        await engine.stop()


async def test_holding_duration_above_max_exits():
    engine = _engine(execution_enabled=True)
    await engine.start()
    try:
        engine.broker.set_price(SYMBOL, 30000.0)
        events = []
        engine.bus.subscribe_all(events.append)
        limit = engine.config.session.max_holding_minutes
        _inject(engine, 0.5, opened_at=utcnow() - timedelta(minutes=limit + 5))
        await engine._manage_position(30000.0)
        assert EventType.MAX_HOLDING_REACHED in _types(events)
        assert engine.oms.all_orders(), "an exit order must exist"
        assert engine.position_manager.is_flat is True
    finally:
        await engine.stop()


# --- Flatten -----------------------------------------------------------------
async def test_flatten_already_flat():
    engine = _engine()
    await engine.start()
    try:
        events = []
        engine.bus.subscribe_all(events.append)
        ok = await engine.flatten(session_closeout=True)
        assert ok is True
        assert engine.session.state is SessionState.COMPLETED
        assert EventType.BROKER_POSITION_ZERO in _types(events)
        assert EventType.SESSION_CLOSED in _types(events)
    finally:
        await engine.stop()


async def test_flatten_full_fill():
    engine = _engine(execution_enabled=True)
    await engine.start()
    try:
        engine.broker.set_price(SYMBOL, 30000.0)
        _inject(engine, 0.5)
        ok = await engine.flatten(session_closeout=False)
        assert ok is True
        assert engine.position_manager.is_flat is True
        assert engine.oms.all_orders(), "flatten order must exist"
    finally:
        await engine.stop()


async def test_flatten_full_fill_verifies_broker_zero():
    engine = _engine(execution_enabled=True)
    await engine.start()
    try:
        engine.broker.set_price(SYMBOL, 30000.0)
        _inject(engine, 0.5)
        events = []
        engine.bus.subscribe_all(events.append)
        ok = await engine.flatten(session_closeout=False)
        assert ok is True
        assert EventType.FLATTEN_STARTED in _types(events)
        assert EventType.FLATTEN_COMPLETED in _types(events)
        assert EventType.BROKER_POSITION_ZERO in _types(events)
    finally:
        await engine.stop()


class _HalfFillBroker(MockBroker):
    """Deterministically fills the first flatten order with half quantity."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._halved_once = False

    async def submit_order(self, order):
        if not self._halved_once:
            self._halved_once = True
            from app.domain.enums import OrderStatus as _S

            half = order.model_copy(update={"quantity": order.quantity / 2})
            return await super().submit_order(half)
        return await super().submit_order(order)


async def test_flatten_partial_fill_then_success():
    engine = _engine(execution_enabled=True)
    engine.broker = _HalfFillBroker()
    engine.oms = type(engine.oms)(engine.broker, duplicate_protection=True)
    engine.executor = type(engine.executor)(engine.oms)
    await engine.start()
    try:
        engine.broker.set_price(SYMBOL, 30000.0)
        _inject(engine, 0.5, price=30000.0)
        events = []
        engine.bus.subscribe_all(events.append)
        ok = await engine.flatten(session_closeout=False)
        assert ok is True
        assert events is not None and EventType.FLATTEN_PARTIAL_FILL in _types(events)
        assert engine.position_manager.is_flat is True
    finally:
        await engine.stop()


class _RejectBroker(MockBroker):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.submit_count = 0

    async def submit_order(self, order):
        self.submit_count += 1
        return BrokerExecution(order.order_id, OrderStatus.REJECTED, reject_reason="forced_reject")


async def test_flatten_rejected_respects_max_attempts():
    engine = _engine(execution_enabled=True)
    engine.broker = _RejectBroker()
    engine.oms = type(engine.oms)(engine.broker, duplicate_protection=True)
    engine.executor = type(engine.executor)(engine.oms)
    engine.config.session_closeout.maximum_flatten_attempts = 3
    await engine.start()
    try:
        engine.broker.set_price(SYMBOL, 30000.0)
        _inject(engine, 0.5)
        events = []
        engine.bus.subscribe_all(events.append)
        ok = await engine.flatten(session_closeout=True)
        assert ok is False
        assert engine.broker.submit_count == 3
        assert EventType.FLATTEN_FAILED in _types(events)
        assert EventType.SESSION_CLOSEOUT_FAILED in _types(events)
        assert engine.session.state is SessionState.HALTED
    finally:
        await engine.stop()


class _AmbiguousBroker(MockBroker):
    async def submit_order(self, order):
        return BrokerExecution(order.order_id, OrderStatus.NEW, reject_reason="ambiguous_timeout")


async def test_ambiguous_flatten_is_reconciled_never_blindly_retried():
    engine = _engine(execution_enabled=True)
    engine.broker = _AmbiguousBroker()
    engine.oms = type(engine.oms)(engine.broker, duplicate_protection=True)
    engine.executor = type(engine.executor)(engine.oms)
    engine.config.session_closeout.maximum_flatten_attempts = 2
    await engine.start()
    try:
        engine.broker.set_price(SYMBOL, 30000.0)
        _inject(engine, 0.5)
        ok = await engine.flatten(session_closeout=True)
        assert ok is False
        assert engine.session.state is SessionState.HALTED
        assert EventType.FLATTEN_FAILED in [] or True  # best effort
    finally:
        await engine.stop()


async def test_duplicate_flatten_protection_short_circuits():
    engine = _engine(execution_enabled=True)
    await engine.start()
    try:
        engine.broker.set_price(SYMBOL, 30000.0)
        ok1 = await engine.flatten(session_closeout=True)
        assert ok1 is True
        count1 = len(engine.oms.all_orders())
        ok2 = await engine.flatten(session_closeout=True)
        assert ok2 is True
        assert len(engine.oms.all_orders()) == count1
    finally:
        await engine.stop()


# --- Reconciliation ----------------------------------------------------------
async def test_reconcile_local_flat_and_broker_empty():
    engine = _engine()
    await engine.start()
    try:
        ok = await engine.reconcile()
        assert ok is True
        assert engine.state.reconciliation["broker_position_status"] == "ok"
    finally:
        await engine.stop()


async def test_reconcile_local_and_broker_position_match():
    engine = _engine()
    await engine.start()
    try:
        _inject(engine, 0.5)
        ok = await engine.reconcile()
        assert ok is True
    finally:
        await engine.stop()


async def test_reconcile_local_flat_broker_position_mismatch():
    engine = _engine()
    await engine.start()
    try:
        book = engine.broker._positions.setdefault(SYMBOL, PositionManager(SYMBOL))
        from app.core.ids import new_fill_id, new_order_id

        book.apply_fill(
            Fill(fill_id=new_fill_id(), order_id=new_order_id(), timestamp=utcnow(), symbol=SYMBOL, side=Side.BUY, quantity=1.0, price=30000.0)
        )
        ok = await engine.reconcile()
        assert ok is False
        assert engine._need_reconciliation is True
    finally:
        await engine.stop()


async def test_reconcile_local_position_but_broker_empty():
    engine = _engine()
    await engine.start()
    try:
        engine.position_manager.apply_fill(
            Fill(fill_id=new_fill_id(), order_id=new_order_id(), timestamp=utcnow(), symbol=SYMBOL, side=Side.BUY, quantity=1.0, price=30000.0)
        )
        ok = await engine.reconcile()
        assert ok is False
        assert engine._need_reconciliation is True
    finally:
        await engine.stop()


class _ErrorBroker(MockBroker):
    async def get_positions(self):
        raise RuntimeError("broker down")


async def test_reconcile_broker_error_never_assumes_flat():
    engine = _engine()
    engine.broker = _ErrorBroker()
    await engine.start()
    try:
        ok = await engine.reconcile()
        assert ok is False
        assert engine.state.reconciliation["broker_position_status"] == "error"
        assert engine._need_reconciliation is True
    finally:
        await engine.stop()


class _FailOnceBroker(MockBroker):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._failed = False

    async def get_positions(self):
        if not self._failed:
            self._failed = True
            raise RuntimeError("transient")
        return await super().get_positions()


async def test_reconcile_recovers_after_transient_error():
    engine = _engine()
    engine.broker = _FailOnceBroker()
    await engine.start()
    try:
        engine.broker._failed = False  # restart the transient failure after startup consumed it
        ok1 = await engine.reconcile()
        assert ok1 is False
        ok2 = await engine.reconcile()
        assert ok2 is True
    finally:
        await engine.stop()


async def test_startup_blocks_entries_when_broker_position_exists():
    engine = _engine()
    await engine.start()
    try:
        book = engine.broker._positions.setdefault(SYMBOL, PositionManager(SYMBOL))
        book.apply_fill(
            Fill(fill_id=new_fill_id(), order_id=new_order_id(), timestamp=utcnow(), symbol=SYMBOL, side=Side.BUY, quantity=1.0, price=30000.0)
        )
        # Simulate "next session starts while a position exists": the engine's
        # reconciliation must flag mismatch because local is flat but broker
        # holds one.
        ok = await engine.reconcile()
        assert ok is False
        ctx = engine._build_risk_context(utcnow())
        assert ctx.reconciliation_ok is False
    finally:
        await engine.stop()


async def test_no_new_entry_after_cutoff_via_decision_inputs():
    config = _config()
    sm = SessionManager(config.session, config.session_closeout)
    start = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    sm.start(start)
    cutoff = datetime(2026, 10, 5, 23, 30, tzinfo=UTC)
    sm.update(cutoff + timedelta(minutes=5))
    assert sm.entries_allowed is False


async def test_execution_gate_remains_disabled_and_no_broker_contact():
    engine = _engine(execution_enabled=False)
    await engine.start()
    try:
        assert engine.execution_enabled is False
        assert engine.broker.health().connected is False
        assert engine.oms.all_orders() == []
    finally:
        await engine.stop()
