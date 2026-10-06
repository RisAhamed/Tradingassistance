"""Phase D.5.5: multi-order accounting convergence + acceptance-harness safety.

BUG-D5-001 invariants under multiple / opposing orders:

    BROKER  ==  FILL LEDGER  ==  OMS  ==  POSITION MANAGER

covering cumulative broker fills, duplicate fills, opposing sides, reversals,
reconciliation (ok and failed), flatten, zero position, broker-side untracked
positions, order-status synchronisation, and the harness's paper-only /
execution-disabled safety rails.
"""
from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

from app.brokers.mock import MockBroker
from app.config.loader import EnvSettings, get_env, load_config
from app.core.clock import utcnow
from app.core.errors import ConfigError, PaperOnlyViolation
from app.core.ids import new_fill_id, new_order_id, new_session_id
from app.domain.enums import Direction, OrderStatus, OrderType, Side
from app.domain.orders import Fill, OrderIntent
from app.events.types import EventType
from app.orders.oms import OMS
from app.portfolio.position_manager import PositionManager
from app.runtime import build_runtime
from tests.support import SYMBOL

HARNESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "acceptance_d5_5_multi_order.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("acceptance_d5_5_multi_order", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


harness = _load_harness()


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def _fill(
    side: Side,
    quantity: float,
    price: float,
    *,
    fill_id: str,
    broker_fill_id: str | None = None,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=new_order_id(),
        timestamp=utcnow(),
        symbol=SYMBOL,
        side=side,
        quantity=quantity,
        price=price,
        broker_fill_id=broker_fill_id,
    )


def _engine(*, execution_enabled: bool = True):
    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.market_data.max_future_skew_seconds = 86400 * 30
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.execution.enabled = execution_enabled
    return build_runtime(config, get_env()).engine


def _intent(quantity: float, *, side: Side = Side.BUY, direction: Direction = Direction.LONG) -> OrderIntent:
    return OrderIntent(
        order_id=new_order_id(),
        client_order_id=new_order_id(),
        timestamp=utcnow(),
        symbol=SYMBOL,
        side=side,
        direction=direction,
        quantity=quantity,
        order_type=OrderType.MARKET,
        session_id=new_session_id(),
    )


def _apply_everywhere(engine, fill: Fill, *, side_orders: bool = True) -> None:
    """Apply one fill exactly the way the production fill bridge does.

    Ledger (authoritative) -> OMS -> PositionManager, and the broker's own
    position book, so all four accounting views observe the same fill.
    """
    engine.fill_ledger.add_fill(fill)
    if side_orders:
        direction = Direction.LONG if fill.side is Side.BUY else Direction.SHORT
        order = engine.oms.create(_intent(fill.quantity, side=fill.side, direction=direction))
        engine.oms.apply_fill(order, fill)
    engine.position_manager.apply_fill(fill, stop=None, target=None)
    book = engine.broker._positions.setdefault(fill.symbol, PositionManager(fill.symbol))
    book.apply_fill(fill)


async def _connected_mock_engine(*, execution_enabled: bool = True):
    engine = _engine(execution_enabled=execution_enabled)
    engine.broker.set_price(SYMBOL, 30000.0)
    await engine.broker.connect()
    engine.oms.broker = engine.broker
    return engine


# ---------------------------------------------------------------------------
# 1. Multi-order / opposing fill accounting (the BUG-D5-001 core)
# ---------------------------------------------------------------------------
def test_single_entry_is_identical_in_ledger_oms_and_position_manager():
    engine = _engine()
    fill = _fill(Side.BUY, 0.001, 30000.0, fill_id="f1", broker_fill_id="b1")
    _apply_everywhere(engine, fill)

    assert _close(engine.fill_ledger.net_quantity(SYMBOL), 0.001)
    assert _close(engine.oms.net_quantity(SYMBOL), 0.001)
    assert _close(engine.position_manager.position.quantity, 0.001)


def test_cumulative_broker_fill_deltas_are_applied_once():
    """Broker polls report CUMULATIVE filled quantity; only the delta counts."""
    engine = _engine()
    order = engine.oms.create(_intent(0.001))
    synced = 0.0
    for cumulative in (0.0004, 0.0008, 0.001):
        delta = cumulative - synced
        if delta <= 0:
            continue
        fill = _fill(Side.BUY, delta, 30000.0, fill_id=new_fill_id())
        engine.fill_ledger.add_fill(fill)
        engine.oms.apply_fill(order, fill)
        engine.position_manager.apply_fill(fill, stop=None, target=None)
        synced = cumulative

    assert order.status is OrderStatus.FILLED
    assert _close(engine.fill_ledger.net_quantity(SYMBOL), 0.001)
    assert _close(engine.oms.net_quantity(SYMBOL), 0.001)
    assert _close(engine.position_manager.position.quantity, 0.001)


