"""Phase A #7: fill / position safety.

Invariant: position state derives from actual fills (never submitted orders),
partial fills accumulate, duplicate fill events never double-count, and the
average entry is a quantity-weighted mean.
"""
from app.brokers.mock import MockBroker
from app.core.clock import utcnow
from app.core.ids import new_order_id
from app.domain.enums import Direction, OrderStatus, OrderType, Side
from app.domain.orders import OrderIntent
from app.orders.oms import OMS
from app.portfolio.position_manager import PositionManager
from tests.support import SYMBOL, make_fill


def _intent(quantity: float) -> OrderIntent:
    return OrderIntent(
        order_id=new_order_id(),
        client_order_id=new_order_id(),
        timestamp=utcnow(),
        symbol=SYMBOL,
        side=Side.BUY,
        direction=Direction.LONG,
        quantity=quantity,
        order_type=OrderType.MARKET,
    )


def test_duplicate_fill_does_not_double_count():
    manager = PositionManager(SYMBOL)
    fill = make_fill(2.0, 100.0, Side.BUY, fill_id="f1")
    manager.apply_fill(fill)
    manager.apply_fill(fill)  # exact replay
    assert manager.position.quantity == 2.0


def test_partial_fills_compute_weighted_average_entry():
    manager = PositionManager(SYMBOL)
    manager.apply_fill(make_fill(1.0, 100.0, Side.BUY, fill_id="f1"))
    manager.apply_fill(make_fill(1.0, 120.0, Side.BUY, fill_id="f2"))
    assert manager.position.quantity == 2.0
    assert manager.position.average_entry == 110.0


def test_invalid_fill_never_corrupts_position():
    manager = PositionManager(SYMBOL)
    manager.apply_fill(make_fill(0.0, 100.0, Side.BUY, fill_id="f0"))
    manager.apply_fill(make_fill(1.0, -5.0, Side.BUY, fill_id="fneg"))
    assert manager.is_flat


def test_position_reflects_actual_fill_not_order_quantity():
    manager = PositionManager(SYMBOL)
    # A partial fill of an order that requested 10 units.
    manager.apply_fill(make_fill(4.0, 100.0, Side.BUY, fill_id="f_partial"))
    assert manager.position.quantity == 4.0


def test_oms_tracks_partial_fill_then_full_and_dedupes_replays():
    oms = OMS(MockBroker())
    order = oms.create(_intent(2.0))
    oms.apply_fill(order, make_fill(1.0, 100.0, Side.BUY, order_id=order.order_id, broker_fill_id="bf1"))
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == 1.0
    # Replay of the same broker fill id must be ignored.
    oms.apply_fill(order, make_fill(1.0, 100.0, Side.BUY, order_id=order.order_id, broker_fill_id="bf1"))
    assert order.filled_quantity == 1.0
    oms.apply_fill(order, make_fill(1.0, 120.0, Side.BUY, order_id=order.order_id, broker_fill_id="bf2"))
    assert order.filled_quantity == 2.0
    assert order.status is OrderStatus.FILLED
    assert order.average_fill_price == 110.0


def test_closing_fill_updates_realized_pnl_and_flattens():
    manager = PositionManager(SYMBOL)
    manager.apply_fill(make_fill(1.0, 100.0, Side.BUY, fill_id="open"))
    update = manager.apply_fill(make_fill(1.0, 120.0, Side.SELL, fill_id="close"))
    assert manager.is_flat
    assert update.closed
    assert update.realized_delta == 20.0
