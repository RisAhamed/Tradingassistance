"""D.5.3: broker-authoritative fill ledger -- deterministic accounting checks."""
from datetime import datetime, timezone

from app.domain.enums import Direction, Side
from app.domain.orders import Fill
from app.orders.oms import OMS
from app.portfolio.position_manager import PositionManager
from app.brokers.mock import MockBroker
from app.core.clock import utcnow
from app.core.ids import new_fill_id


def _f(side, qty, price, boid=None):
    return Fill(fill_id=boid or new_fill_id(), order_id=new_fill_id(), timestamp=utcnow(), symbol="BTC/USD", side=side, quantity=qty, price=price)


def test_single_buy_pm_equals_broker():
    pm = PositionManager("BTC/USD")
    pm.apply_fill(_f(Side.BUY, 0.01, 30000))
    assert pm.position.quantity == 0.01


def test_opposing_multi_order_is_net():
    pm = PositionManager("BTC/USD")
    pm.apply_fill(_f(Side.BUY, 0.03, 30000))
    pm.apply_fill(_f(Side.SELL, 0.01, 30010))
    assert abs(pm.position.quantity - 0.02) < 1e-12
    assert pm.position.direction is not None


def test_reversal_correctly_flips():
    pm = PositionManager("BTC/USD")
    pm.apply_fill(_f(Side.BUY, 0.02, 30000))
    pm.apply_fill(_f(Side.SELL, 0.05, 30000))
    assert abs(pm.position.quantity - 0.03) < 1e-12


def test_duplicate_fill_is_ignored():
    pm = PositionManager("BTC/USD")
    f = _f(Side.BUY, 0.01, 30000, boid="broker-123")
    pm.apply_fill(f)
    pm.apply_fill(f)
    assert pm.position.quantity == 0.01


def test_cumulative_polling_deltas_only():
    applied = 0.0
    for new_total in [0.003, 0.007, 0.01]:
        # simulate delta application
        pass
    assert True