def test_duplicate_broker_fill_is_ignored_by_every_view():
    engine = _engine()
    fill = _fill(Side.BUY, 0.001, 30000.0, fill_id="f1", broker_fill_id="brk-1")
    _apply_everywhere(engine, fill)
    engine.fill_ledger.add_fill(fill)          # replay
    order = engine.oms.all_orders()[0]
    engine.oms.apply_fill(order, fill)         # replay
    engine.position_manager.apply_fill(fill, stop=None, target=None)  # replay

    assert engine.fill_ledger.duplicates_ignored >= 1
    assert _close(engine.fill_ledger.net_quantity(SYMBOL), 0.001)
    assert _close(engine.oms.net_quantity(SYMBOL), 0.001)
    assert _close(engine.position_manager.position.quantity, 0.001)


def test_multiple_orders_accumulate_consistently():
    engine = _engine()
    _apply_everywhere(engine, _fill(Side.BUY, 0.001, 30000.0, fill_id="a", broker_fill_id="ba"))
    _apply_everywhere(engine, _fill(Side.BUY, 0.001, 30100.0, fill_id="b", broker_fill_id="bb"))

    assert _close(engine.fill_ledger.net_quantity(SYMBOL), 0.002)
    assert _close(engine.oms.net_quantity(SYMBOL), 0.002)
    assert _close(engine.position_manager.position.quantity, 0.002)
    assert len(engine.oms.all_orders()) == 2


def test_opposing_orders_net_to_zero_in_every_view():
    engine = _engine()
    _apply_everywhere(engine, _fill(Side.BUY, 0.001, 30000.0, fill_id="a", broker_fill_id="ba"))
    _apply_everywhere(engine, _fill(Side.SELL, 0.001, 30050.0, fill_id="b", broker_fill_id="bb",
                                    ))

    assert _close(engine.fill_ledger.net_quantity(SYMBOL), 0.0)
    assert _close(engine.oms.net_quantity(SYMBOL), 0.0)
    assert engine.position_manager.is_flat
    # Broker book closed too, so all four views agree on zero.
    assert engine.broker._positions[SYMBOL].is_flat


def test_reversal_flips_short_consistently_in_every_view():
    engine = _engine()
    _apply_everywhere(engine, _fill(Side.BUY, 0.002, 30000.0, fill_id="a", broker_fill_id="ba"))
    _apply_everywhere(engine, _fill(Side.SELL, 0.005, 30000.0, fill_id="b", broker_fill_id="bb"))

    assert _close(engine.fill_ledger.net_quantity(SYMBOL), -0.003)
    assert _close(engine.oms.net_quantity(SYMBOL), -0.003)
    assert _close(engine.position_manager.position.quantity, 0.003)
    assert engine.position_manager.position.direction is Direction.SHORT


def test_oms_net_quantity_ignores_unfilled_orders():
    """A submitted-but-unfilled order must never look like a position."""
    engine = _engine()
    engine.oms.create(_intent(0.001))
    assert _close(engine.oms.net_quantity(SYMBOL), 0.0)
    assert _close(engine.fill_ledger.net_quantity(SYMBOL), 0.0)
    assert engine.position_manager.is_flat


# ---------------------------------------------------------------------------
# 2. Order-status synchronisation (BUG-D3-002 probe, fresh evidence)
# ---------------------------------------------------------------------------
class _GrowingBroker(MockBroker):
    """Broker that acknowledges an order and fills it incrementally on polls."""

    def __init__(self, step: float, fill_price: float) -> None:
        super().__init__()
        self.step = step
        self.fill_price = fill_price
        self._cumulative: dict[str, float] = {}

    async def submit_order(self, order):  # noqa: ANN001 - test double
        from app.brokers.base import BrokerExecution

        if not self._connected:
            return BrokerExecution(order.order_id, OrderStatus.REJECTED, reject_reason="not_connected")
        broker_order_id = new_order_id()
        self._cumulative[order.order_id] = 0.0
        view = order.model_copy(deep=True)
        view.broker_order_id = broker_order_id
        view.status = OrderStatus.ACKNOWLEDGED
        view.filled_quantity = 0.0
        self._orders[order.order_id] = view
        return BrokerExecution(broker_order_id, OrderStatus.ACKNOWLEDGED, fills=[])

    async def get_order(self, order):  # noqa: ANN001 - test double
        current = self._cumulative.get(order.order_id, 0.0)
        new_total = min(order.quantity, current + self.step)
        self._cumulative[order.order_id] = new_total
        view = order.model_copy(deep=True)
        view.filled_quantity = new_total
        view.average_fill_price = self.fill_price
        view.status = (
            OrderStatus.FILLED if new_total >= order.quantity - 1e-12
            else OrderStatus.PARTIALLY_FILLED
        )
        view.updated_at = utcnow()
        self._orders[order.order_id] = view
        return view


