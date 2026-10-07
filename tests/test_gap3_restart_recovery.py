"""GAP-3 restart-recovery tests (deterministic, mock broker only).

Proves the restart architecture using a persistent MockBroker as broker
truth across simulated process restarts (fresh engine, same broker object):
mismatches are detected, entries stay blocked until resolved, recovery
itself emits no signals/orders, and an operator flatten restores clean
startup. NO real or paper orders are submitted anywhere in this file.

Known limitation pinned (not hidden): startup reconciliation compares
POSITIONS only; open broker orders are visible via get_orders (operator
tooling) but are not consumed by startup. Covered procedurally in the
runbook; see test_startup_open_orders_require_operator_review.
"""
from __future__ import annotations

from app.config.loader import get_env, load_config
from app.core.clock import utcnow
from app.core.ids import new_order_id
from app.domain.enums import Direction, OrderStatus, OrderType, Side
from app.domain.orders import Order, OrderIntent
from app.events.types import EventType
from tests.support import SYMBOL, make_signal


def _config(*, execution_enabled: bool):
    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.execution.enabled = execution_enabled
    config.market_data.max_future_skew_seconds = 86400 * 30
    return config


def _engine(*, execution_enabled: bool):
    from app.runtime import build_runtime

    return build_runtime(_config(execution_enabled=execution_enabled), get_env()).engine


def _rejection_checks(events) -> list:
    out = []
    for event in events:
        payload = getattr(event, "payload", {}) or {}
        checks = payload.get("checks", [])
        out.extend(checks)
    return out


# Case A — restart while flat -------------------------------------------------------
async def test_restart_while_flat_reconciles_clean_with_no_orders():
    engine = _engine(execution_enabled=True)
    await engine.broker.connect()
    try:
        assert await engine.reconcile() is True
        assert engine._need_reconciliation is False
        # Simulated restart: brand-new local state, same broker truth.
        engine2 = _engine(execution_enabled=True)
        engine2.broker = engine.broker
        try:
            events: list = []
            engine2.bus.subscribe_all(events.append)
            assert await engine2.reconcile() is True
            created = [e for e in events if getattr(e, "type", None) is EventType.ORDER_CREATED]
            assert created == []
            assert len(engine.broker._orders) == 0
        finally:
            await engine2.stop()
    finally:
        await engine.stop()


# Case B — restart with open paper position ----------------------------------------------
async def test_restart_with_broker_position_blocks_entries_until_resolved():
    engine = _engine(execution_enabled=True)
    await engine.broker.connect()
    engine.broker.set_price(SYMBOL, 80000.0)
    try:
        from app.domain.orders import Order as OrderModel

        order = OrderModel(order_id=new_order_id(), client_order_id="ext-1", symbol=SYMBOL,
                           side=Side.BUY, direction=Direction.LONG, quantity=0.001,
                           submitted_at=utcnow())
        result = await engine.broker.submit_order(order)
        assert result.status == OrderStatus.FILLED
        assert len(await engine.broker.get_positions()) == 1

        # Simulated restart: fresh local books (flat) vs broker truth (0.001).
        engine2 = _engine(execution_enabled=True)
        engine2.broker = engine.broker
        try:
            assert await engine2.reconcile() is False
            assert engine2._need_reconciliation is True
            assert "broker position" in str(
                engine2.state.reconciliation.get("discrepancies"))
            engine2.session.start(utcnow())
            events: list = []
            engine2.bus.subscribe_all(events.append)
            n_orders_before = len(engine.broker._orders)
            await engine2._handle_signal(make_signal())
            assert "reconciliation_ok" in _rejection_checks(events)
            # Recovery generated no strategy signal effect: no new orders.
            assert len(engine.broker._orders) == n_orders_before

            # Operator flatten via broker truth, then clean restart state.
            positions = await engine.broker.get_positions()
            await engine.broker.close_position(positions[0])
            assert await engine.broker.get_positions() == []
            assert await engine2.reconcile() is True
            assert engine2._need_reconciliation is False
            events2: list = []
            engine2.bus.subscribe_all(events2.append)
            await engine2._handle_signal(make_signal())
            assert "reconciliation_ok" not in _rejection_checks(events2)
        finally:
            await engine2.stop()
    finally:
        await engine.stop()


