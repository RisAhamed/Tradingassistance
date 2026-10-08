"""Phase J.1: configurable poll window + engine-poll fill proof (fakes only)."""
import asyncio
import hashlib
import time
from pathlib import Path

import pytest

from app.acceptance import direct as direct_mod
from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.clock import utcnow
from app.core.ids import new_order_id
from app.domain.enums import Direction, OrderStatus, Side
from app.domain.orders import Order, OrderIntent
from app.events.types import EventType
from app.portfolio.position_manager import PositionManager
from tests.test_phase_j_acceptance import FakeAlpacaBroker, _config, _env

CONFIG_YAML = PROJECT_ROOT / "configs" / "config.yaml"


@pytest.fixture(autouse=True)
def _offline_provider(monkeypatch):
    from app.market_data.mock import MockMarketDataProvider

    monkeypatch.setattr(
        "app.runtime.create_provider",
        lambda config, env, bus=None: MockMarketDataProvider(config.trading.symbol))


@pytest.fixture(autouse=True)
def _config_yaml_untouched():
    before = hashlib.sha256(CONFIG_YAML.read_bytes()).hexdigest()
    yield
    assert hashlib.sha256(CONFIG_YAML.read_bytes()).hexdigest() == before


class DelayedBroker(FakeAlpacaBroker):
    """Fills only after a delay (time-based) or N get_order calls."""

    def __init__(self, *, fill_after_seconds=None, fill_after_calls=None):
        super().__init__(mode="ambiguous")
        self._t0 = time.monotonic()
        self.fill_after_seconds = fill_after_seconds
        self.fill_after_calls = fill_after_calls
        self._get_calls = 0
        self._delayed_order_id: str | None = None

    def _due(self, order) -> bool:
        # Only the FIRST submitted order is delayed; exits must fill at
        # once so the close chain can converge.
        if self._delayed_order_id is None:
            self._delayed_order_id = order.order_id
        if order.order_id != self._delayed_order_id:
            return True
        if self.fill_after_calls is not None:
            return self._get_calls >= self.fill_after_calls
        if self.fill_after_seconds is not None:
            return time.monotonic() - self._t0 >= self.fill_after_seconds
        return False

    async def submit_order(self, order):
        # The delayed order goes through the slow path; every later order
        # (exits) fills immediately so the close chain can converge.
        if self._delayed_order_id is not None and order.order_id != self._delayed_order_id:
            from app.brokers.base import BrokerExecution as _Exec

            self.calls.append(f"submit:{order.order_id}")
            self._orders[order.order_id] = order
            book = self._book.setdefault(order.symbol, PositionManager(order.symbol))
            fill = self._fill(order, order.quantity)
            book.apply_fill(fill)
            return _Exec(order.order_id, OrderStatus.FILLED, fills=[fill])
        return await super().submit_order(order)

    async def get_order(self, order: Order) -> Order:
        self.calls.append(f"get_order:{order.order_id}")
        self._get_calls += 1
        if self._due(order):
            order.status = OrderStatus.FILLED
            order.filled_quantity = order.quantity
            order.average_fill_price = 30000.0
            book = self._book.setdefault(order.symbol, PositionManager(order.symbol))
            book.apply_fill(self._fill(order, order.quantity))
        else:
            order.status = OrderStatus.SUBMITTED
        return order


# --- 1. poll window config ------------------------------------------------------
def test_poll_window_defaults_and_bounds():
    from app.config.models import LiveAcceptanceConfig

    cfg = LiveAcceptanceConfig()
    assert cfg.poll_window_seconds == 60
    assert cfg.poll_interval_seconds == 1.0
    assert LiveAcceptanceConfig(poll_window_seconds=60).poll_window_seconds == 60
    with pytest.raises(ValueError):
        LiveAcceptanceConfig(poll_window_seconds=10)
    with pytest.raises(ValueError):
        LiveAcceptanceConfig(poll_window_seconds=4000)


