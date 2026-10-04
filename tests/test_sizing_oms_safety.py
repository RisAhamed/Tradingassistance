"""Phase A #5 (position sizing) and #6 (order idempotency)."""
import pytest

from app.brokers.mock import MockBroker
from app.config.models import PositionSizingConfig
from app.core.clock import utcnow
from app.core.ids import new_order_id
from app.domain.enums import Direction, OrderStatus, OrderType, Side
from app.domain.orders import OrderIntent
from app.execution.executor import OrderExecutor
from app.orders.oms import DuplicateOrderError, OMS
from app.portfolio.position_sizing import PositionSizer
from tests.support import SYMBOL, make_signal


def _intent(quantity: float, *, order_id: str | None = None, client_order_id: str | None = None) -> OrderIntent:
    return OrderIntent(
        order_id=order_id or new_order_id(),
        client_order_id=client_order_id or new_order_id(),
        timestamp=utcnow(),
        symbol=SYMBOL,
        side=Side.BUY,
        direction=Direction.LONG,
        quantity=quantity,
        order_type=OrderType.MARKET,
    )


# --- #5 position-sizing safety ---------------------------------------------
def test_sizer_rejects_zero_or_missing_stop_distance():
    sizer = PositionSizer(PositionSizingConfig())
    zero = sizer.size(make_signal(stop_distance=0.0), 100000.0, reference_price=30000.0, max_notional=20000.0)
    assert not zero.ok and zero.rejected_reason == "invalid_stop_distance"
    no_stop = sizer.size(make_signal(stop_reference=None), 100000.0, reference_price=30000.0, max_notional=20000.0)
    assert not no_stop.ok and no_stop.rejected_reason == "invalid_stop_distance"


def test_sizer_rejects_nonpositive_or_nonfinite_equity():
    sizer = PositionSizer(PositionSizingConfig())
    for equity in (0.0, -100.0, float("nan")):
        result = sizer.size(make_signal(), equity, reference_price=30000.0, max_notional=20000.0)
        assert not result.ok
        assert result.rejected_reason == "non_positive_equity"


def test_sizer_rejects_malformed_reference_price_and_cap():
    sizer = PositionSizer(PositionSizingConfig())
    bad_price = sizer.size(make_signal(), 100000.0, reference_price=-1.0, max_notional=20000.0)
    assert not bad_price.ok and bad_price.rejected_reason == "invalid_reference_price"
    bad_cap = sizer.size(make_signal(), 100000.0, reference_price=30000.0, max_notional=float("inf"))
    assert not bad_cap.ok and bad_cap.rejected_reason == "invalid_max_notional"


def test_sizer_never_emits_invalid_quantity():
    sizer = PositionSizer(PositionSizingConfig())
    cases = [
        sizer.size(make_signal(stop_distance=0.0), 100000.0, reference_price=30000.0, max_notional=20000.0),
        sizer.size(make_signal(), 0.0, reference_price=30000.0, max_notional=20000.0),
        sizer.size(make_signal(), 100000.0, reference_price=-5.0, max_notional=20000.0),
        sizer.size(make_signal(stop_distance=1e9), 1.0, reference_price=30000.0, max_notional=20000.0),
    ]
    for result in cases:
        qty = result.final_quantity
        assert qty is not None
        assert qty == qty  # not NaN
        assert qty not in (float("inf"), float("-inf"))
        assert qty >= 0
        if not result.ok:
            assert qty == 0.0


def test_sizer_caps_notional_below_max_value_after_sizing():
    sizer = PositionSizer(PositionSizingConfig())
    result = sizer.size(make_signal(price=30000.0, stop_distance=10.0), 100000.0, reference_price=30000.0, max_notional=20000.0)
    assert result.ok
    assert result.final_quantity * 30000.0 <= 20000.0 + 1e-6


def test_sizer_below_minimum_quantity_is_rejected():
    sizer = PositionSizer(PositionSizingConfig())
    result = sizer.size(make_signal(stop_distance=1e9), 1.0, reference_price=30000.0, max_notional=None)
    assert not result.ok and result.rejected_reason == "below_minimum_quantity"


# --- #6 order idempotency ---------------------------------------------------
def test_oms_rejects_nonpositive_and_nonfinite_quantity():
    oms = OMS(MockBroker())
    for quantity in (0.0, -1.0, float("nan"), float("inf")):
        order = oms.create(_intent(quantity))
        assert oms.validate(order) is False
        assert order.status is OrderStatus.REJECTED
        assert order.reject_reason == "invalid_quantity"


async def test_oms_blocks_duplicate_submission_of_same_order():
    broker = MockBroker()
    broker.set_price(SYMBOL, 100.0)
    await broker.connect()
    oms = OMS(broker)
    order = oms.create(_intent(1.0))
    await oms.submit(order)
    with pytest.raises(DuplicateOrderError):
        await oms.submit(order)


async def test_oms_blocks_client_order_id_rebinding():
    broker = MockBroker()
    broker.set_price(SYMBOL, 100.0)
    await broker.connect()
    oms = OMS(broker)
    first = oms.create(_intent(1.0, order_id="ord_a", client_order_id="cli_shared"))
    oms.create(_intent(1.0, order_id="ord_b", client_order_id="cli_shared"))
    with pytest.raises(DuplicateOrderError):
        await oms.submit(first)


async def test_executor_does_not_blindly_retry_ambiguous_submission():
    class _FlakyBroker(MockBroker):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def submit_order(self, order):  # noqa: ANN001 - test double
            self.calls += 1
            raise TimeoutError("network timeout")

    broker = _FlakyBroker()
    broker.set_price(SYMBOL, 100.0)
    await broker.connect()
    oms = OMS(broker)
    executor = OrderExecutor(oms)
    order = oms.create(_intent(1.0))

    result = await executor.submit(order)
    assert result.ambiguous is True
    assert result.rejected is False
    assert broker.calls == 1  # never retried blindly


async def test_executor_reports_duplicate_as_rejected_not_resubmitted():
    broker = MockBroker()
    broker.set_price(SYMBOL, 100.0)
    await broker.connect()
    oms = OMS(broker)
    executor = OrderExecutor(oms)
    order = oms.create(_intent(1.0))
    await executor.submit(order)

    result = await executor.submit(order)
    assert result.rejected is True
    assert result.reason == "duplicate_submission"
