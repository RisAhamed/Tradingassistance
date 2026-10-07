"""D.5.6 — vendor-leak regression: capability flags, registries, symbol resolver."""
import re

import pytest

from app.config.models import AppConfig
from app.config.loader import get_env, load_config


def test_alpaca_supports_in_kind_fees_flag():
    from app.brokers.alpaca import AlpacaPaperBroker

    assert AlpacaPaperBroker.supports_in_kind_fees is True
    assert AlpacaPaperBroker.requires_authoritative_qty_on_exit is True


def test_mock_capability_flags_default_false():
    from app.brokers.mock import MockBroker

    m = MockBroker()
    assert m.supports_in_kind_fees is False
    assert m.requires_authoritative_qty_on_exit is False


def test_resolve_symbol_alpaca_strips_slash():
    from app.brokers.alpaca import AlpacaPaperBroker

    # Do not instantiate the real Alpaca client; just use the adapter's pure
    # resolver method on a dummy instance shape.
    class _Dummy(AlpacaPaperBroker):
        def __init__(self):
            pass

    assert _Dummy().resolve_symbol("BTC/USD") == "BTCUSD"


def test_resolve_symbol_mock_unchanged():
    from app.brokers.mock import MockBroker

    m = MockBroker()
    assert m.resolve_symbol("BTC/USD") == "BTC/USD"


def test_unknown_broker_fails_config():
    env = get_env()
    config = load_config(env=env)
    config.trading.broker = "bogus"
    with pytest.raises(ValueError, match="unknown broker"):
        # Force revalidation through a fresh model copy
        from app.config.models import AppConfig as _AC

        _AC.model_validate(config.model_dump(mode="json"))


def test_unknown_provider_fails_config():
    from app.config.models import MarketDataConfig

    with pytest.raises(ValueError, match="unknown market-data provider"):
        MarketDataConfig.model_validate({"provider": "bogus"})


def test_symbol_suffixes_from_config():
    from app.config.models import MarketDataConfig

    cfg = MarketDataConfig.model_validate({"symbol": {"quote_suffixes": ["USDT", "USD"]}})
    assert cfg.symbol.quote_suffixes == ["USDT", "USD"]


def test_symbol_suffixes_empty_rejected():
    from app.config.models import MarketDataConfig

    with pytest.raises(ValueError):
        MarketDataConfig.model_validate({"symbol": {"quote_suffixes": []}})


def test_registry_add_and_remove():
    from app.brokers.registry import _BUILDERS, known_brokers, register_broker

    def fake(cfg, env):
        return None

    register_broker("fake", fake)
    assert "fake" in known_brokers()
    del _BUILDERS["fake"]
    assert "fake" not in known_brokers()
    with pytest.raises(ValueError, match="unknown broker"):
        from app.config.models import AppConfig as _AC

        _AC(trading={"mode": "paper", "symbol": "BTC/USD", "broker": "fake"})