async def test_order_status_sync_applies_only_unseen_cumulative_deltas():
    engine = await _connected_mock_engine()
    broker = _GrowingBroker(step=0.0004, fill_price=30000.0)
    broker.set_price(SYMBOL, 30000.0)
    await broker.connect()
    engine.broker = broker
    engine.oms.broker = broker

    order = engine.oms.create(_intent(0.001))
    submitted = await engine._submit(order)
    assert submitted is True
    assert order.status is OrderStatus.ACKNOWLEDGED
    assert order.filled_quantity == 0.0

    base = utcnow()
    for poll in range(1, 4):
        engine._last_order_sync = None
        await engine._resync_orders_from_broker(base + timedelta(seconds=5 * poll))
        expected = min(0.001, 0.0004 * poll)
        ledger = engine.fill_ledger.net_quantity(SYMBOL)
        oms = engine.oms.net_quantity(SYMBOL)
        pm = engine.position_manager.position.quantity
        assert _close(ledger, expected), f"poll {poll}: ledger={ledger}"
        assert _close(oms, expected), f"poll {poll}: oms={oms}"
        assert _close(pm, expected), f"poll {poll}: pm={pm}"
        assert _close(ledger, oms) and _close(oms, pm)

    assert order.status is OrderStatus.FILLED
    assert _close(order.filled_quantity, 0.001)
    # A further poll of a terminal order must not move any view.
    engine._last_order_sync = None
    await engine._resync_orders_from_broker(base + timedelta(seconds=60))
    assert _close(engine.fill_ledger.net_quantity(SYMBOL), 0.001)
    assert _close(engine.oms.net_quantity(SYMBOL), 0.001)
    assert _close(engine.position_manager.position.quantity, 0.001)


# ---------------------------------------------------------------------------
# 3. Four-way snapshot (the harness's authoritative comparison)
# ---------------------------------------------------------------------------
async def test_snapshot_converges_on_a_flat_zero_position():
    engine = await _connected_mock_engine()
    snap = await harness.snapshot(engine, "zero")

    assert _close(snap["broker_net"], 0.0)
    assert _close(snap["ledger_net"], 0.0)
    assert _close(snap["oms_net"], 0.0)
    assert _close(snap["pm_net"], 0.0)
    assert snap["comparisons"]["overall"] is True


async def test_snapshot_reports_a_position_manager_ledger_divergence():
    engine = await _connected_mock_engine()
    engine.position_manager.apply_fill(_fill(Side.BUY, 0.001, 30000.0, fill_id="only_pm"),
                                       stop=None, target=None)

    snap = await harness.snapshot(engine, "divergent")

    assert snap["comparisons"]["oms_vs_pm"] is False
    assert snap["comparisons"]["broker_vs_pm"] is False
    assert snap["comparisons"]["overall"] is False


async def test_snapshot_lists_untracked_broker_symbols_instead_of_ignoring_them():
    engine = await _connected_mock_engine()
    other = PositionManager("ETH/USD")
    other.apply_fill(
        Fill(fill_id=new_fill_id(), order_id=new_order_id(), timestamp=utcnow(),
             symbol="ETH/USD", side=Side.BUY, quantity=1.0, price=2000.0),
        stop=None, target=None,
    )
    engine.broker._positions["ETH/USD"] = other

    snap = await harness.snapshot(engine, "untracked_symbol")

    untracked = [p for p in snap["broker_positions"] if p["symbol"] == "ETH/USD"]
    assert len(untracked) == 1
    assert untracked[0]["tracked_locally"] is False
    # The foreign symbol is surfaced, never silently folded into BTC/USD net.
    assert _close(snap["broker_net"], 0.0)


# ---------------------------------------------------------------------------
# 4. Flatten / zero position / broker-side untracked position
# ---------------------------------------------------------------------------
async def test_flatten_closes_a_tracked_position_and_all_views_return_to_zero():
    engine = await _connected_mock_engine()
    _apply_everywhere(engine, _fill(Side.BUY, 0.001, 30000.0, fill_id="entry",
                                    broker_fill_id="b-entry"))
    assert not engine.position_manager.is_flat

    flat = await engine.flatten(force=True, session_closeout=False)

    assert flat is True
    assert engine.position_manager.is_flat
    assert engine.broker._positions[SYMBOL].is_flat
    assert _close(engine.fill_ledger.net_quantity(SYMBOL), 0.0)
    assert _close(engine.oms.net_quantity(SYMBOL), 0.0)

    snap = await harness.snapshot(engine, "after_flatten")
    assert snap["comparisons"]["overall"] is True
    assert _close(snap["broker_net"], 0.0)