# --- 2. engine continuous poll observes a delayed fill ---------------------------
@pytest.mark.asyncio
async def test_engine_resync_loop_emits_filled_on_delayed_fill(monkeypatch):
    from app.runtime import build_runtime

    fake = DelayedBroker(fill_after_seconds=45.0)
    monkeypatch.setattr("app.runtime.create_broker", lambda config, env: fake)
    config, env = _config(), _env()
    config.execution.enabled = True  # in-process only
    config.execution.order_sync_interval_seconds = 0.5
    engine = build_runtime(config, env).engine
    seen: list[str] = []
    engine.bus.subscribe_all(
        lambda e: seen.append(e.type.value) if e.type is EventType.ORDER_FILLED else None)
    order = engine.oms.create(OrderIntent(
        order_id=new_order_id(), client_order_id=new_order_id(), timestamp=utcnow(),
        symbol="BTC/USD", side=Side.BUY, direction=Direction.LONG, quantity=0.001))
    submitted = await engine.oms.submit(order)
    assert submitted is not None
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        await engine._resync_orders_from_broker(utcnow())
        if "OrderFilled" in seen:
            break
        await asyncio.sleep(0.5)
    assert "OrderFilled" in seen, "engine resync loop must observe the delayed fill"
    assert not engine.position_manager.is_flat
    config.execution.enabled = False


# --- 3. harness window honored on never-fill --------------------------------------
@pytest.mark.asyncio
async def test_harness_window_honored_without_retry(monkeypatch):
    fake = FakeAlpacaBroker(mode="ambiguous")
    monkeypatch.setattr("app.brokers.alpaca.AlpacaPaperBroker", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr("app.runtime.create_broker", lambda config, env: fake)
    config, env = _config(), _env()
    config.live_acceptance.poll_window_seconds = 60.0
    t0 = time.monotonic()
    code, report = await direct_mod.run_direct(config, env, quantity=0.001, timeout=120.0)
    elapsed = time.monotonic() - t0
    assert code == 4 and report["outcome"] == "ambiguous_submission"
    assert report["poll_window_seconds"] == 60.0
    assert elapsed < 120.0  # bounded by the window, not the timeout
    assert len([c for c in fake.calls if c.startswith("submit:")]) == 1


# --- 4. deferred fill via engine catch-up --------------------------------------------
@pytest.mark.asyncio
async def test_deferred_fill_converges_via_engine_catchup(monkeypatch):
    # Fill appears only after the harness window closes (call-count flip),
    # so the engine catch-up — not the sync wait — must observe it.
    fake = DelayedBroker(fill_after_calls=12)
    monkeypatch.setattr("app.brokers.alpaca.AlpacaPaperBroker", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr("app.runtime.create_broker", lambda config, env: fake)
    config, env = _config(), _env()
    config.live_acceptance.poll_window_seconds = 2.0
    config.live_acceptance.poll_interval_seconds = 0.5
    config.execution.order_sync_interval_seconds = 0.1
    code, report = await direct_mod.run_direct(config, env, quantity=0.001, timeout=30.0)
    assert code == 0, report
    assert report["outcome"] == "pass_deferred_fill"
    assert report["engine_poll_observed_fill"] is True
    assert report["flatten"]["final_broker_zero"] is True
    assert report["reconciliation"]["ok"] is True


# --- 5. schema keys on every path ----------------------------------------------------------
@pytest.mark.asyncio
async def test_schema_keys_present_on_ambiguous_path(monkeypatch):
    fake = FakeAlpacaBroker(mode="ambiguous")
    monkeypatch.setattr("app.brokers.alpaca.AlpacaPaperBroker", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr("app.runtime.create_broker", lambda config, env: fake)
    config, env = _config(), _env()
    config.live_acceptance.poll_window_seconds = 30.0
    _, report = await direct_mod.run_direct(config, env, quantity=0.001, timeout=5.0)
    for key in ("engine_poll_observed_fill", "engine_poll_first_seen_at",
                "harness_sync_first_seen_at", "fill_latency_seconds",
                "poll_window_seconds", "poll_count", "poll_error_count"):
        assert key in report, key
