"""PHASE B #1: the execution safety gate.

Invariant: with ``execution.enabled=false`` the full pipeline (market data ->
features -> regime -> strategy -> signal -> risk -> sizing) still runs, but NOT a
single order may be created, submitted, or reach any broker.
"""
import asyncio

from fastapi.testclient import TestClient

from app.config.loader import get_env, load_config
from app.config.models import ExecutionConfig
from app.core.clock import utcnow
from app.domain.enums import Direction, OrderType, Side
from app.domain.market import MarketSnapshot
from app.domain.orders import OrderIntent
from app.events.types import EventType
from app.main import create_app
from app.runtime import build_runtime
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
    return config


def _engine(*, execution_enabled: bool):
    return build_runtime(_config(execution_enabled=execution_enabled), get_env()).engine


def test_execution_disabled_does_not_construct_alpaca_trading_client(monkeypatch):
    config = load_config(env=get_env())
    config.execution.enabled = False
    config.trading.broker = "alpaca"

    def _unexpected(*args, **kwargs):
        raise AssertionError("Alpaca trading client must not be constructed")

    monkeypatch.setattr("app.brokers.alpaca.AlpacaPaperBroker.__init__", _unexpected)
    runtime = build_runtime(config, get_env())
    assert runtime.engine.execution_enabled is False
    assert runtime.engine.broker.name == "mock"


async def _pump(engine, limit: int = 700) -> None:
    provider = engine.provider
    ticks = 0
    while not provider.exhausted and ticks < limit:
        await provider.pump_one()
        ticks += 1
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


def test_execution_is_disabled_by_default():
    """Fail-closed: the shipped configuration must not allow orders."""
    config = load_config(env=get_env())
    assert config.execution.enabled is False
    assert ExecutionConfig().enabled is False  # also the model default


async def test_execution_disabled_never_creates_an_order():
    engine = _engine(execution_enabled=False)
    assert engine.execution_enabled is False
    # Give the engine an equity baseline so sizing succeeds and we reach the gate.
    engine._account_equity = 100000.0
    engine.pnl.set_equity(100000.0)
    engine.state.latest_snapshot = MarketSnapshot(symbol=SYMBOL, timestamp=utcnow(), last=30000.0)
    events: list = []
    engine.bus.subscribe_all(events.append)

    await engine._submit_entry(make_signal())

    assert engine.oms.all_orders() == []
    blocked = [e for e in events if e.type is EventType.EXECUTION_BLOCKED]
    assert blocked and blocked[0].payload["reason"] == "execution_disabled"


async def test_execution_disabled_refuses_submission_even_with_a_real_order():
    """Defense in depth: the broker funnel itself is gated."""
    engine = _engine(execution_enabled=False)
    engine.broker.set_price(SYMBOL, 30000.0)
    order = engine.oms.create(
        OrderIntent(
            order_id="ord_phaseb",
            client_order_id="cli_phaseb",
            timestamp=utcnow(),
            symbol=SYMBOL,
            side=Side.BUY,
            direction=Direction.LONG,
            quantity=1.0,
            order_type=OrderType.MARKET,
        )
    )
    assert await engine._submit(order) is False
    assert order.status.value == "new"  # never advanced to submitted


async def test_execution_disabled_never_connects_the_broker():
    engine = _engine(execution_enabled=False)
    await engine.start()
    try:
        assert engine.broker.health().connected is False
    finally:
        await engine.stop()


async def test_full_pipeline_runs_with_execution_disabled_but_orders_none():
    """The Phase B end-to-end promise: data + signals run, zero orders."""
    engine = _engine(execution_enabled=False)
    await engine.start()
    try:
        await _pump(engine)
        assert len(engine.store.candles(engine.symbol, "5m")) > 0
        assert engine.state.regime is not None  # regime warm-up still happens
        assert engine.oms.all_orders() == []
        assert engine.position_manager.is_flat
        assert engine.payload()["execution"]["status"] == "DISABLED"
    finally:
        await engine.stop()


async def test_execution_enabled_allows_orders_again():
    """Sanity check that the gate is what blocks, not a broken pipeline."""
    engine = _engine(execution_enabled=True)
    await engine.start()
    try:
        await _pump(engine)
        assert engine.oms.all_orders(), "expected orders when execution is enabled"
        assert engine.payload()["execution"]["status"] == "ENABLED"
    finally:
        await engine.stop()


def test_execution_disabled_is_visible_on_the_dashboard_and_api():
    # `with` runs the lifespan so the runtime (API-backed) is actually built.
    with TestClient(create_app(config=_config(execution_enabled=False), autostart=False)) as client:
        payload = client.get("/api/execution").json()
        assert payload["execution"]["enabled"] is False
        assert payload["execution"]["order_creation"] == "DISABLED"
        assert payload["execution"]["risk_evaluation"] == "ENABLED"
        html = client.get("/dashboard").text
        assert "EXECUTION: DISABLED" in html
        assert 'id="execution"' in html


async def test_reconciliation_is_a_noop_while_execution_is_disabled():
    """Orders are impossible, so there is nothing to reconcile — and we must not
    contact the broker to find that out."""
    engine = _engine(execution_enabled=False)
    assert await engine.reconcile() is True
    assert engine.state.reconciliation["mode"] == "execution_disabled"
    assert engine._need_reconciliation is False


async def test_stale_market_data_blocks_entry_even_with_execution_enabled():
    engine = _engine(execution_enabled=True)
    await engine.start()
    try:
        engine.session.start(utcnow())
        # No market data was ever pushed -> data age is None -> stale.
        assert engine._build_risk_context(utcnow()).market_data_fresh is False
        await engine._handle_signal(make_signal())
        assert engine.oms.all_orders() == []
    finally:
        await engine.stop()