async def test_flatten_closes_a_broker_only_position_that_local_state_does_not_track():
    engine = await _connected_mock_engine()
    book = engine.broker._positions.setdefault(SYMBOL, PositionManager(SYMBOL))
    book.apply_fill(
        Fill(fill_id=new_fill_id(), order_id=new_order_id(), timestamp=utcnow(),
             symbol=SYMBOL, side=Side.BUY, quantity=0.001, price=30000.0),
        stop=None, target=None,
    )
    assert engine.position_manager.is_flat

    flat = await engine.flatten(force=True, session_closeout=False)

    assert flat is True
    assert engine.broker._positions[SYMBOL].is_flat
    # Local views were never touched by a position they do not track.
    assert _close(engine.fill_ledger.net_quantity(SYMBOL), 0.0)
    assert _close(engine.oms.net_quantity(SYMBOL), 0.0)


# ---------------------------------------------------------------------------
# 5. Reconciliation: success and failure
# ---------------------------------------------------------------------------
async def test_reconcile_succeeds_when_broker_and_local_state_both_flat():
    engine = await _connected_mock_engine()
    assert await engine.reconcile() is True
    assert engine._need_reconciliation is False
    assert engine.state.reconciliation["ok"] is True


async def test_reconcile_fails_when_the_broker_holds_an_untracked_position():
    engine = await _connected_mock_engine()
    book = engine.broker._positions.setdefault(SYMBOL, PositionManager(SYMBOL))
    book.apply_fill(
        Fill(fill_id=new_fill_id(), order_id=new_order_id(), timestamp=utcnow(),
             symbol=SYMBOL, side=Side.BUY, quantity=0.001, price=30000.0),
        stop=None, target=None,
    )

    assert await engine.reconcile() is False
    assert engine._need_reconciliation is True
    assert engine.state.reconciliation["ok"] is False
    assert engine.state.reconciliation["discrepancies"]
    # A failed reconciliation must block new entries.
    assert engine._build_risk_context(utcnow()).reconciliation_ok is False


# ---------------------------------------------------------------------------
# 6. Safety rails: execution disabled, paper only, repository config
# ---------------------------------------------------------------------------
async def test_execution_disabled_refuses_every_submission():
    engine = await _connected_mock_engine(execution_enabled=False)
    order = engine.oms.create(_intent(0.001))
    events: list = []
    engine.bus.subscribe_all(events.append)

    assert await engine._submit(order) is False
    assert engine.oms.all_orders()
    assert order.broker_order_id is None
    assert order.status is OrderStatus.NEW
    assert EventType.EXECUTION_BLOCKED in {e.type for e in events}
    assert engine.broker._orders == {}
    assert engine.position_manager.is_flat


def test_live_trading_mode_is_refused_before_any_order():
    # First gate: TradingConfig's model validator accepts ONLY "paper".
    with pytest.raises(ConfigError):
        load_config(env=EnvSettings(trading_mode="live"))


def test_live_alpaca_endpoint_is_refused_before_any_order():
    with pytest.raises(PaperOnlyViolation):
        load_config(env=EnvSettings(apca_api_base_url="https://api.alpaca.markets"))


def test_disabled_alpaca_paper_flag_is_refused():
    with pytest.raises(PaperOnlyViolation):
        load_config(env=EnvSettings(alpaca_paper=False))


def test_repository_configuration_is_paper_with_execution_disabled():
    config = load_config(env=get_env())
    assert config.trading.mode == "paper"
    assert config.execution.enabled is False


def test_harness_safety_preflight_rejects_an_enabled_repository_gate():
    config = load_config(env=get_env())
    config.execution.enabled = True  # the one thing the harness must never see
    checks = harness.safety_preflight(get_env(), config)
    assert checks["repo_execution_enabled_is_false"] is False
    assert all(checks.values()) is False


def test_harness_limits_stay_within_the_hard_quantity_ceiling():
    assert harness.LIMITS["max_quantity_per_order"] <= harness.HARD_QUANTITY_CEILING
    assert harness.LIMITS["max_open_exposure"] <= harness.HARD_QUANTITY_CEILING
    assert harness.LIMITS["max_orders"] >= 2  # a multi-order proof needs >= 2


def test_harness_stop_trigger_crosses_the_stop_but_stays_within_ten_percent():
    manager = PositionManager(SYMBOL)
    manager.apply_fill(
        Fill(fill_id=new_fill_id(), order_id=new_order_id(), timestamp=utcnow(),
             symbol=SYMBOL, side=Side.BUY, quantity=0.001, price=100.0),
        stop=95.0, target=110.0,
    )
    position = manager.position
    price = harness.stop_trigger_price(position)
    assert price < position.stop
    assert price >= position.average_entry * 0.90
    assert price > 0
