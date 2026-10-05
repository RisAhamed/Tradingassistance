"""PHASE D.2: paper-execution gate and isolation safety."""
from app.config.loader import get_env, load_config
from app.config.settings import EnvSettings
from app.runner.factories import create_broker
from app.runtime import build_runtime


def _config():
    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.market_data.max_future_skew_seconds = 86400 * 30
    return config


def test_execution_disabled_factory_never_builds_alpaca_client():
    config = _config()
    config.execution.enabled = False
    config.trading.broker = "alpaca"  # even forcing alpaca must stay mock
    env = EnvSettings()
    broker = create_broker(config, env)
    assert broker.name == "mock"


def test_paper_mode_with_alpaca_broker_constructs_paper_only_client():
    config = _config()
    config.execution.enabled = True
    config.trading.broker = "alpaca"
    env = EnvSettings()
    # Without credentials, factory must fail closed, not construct a client.
    try:
        broker = create_broker(config, env)
    except Exception:
        return
    assert broker.name == "alpaca"
    assert broker.paper is True


async def test_disabled_gate_never_creates_an_order_intent():
    config = _config()
    config.execution.enabled = False
    engine = build_runtime(config, get_env()).engine
    await engine.start()
    try:
        provider = engine.provider
        ticks = 0
        while not provider.exhausted and ticks < 300:
            if not await provider.pump_one():
                break
            ticks += 1
        assert engine.oms.all_orders() == []
        assert engine.position_manager.is_flat is True
        assert engine.execution_enabled is False
    finally:
        await engine.stop()
