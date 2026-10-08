"""Phase J: bounded live acceptance harness (mock/fake brokers only).

No test here touches the network. Real-network runs are manual only.
"""
import hashlib
import json
from pathlib import Path

import pytest

from app.acceptance import direct as direct_mod
from app.acceptance import pipeline as pipeline_mod
from app.acceptance.preflight import run_preflight
from app.brokers.base import AccountInfo, BrokerAdapter, BrokerExecution, BrokerHealth
from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.core.clock import utcnow
from app.core.ids import new_fill_id, new_order_id
from app.domain.enums import Direction, OrderStatus, Side
from app.domain.orders import Fill, Order
from app.domain.positions import Position
from app.portfolio.position_manager import PositionManager

CONFIG_YAML = PROJECT_ROOT / "configs" / "config.yaml"


@pytest.fixture(autouse=True)
def _config_yaml_untouched():
    before = hashlib.sha256(CONFIG_YAML.read_bytes()).hexdigest()
    yield
    assert hashlib.sha256(CONFIG_YAML.read_bytes()).hexdigest() == before


class FakeAlpacaBroker(BrokerAdapter):
    """Alpaca-shaped scripted broker. Offline. Records every call."""

    name = "alpaca"
    paper = True
    supports_in_kind_fees = True
    requires_authoritative_qty_on_exit = True
    authoritative_positions_require_connection = True

    def __init__(self, mode: str = "ok"):
        self.mode = mode
        self.calls: list[str] = []
        self._connected = False
        self._book: dict[str, PositionManager] = {}
        self._orders: dict[str, Order] = {}

    async def connect(self) -> None:
        self.calls.append("connect")
        if self.mode == "down":
            raise ConnectionError("broker unreachable")
        self._connected = True

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self._connected = False

    def health(self) -> BrokerHealth:
        return BrokerHealth(connected=self._connected, detail="fake alpaca")

    async def get_account(self) -> AccountInfo:
        self.calls.append("get_account")
        return AccountInfo(equity=100000.0, cash=100000.0, buying_power=100000.0)

    async def get_positions(self) -> list[Position]:
        self.calls.append("get_positions")
        if self.mode == "sticky":
            return [Position(position_id="sticky", symbol="BTC/USD", direction=Direction.LONG,
                             quantity=0.005, average_entry=30000.0)]
        return [pm.position for pm in self._book.values() if not pm.is_flat]

    def _fill(self, order: Order, qty: float, price: float = 30000.0) -> Fill:
        return Fill(fill_id=new_fill_id(), order_id=order.order_id,
                    timestamp=utcnow(), symbol=order.symbol, side=order.side,
                    quantity=qty, price=price, broker_fill_id=new_order_id())

    async def submit_order(self, order: Order) -> BrokerExecution:
        self.calls.append(f"submit:{order.order_id}")
        self._orders[order.order_id] = order
        if self.mode == "reject":
            return BrokerExecution(order.order_id, OrderStatus.REJECTED,
                                   reject_reason="fake_reject")
        book = self._book.setdefault(order.symbol, PositionManager(order.symbol))
        fill = self._fill(order, order.quantity)
        execution = BrokerExecution(new_order_id(), OrderStatus.SUBMITTED,
                                    fills=[] if self.mode == "ambiguous" else [fill])
        if self.mode != "ambiguous":
            book.apply_fill(fill)
        return execution

    async def get_order(self, order: Order) -> Order:
        self.calls.append(f"get_order:{order.order_id}")
        if self.mode == "ambiguous":
            order.status = OrderStatus.SUBMITTED
            return order
        stored = self._orders.get(order.order_id, order)
        stored.status = OrderStatus.FILLED
        stored.filled_quantity = stored.quantity
        stored.average_fill_price = 30000.0
        return stored

    async def get_orders(self, *, status: str | None = None) -> list[Order]:
        return list(self._orders.values())

    async def cancel_order(self, order: Order) -> Order:
        order.status = OrderStatus.CANCELLED
        return order

    async def close_position(self, position: Position) -> BrokerExecution:
        book = self._book.get(position.symbol)
        if book is not None and not book.is_flat:
            side = Side.SELL if position.direction is Direction.LONG else Side.BUY
            book.apply_fill(self._fill(
                Order(order_id="x", client_order_id="x", symbol=position.symbol,
                      side=side, direction=Direction.FLAT,
                      quantity=abs(position.quantity)),
                abs(position.quantity)))
        return BrokerExecution(new_order_id(), OrderStatus.FILLED, fills=[])


