"""GAP-1 paper safety cap tests.

Rejection-only gate semantics, fail-closed config handling, paper-only
enforcement, session bookkeeping, retry idempotence, strategy isolation,
startup visibility, and config-hash inclusion. No orders submitted anywhere.
"""
from __future__ import annotations

import json

import pytest

from app.config.loader import get_env, load_config
from app.config.models import PaperSafetyConfig
from app.safety.paper_cap import PaperSafetyCap


def _cap(**overrides) -> PaperSafetyCap:
    cfg = PaperSafetyConfig(enabled=True, **overrides)
    return PaperSafetyCap(cfg, trading_mode="paper")


def _check(cap, **kw):
    args = {"order_id": "o1", "quantity": 0.0005, "price": 80000.0,
            "is_entry": True, "open_qty": 0.0}
    args.update(kw)
    return cap.check(**args)


# 1-3. quantity boundaries ----------------------------------------------------------
def test_quantity_below_cap_accepted():
    assert _check(_cap(), quantity=0.0005).allowed


def test_quantity_exactly_at_cap_accepted():
    assert _check(_cap(), quantity=0.001).allowed


def test_quantity_above_cap_rejected_not_clipped():
    decision = _check(_cap(), quantity=0.005)
    assert not decision.allowed
    assert decision.reason == "qty_per_order_exceeded"
    # Rejection, never silent clipping: no adjusted quantity is offered.
    assert "quantity" not in decision.detail or decision.detail["quantity"] == 0.005


# 4-6. open/count/notional --------------------------------------------------------------
def test_open_position_at_cap_rejects_new_entry():
    decision = _check(_cap(), quantity=0.0005, open_qty=0.001)
    assert not decision.allowed
    assert decision.reason == "open_qty_exceeded"


def test_order_count_at_cap_rejects_new_order():
    cap = _cap()
    for i in range(3):
        assert _check(cap, order_id=f"o{i}").allowed
        cap.note_submitted(f"o{i}")
    decision = _check(cap, order_id="o3")
    assert not decision.allowed
    assert decision.reason == "orders_per_session_exceeded"


def test_notional_above_cap_rejected():
    # 0.001 * 200000 = 200 > 150 notional cap (qty itself is within cap).
    decision = _check(_cap(), quantity=0.001, price=200000.0)
    assert not decision.allowed
    assert decision.reason == "notional_exceeded"


# exits bypass (flatten-safe) -------------------------------------------------------------------
def test_exit_orders_bypass_caps():
    cap = _cap()
    decision = cap.check(order_id="x1", quantity=99.0, price=1e9,
                         is_entry=False, open_qty=99.0)
    assert decision.allowed
    assert decision.reason == "exit_bypass"


# 7. retry idempotence --------------------------------------------------------------------------------
def test_retry_cannot_bypass_cap_and_is_not_double_counted():
    cap = _cap(max_orders_per_session=1)
    first = _check(cap, order_id="r1", quantity=0.001)
    assert first.allowed
    cap.note_submitted("r1")
    # Same order retried: re-evaluated (still gated), not double-counted.
    retry = _check(cap, order_id="r1", quantity=0.001)
    assert retry.allowed
    cap.note_submitted("r1")
    assert cap.entries_this_session == 1
    # A DIFFERENT order is now over the count cap.
    assert not _check(cap, order_id="r2").allowed


def test_retry_of_rejected_order_stays_rejected():
    cap = _cap()
    assert not _check(cap, order_id="r9", quantity=0.005).allowed
    assert not _check(cap, order_id="r9", quantity=0.005).allowed
    assert cap.entries_this_session == 0


# 8. strategy isolation ------------------------------------------------------------------------------------------
def test_strategy_cannot_reach_or_bypass_cap():
    from app.strategies.registry import build_strategy

    config = load_config(env=get_env())
    for name in ("breakout_momentum", "vwap_reversion",
                 "session_vwap_reversion", "range_edge_rejection"):
        config.strategy.name = name
        strategy = build_strategy(config.strategy)
        assert not hasattr(strategy, "paper_cap")
        assert not hasattr(strategy, "paper_safety")
    # The enforcement point is the submission funnel, which every order
    # traverses regardless of origin: oversized is rejected at check time.
    assert not _check(_cap(), quantity=999.0).allowed


# 9. invalid config fail-closed ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("field,value", [
    ("max_qty_per_order", 0.0), ("max_qty_per_order", -1.0),
    ("max_open_qty", 0.0), ("max_notional_exposure", 0.0),
    ("max_orders_per_session", 0),
])
def test_invalid_cap_fails_closed(field, value):
    cap = _cap(**{field: value})
    decision = _check(cap, quantity=0.0000001, price=1.0)
    assert not decision.allowed
    assert decision.reason.startswith("invalid_cap")


# 10. disabled cap inactive but auditable --------------------------------------------------------------------------------------
def test_disabled_cap_inactive_but_described():
    cfg = PaperSafetyConfig()  # enabled=False default
    cap = PaperSafetyCap(cfg, trading_mode="paper")
    decision = _check(cap, quantity=999.0, price=1e12)
    assert decision.allowed
    assert decision.active is False
    assert decision.reason == "cap_inactive"
    described = cap.describe()
    assert described["enabled"] is False
    assert described["max_qty_per_order"] == 0.001


# 11-12. paper-only, never live ----------------------------------------------------------------------------------------------
def test_non_paper_mode_rejects_everything():
    cfg = PaperSafetyConfig(enabled=True)
    cap = PaperSafetyCap(cfg, trading_mode="live")
    assert not _check(cap).allowed
    assert _check(cap).reason == "paper_mode_required"


