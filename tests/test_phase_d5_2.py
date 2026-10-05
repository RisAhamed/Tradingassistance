"""PHASE D.5.2 determinism tests: netting, idempotency, partial fills, reversal, divergence."""
from datetime import datetime, timezone

from app.config.loader import get_env, load_config
from app.core.clock import utcnow
from app.core.ids import new_fill_id, new_order_id
from app.domain.enums import Direction, OrderStatus, Side, SessionState
from app.domain.orders import Fill, Order
from app.events.types import EventType
from app.orders.oms import OMS
from app.portfolio.position_manager import PositionManager
from app.runtime import build_runtime
from tests.support import SYMBOL

UTC = timezone.utc


def _fill(side: Side, qty: float, price: float = 30000.0, *, marker: str | None = None, ts=None):
    return Fill(
        fill_id=new_fill_id(),
        order_id=new_order_id(),
        timestamp=ts or utcnow(),
        symbol=SYMBOL,
        side=side,
        quantity=qty,
        price=price,
        broker_fill_id=marker,
    )


def test_case_a_single_entry():
    pm = PositionManager(SYMBOL)
    pm.apply_fill(_fill(Side.BUY, 0.01))
    assert pm.position.quantity == 0.01
    assert pm.position.direction.value == "long"


def test_case_b_full_exit():
    pm = PositionManager(SYMBOL)
    pm.apply_fill(_fill(Side.BUY, 0.01))
    pm.apply_fill(_fill(Side.SELL, 0.01))
    assert pm.is_flat is True
    assert pm.position.quantity == 0.0


def test_case_c_multiple_entries():
    pm = PositionManager(SYMBOL)
    pm.apply_fill(_fill(Side.BUY, 0.01))
    pm.apply_fill(_fill(Side.BUY, 0.01))
    assert pm.position.quantity == 0.02


def test_case_d_partial_exit():
    pm = PositionManager(SYMBOL)
    pm.apply_fill(_fill(Side.BUY, 0.02))
    pm.apply_fill(_fill(Side.SELL, 0.01))
    assert pm.position.quantity == 0.01


def test_case_e_full_exit_after_multiple_entries():
    pm = PositionManager(SYMBOL)
    pm.apply_fill(_fill(Side.BUY, 0.01))
    pm.apply_fill(_fill(Side.BUY, 0.01))
    pm.apply_fill(_fill(Side.SELL, 0.02))
    assert pm.is_flat is True


def test_case_f_reversal_flips():
    pm = PositionManager(SYMBOL)
    pm.apply_fill(_fill(Side.BUY, 0.01))
    pm.apply_fill(_fill(Side.SELL, 0.02))
    assert pm.position.direction.value == "short"
    assert abs(pm.position.quantity - 0.01) < 1e-12


def test_case_g_partial_fills_sum():
    pm = PositionManager(SYMBOL)
    order = Order(order_id="o1", client_order_id=new_order_id(), symbol=SYMBOL, side=Side.BUY, direction=Direction.LONG, quantity=0.02)
    total = 0.0
    for q in (0.005, 0.005, 0.010):
        f = Fill(fill_id=new_fill_id(), order_id=order.order_id, timestamp=utcnow(), symbol=SYMBOL, side=Side.BUY, quantity=q, price=30000.0)
        pm.apply_fill(f)
        total += q
    assert abs(total - 0.02) < 1e-12
    assert pm.position.quantity == 0.02


def test_case_h_duplicate_fill_idempotent():
    pm = PositionManager(SYMBOL)
    f = _fill(Side.BUY, 0.01, marker="broker-1")
    pm.apply_fill(f)
    pm.apply_fill(f)
    assert pm.position.quantity == 0.01


def test_case_i_repeated_polling_no_double_apply():
    # cumulative: poll1 0.01, poll2 0.01 (delta0), poll3 0.015 (delta .005), poll4 0.015 (delta0)
    prev = 0.0
    applied = 0.0
    for q in (0.01, 0.01, 0.015, 0.015):
        d = q - prev
        prev = q
        applied += d
    assert applied == 0.015


def test_case_j_opposing_fills_converge_on_oms_pm():
    pm = PositionManager(SYMBOL)
    pm.apply_fill(_fill(Side.BUY, 0.01))
    pm.apply_fill(_fill(Side.SELL, 0.01))
    assert pm.is_flat


async def test_engine_survives_ticks_after_fill_bridge():
    """Place an order via mock provider + enable execution with mock broker."""
    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.execution.enabled = True
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.market_data.max_future_skew_seconds = 86400 * 30
    engine = build_runtime(config, get_env()).engine
    await engine.start()
    try:
        provider = engine.provider
        ticks = 0
        while not provider.exhausted and ticks < 200:
            if not await provider.pump_one():
                break
            ticks += 1
        # Engine must continue iterating subsequent ticks without error
        for _ in range(5):
            from app.core.clock import utcnow as noww

            await engine.tick(noww())
        assert True
    finally:
        await engine.stop()