def _env(**overrides):
    base = dict(alpaca_api_key="fake", alpaca_api_secret="fake", apca_api_base_url="")
    base.update(overrides)
    from app.config.settings import EnvSettings

    return EnvSettings(**base)


def _config(**overrides):
    config = load_config(env=get_env())
    # Harness requires alpaca/alpaca (asserted, never bypassed). The direct
    # path never connects the provider; pipeline tests patch it to mock.
    config.market_data.provider = "alpaca"
    config.trading.broker = "alpaca"
    config.ai.enabled = False
    config.storage.enabled = False
    config.execution.enabled = False
    config.paper_safety.enabled = False
    config.market_data.max_future_skew_seconds = 86400 * 30
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _engine(config, env):
    from app.runtime import build_runtime

    return build_runtime(config, env).engine


@pytest.fixture(autouse=True)
def _offline_provider(monkeypatch):
    # Every test stays offline: config still says alpaca (asserted by the
    # harness, never bypassed); only the constructed object is a mock.
    from app.market_data.mock import MockMarketDataProvider

    monkeypatch.setattr(
        "app.runtime.create_provider",
        lambda config, env, bus=None: MockMarketDataProvider(config.trading.symbol))


@pytest.fixture
def fake_broker(monkeypatch):
    fake = FakeAlpacaBroker()
    monkeypatch.setattr("app.brokers.alpaca.AlpacaPaperBroker", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr("app.runtime.create_broker", lambda config, env: fake)
    return fake


# --- 1. preflight -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_preflight_all_eleven_checks(monkeypatch, fake_broker):
    config, env = _config(), _env()
    engine = _engine(config, env)
    checks = await run_preflight(config, env, engine)
    assert len(checks) == 11
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert not [c for c in fake_broker.calls if c.startswith("submit:")]


@pytest.mark.asyncio
async def test_preflight_broker_position_blocks(monkeypatch):
    fake = FakeAlpacaBroker(mode="sticky")
    monkeypatch.setattr("app.brokers.alpaca.AlpacaPaperBroker", lambda *a, **k: fake, raising=False)
    config, env = _config(), _env()
    engine = _engine(config, env)
    checks = await run_preflight(config, env, engine)
    flat = next(c for c in checks if c.name == "broker_position_flat")
    assert flat.ok is False
    assert not [c for c in fake.calls if c.startswith("submit:")]


@pytest.mark.asyncio
async def test_preflight_unreachable_broker_blocks(monkeypatch):
    fake = FakeAlpacaBroker(mode="down")
    monkeypatch.setattr("app.brokers.alpaca.AlpacaPaperBroker", lambda *a, **k: fake, raising=False)
    config, env = _config(), _env()
    engine = _engine(config, env)
    checks = await run_preflight(config, env, engine)
    assert next(c for c in checks if c.name == "broker_reachable").ok is False


# --- 2. repository gate tripwire -------------------------------------------------------
@pytest.mark.asyncio
async def test_repository_gate_open_refuses(fake_broker):
    config, env = _config(), _env()
    config.execution.enabled = True  # repository must stay closed
    engine = _engine(config, env)
    checks = await run_preflight(config, env, engine)
    assert next(c for c in checks if c.name == "repository_execution_disabled").ok is False
    assert not [c for c in fake_broker.calls if c.startswith("submit:")]


# --- 3/10. direct happy path --------------------------------------------------------------
@pytest.mark.asyncio
async def test_direct_happy_path(fake_broker, tmp_path):
    config, env = _config(), _env()
    code, report = await direct_mod.run_direct(config, env, quantity=0.001, timeout=10.0)
    assert code == 0 and report["outcome"] == "pass"
    assert report["order"]["status"] == "filled"
    assert report["flatten"]["final_broker_zero"] is True
    assert report["flatten"]["final_local_flat"] is True
    assert report["reconciliation"]["ok"] is True
    assert report["repository_execution_enabled"] is False
    assert config.execution.enabled is False and config.paper_safety.enabled is False
    for key in ("phase", "mode", "started_at", "ended_at", "git_commit", "config_hash",
                "repository_execution_enabled", "symbol", "preflight", "order",
                "position_after_fill", "flatten", "reconciliation", "exit_code", "outcome"):
        assert key in report, key


# --- 4. rejected ------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_direct_rejected_no_retry_no_flatten(monkeypatch):
    fake = FakeAlpacaBroker(mode="reject")
    monkeypatch.setattr("app.brokers.alpaca.AlpacaPaperBroker", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr("app.runtime.create_broker", lambda config, env: fake)
    config, env = _config(), _env()
    code, report = await direct_mod.run_direct(config, env, quantity=0.001, timeout=10.0)
    assert code == 3 and report["outcome"] == "order_rejected"
    assert report["order"]["reject_reason"] == "fake_reject"
    submits = [c for c in fake.calls if c.startswith("submit:")]
    assert len(submits) == 1  # exactly one attempt, never retried
    assert report["flatten"]["attempts"] == 0


# --- 5. ambiguous --------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_direct_ambiguous_reconciles_without_resubmit(monkeypatch):
    fake = FakeAlpacaBroker(mode="ambiguous")
    monkeypatch.setattr("app.brokers.alpaca.AlpacaPaperBroker", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr("app.runtime.create_broker", lambda config, env: fake)
    config, env = _config(), _env()
    code, report = await direct_mod.run_direct(config, env, quantity=0.001, timeout=3.0)
    assert code == 4 and report["outcome"] == "ambiguous_submission"
    assert len([c for c in fake.calls if c.startswith("submit:")]) == 1


# --- 6. flatten failure -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_flatten_failure_via_stuck_position(monkeypatch):
    class StuckBroker(FakeAlpacaBroker):
        async def get_positions(self):
            self.calls.append("get_positions")
            # entry fill lands, but the position can never be closed
            if any(c.startswith("submit:") for c in self.calls):
                return [Position(position_id="stuck", symbol="BTC/USD",
                                 direction=Direction.LONG,
                                 quantity=0.001, average_entry=30000.0)]
            return []

    fake = StuckBroker()
    monkeypatch.setattr("app.brokers.alpaca.AlpacaPaperBroker", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr("app.runtime.create_broker", lambda config, env: fake)
    config, env = _config(), _env()
    code, report = await direct_mod.run_direct(config, env, quantity=0.001, timeout=10.0)
    assert code == 5 and report["outcome"] == "flatten_failed"
    assert report["flatten"]["final_broker_zero"] is False


# --- 7. clamp ------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_direct_quantity_clamped_to_ceiling(fake_broker):
    config, env = _config(), _env()
    config.live_acceptance.max_order_quantity = 0.0005
    code, report = await direct_mod.run_direct(config, env, quantity=0.005, timeout=10.0)
    assert code == 0
    assert report["clamped_quantity"] == 0.0005
    assert report["order"]["quantity"] == 0.0005


# --- 8. pipeline timeout --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_no_signal_timeout(monkeypatch, fake_broker):
    from app.market_data.mock import MockMarketDataProvider

    def _mock_provider(config, env, bus=None):
        return MockMarketDataProvider(config.trading.symbol)

    monkeypatch.setattr("app.runtime.create_provider", _mock_provider)
    config, env = _config(), _env()
    code, report = await pipeline_mod.run_pipeline(config, env, quantity=0.001, timeout=2.0)
    assert code == 7 and report["outcome"] == "no_signal_within_timeout"
    assert not [c for c in fake_broker.calls if c.startswith("submit:")]
    assert config.execution.enabled is False


# --- 9. cleanup on every path ----------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cleanup_restores_gates_on_exception(monkeypatch, fake_broker):
    async def _boom(*a, **k):
        raise RuntimeError("injected flatten failure")

    monkeypatch.setattr("app.runner.engine.TradingEngine.flatten", _boom)
    config, env = _config(), _env()
    code, report = await direct_mod.run_direct(config, env, quantity=0.001, timeout=10.0)
    assert code == 6 and report["outcome"] == "unexpected_error"
    assert "injected flatten failure" in (report["error"] or "")
    assert config.execution.enabled is False
    assert config.paper_safety.enabled is False
    assert fake_broker._connected is False


# --- 12. live endpoint refused --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_live_endpoint_refused(fake_broker):
    config = _config()
    env = _env(apca_api_base_url="https://api.alpaca.markets")
    engine = _engine(config, env)
    checks = await run_preflight(config, env, engine)
    assert next(c for c in checks if c.name == "not_live_endpoint").ok is False
    assert not [c for c in fake_broker.calls if c.startswith("submit:")]