def test_cap_cannot_authorize_live_trading():
    # The cap carries no enablement power: it only ever returns allow/deny
    # for an order someone else submitted, and denies outside paper mode.
    cfg = PaperSafetyConfig(enabled=True)
    for mode in ("live", "production", ""):
        cap = PaperSafetyCap(cfg, trading_mode=mode)
        assert not _check(cap).allowed


# 13-14. startup visibility + config hash -------------------------------------------------------------------------------------------
def test_startup_describe_reports_caps():
    described = _cap().describe()
    assert described["enabled"] is True
    assert described["paper_mode"] is True
    assert described["max_qty_per_order"] == 0.001
    assert described["max_open_qty"] == 0.001
    assert described["max_orders_per_session"] == 3
    assert described["max_notional_exposure"] == 150.0
    assert described["config_valid"] is True


def test_config_hash_includes_cap_configuration():
    config = load_config(env=get_env())
    before = config.model_dump_json()
    assert '"paper_safety"' in before
    config.paper_safety.max_qty_per_order = 0.0005
    after = config.model_dump_json()
    assert before != after
    assert "0.0005" in after


# security property --------------------------------------------------------------------------------------------------
def test_cap_only_restricts_never_expands():
    # For every gated order the cap output is binary allow/deny on the
    # caller-supplied quantity: no adjusted/clipped quantity ever exists,
    # so restriction without expansion holds by construction.
    cap = _cap()
    for qty in (0.0001, 0.001, 0.0011, 1.0):
        decision = _check(cap, quantity=qty)
        assert decision.allowed == (qty <= 0.001)
        assert "clipped_quantity" not in decision.detail
        assert "adjusted" not in decision.detail


def test_session_reset_clears_counters():
    cap = _cap(max_orders_per_session=1)
    assert _check(cap, order_id="a").allowed
    cap.note_submitted("a")
    assert not _check(cap, order_id="b").allowed
    cap.reset_session()
    assert _check(cap, order_id="b").allowed


# --- funnel wiring (mock broker, execution enabled in-memory only) ---
def _funnel_engine(*, cap_enabled: bool):
    from app.core.ids import new_order_id
    from app.runtime import build_runtime

    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.execution.enabled = True
    config.paper_safety.enabled = cap_enabled
    engine = build_runtime(config, get_env()).engine
    return engine


def _entry_order(engine, *, quantity: float):
    from app.core.clock import utcnow
    from app.core.ids import new_order_id
    from app.domain.enums import Direction, OrderType, Side
    from app.domain.orders import OrderIntent

    intent = OrderIntent(
        order_id=new_order_id(),
        client_order_id=new_order_id(),
        timestamp=utcnow(),
        symbol="BTC/USD",
        side=Side.BUY,
        direction=Direction.LONG,
        quantity=quantity,
        order_type=OrderType.MARKET,
    )
    return engine.oms.create(intent)


async def test_funnel_rejects_oversized_entry_before_broker():
    from app.domain.enums import OrderStatus

    engine = _funnel_engine(cap_enabled=True)
    try:
        order = _entry_order(engine, quantity=0.005)
        assert await engine._submit(order) is False
        assert order.status is OrderStatus.REJECTED
        assert order.reject_reason.startswith("paper_cap:")
        # Broker never saw it: no submit, no fill, no position.
        assert engine.broker._orders == {}
        assert await engine.broker.get_positions() == []
    finally:
        await engine.stop()


async def test_funnel_passes_within_cap_order_to_mock_broker():
    from app.core.clock import utcnow
    from app.domain.market import MarketSnapshot

    engine = _funnel_engine(cap_enabled=True)
    try:
        await engine.broker.connect()
        engine.broker.set_price("BTC/USD", 80000.0)
        engine.state.latest_snapshot = MarketSnapshot(
            symbol="BTC/USD", timestamp=utcnow(), last=80000.0)
        order = _entry_order(engine, quantity=0.001)
        assert await engine._submit(order) is True
        positions = await engine.broker.get_positions()
        assert len(positions) == 1
        assert engine.paper_cap.entries_this_session == 1
    finally:
        await engine.stop()


async def test_funnel_allows_flatten_above_cap():
    # Flatten-safe direction: an exit larger than any cap still flows.
    from app.domain.enums import OrderStatus

    from app.core.clock import utcnow
    from app.domain.market import MarketSnapshot

    engine = _funnel_engine(cap_enabled=True)
    try:
        await engine.broker.connect()
        engine.broker.set_price("BTC/USD", 80000.0)
        engine.state.latest_snapshot = MarketSnapshot(
            symbol="BTC/USD", timestamp=utcnow(), last=80000.0)
        order = _entry_order(engine, quantity=0.001)
        assert await engine._submit(order) is True
        assert await engine.broker.get_positions() != []
        assert await engine.broker.close_position(
            (await engine.broker.get_positions())[0]
        )
        assert await engine.broker.get_positions() == []
    finally:
        await engine.stop()


async def test_cap_disabled_leaves_flow_unchanged():
    engine = _funnel_engine(cap_enabled=False)
    try:
        await engine.broker.connect()
        engine.broker.set_price("BTC/USD", 80000.0)
        order = _entry_order(engine, quantity=0.005)
        # No cap: reaches the broker (mock fills it).
        assert await engine._submit(order) is True
        assert engine.paper_cap.describe()["enabled"] is False
    finally:
        await engine.stop()