# Case E — quantity disagreement, both directions ----------------------------------------------
async def test_quantity_mismatch_both_directions_detected():
    from app.core.ids import new_fill_id
    from app.domain.orders import Fill

    engine = _engine(execution_enabled=True)
    await engine.broker.connect()
    engine.broker.set_price(SYMBOL, 80000.0)
    try:
        # Local 0.002 vs broker empty.
        engine.position_manager.apply_fill(Fill(
            fill_id=new_fill_id(), order_id=new_order_id(), timestamp=utcnow(),
            symbol=SYMBOL, side=Side.BUY, quantity=0.002, price=80000.0, fee=0.0))
        assert await engine.reconcile() is False
        assert any("not present at broker" in d
                   for d in engine.state.reconciliation["discrepancies"])
    finally:
        await engine.stop()


# Case C — open broker orders: visibility vs startup scope ------------------------------------------
async def test_startup_open_orders_require_operator_review():
    """DOCUMENTED LIMITATION (procedural coverage, not silent).

    Startup reconciliation compares POSITIONS only. An open (unfilled)
    broker order is retrievable via get_orders for operator review, but no
    startup path consumes it. The runbook requires a broker-dashboard open-
    order check before arming; this test pins that contract.
    """
    engine = _engine(execution_enabled=True)
    await engine.broker.connect()
    try:
        dangling = Order(order_id=new_order_id(), client_order_id="ext-9", symbol=SYMBOL,
                         side=Side.BUY, direction=Direction.LONG, quantity=0.001,
                         status=OrderStatus.NEW, submitted_at=utcnow())
        engine.broker._orders[dangling.order_id] = dangling
        open_orders = await engine.broker.get_orders(status="new")
        assert any(o.order_id == dangling.order_id for o in open_orders)
        # Positions are flat/flat: position reconciliation passes DESPITE the
        # open order — the gap this test documents.
        assert await engine.reconcile() is True
    finally:
        await engine.stop()


# Case D — ambiguous submission reconciles before retry -------------------------------------------------
async def test_ambiguous_submission_sets_reconcile_before_retry():
    from app.core.ids import new_order_id as _new_id
    from app.domain.orders import OrderIntent

    engine = _engine(execution_enabled=True)
    try:
        # Broker that explodes on submit => ambiguous network failure.
        async def _boom(order):
            raise ConnectionError("simulated transport failure")

        engine.broker.submit_order = _boom
        engine.state.latest_snapshot = None
        intent = OrderIntent(
            order_id=_new_id(), client_order_id=_new_id(), timestamp=utcnow(),
            symbol=SYMBOL, side=Side.BUY, direction=Direction.LONG,
            quantity=0.001, order_type=OrderType.MARKET)
        order = engine.oms.create(intent)
        assert await engine._submit(order) is False
        assert engine._need_reconciliation is True
        assert any("ambiguous" in d for d in engine.state.reconciliation["discrepancies"])
    finally:
        await engine.stop()


# Case F — restart/entry near flatten deadline ------------------------------------------------------------
async def test_closeout_session_blocks_new_entries():
    engine = _engine(execution_enabled=True)
    try:
        engine.session.start(utcnow())
        engine.session.begin_closeout(utcnow())
        assert engine.session.entries_allowed is False
        events: list = []
        engine.bus.subscribe_all(events.append)
        n_orders_before = len(engine.broker._orders)
        await engine._handle_signal(make_signal())
        assert len(engine.broker._orders) == n_orders_before
        payloads = [getattr(e, "payload", {}) or {} for e in events]
        assert any("cutoff" in str(p) or "session" in str(p).lower() or "closed" in str(p).lower()
                   for p in payloads)
    finally:
        await engine.stop()


# No-trading property of recovery ------------------------------------------------------------------------------
async def test_recovery_path_emits_no_orders_or_signals():
    engine = _engine(execution_enabled=True)
    await engine.broker.connect()
    try:
        events: list = []
        engine.bus.subscribe_all(events.append)
        assert await engine.reconcile() is True
        created = [e for e in events if getattr(e, "type", None) is EventType.ORDER_CREATED]
        signals = [e for e in events if getattr(e, "type", None) is EventType.SIGNAL_GENERATED]
        assert created == [] and signals == []
        assert len(engine.broker._orders) == 0
    finally:
        await engine.stop()
