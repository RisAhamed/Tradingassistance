"""Broker vendor registry (D.5.6). Adding a new broker means registering a
builder here — config string validation and factory dispatch both rely on
this registry so no vendor name is hard-coded in schema or engine paths."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.config.models import AppConfig
from app.config.settings import EnvSettings

Builder = Callable[[AppConfig, EnvSettings], Any]

_BUILDERS: dict[str, Builder] = {}
_CAPABILITIES: dict[str, dict[str, Any]] = {}


def register_broker(name: str, builder: Builder, *, capabilities: dict[str, Any] | None = None) -> None:
    _BUILDERS[name] = builder
    _CAPABILITIES[name] = capabilities or {}


def known_brokers() -> list[str]:
    return sorted(_BUILDERS)


def build_broker(config: AppConfig, env: EnvSettings) -> Any:
    try:
        builder = _BUILDERS[config.trading.broker]
    except KeyError:
        raise ValueError(
            f"unknown broker '{config.trading.broker}' (known: {known_brokers()})"
        ) from None
    return builder(config, env)


# -- vendor import hooks (self-registration) --------------------------------


def _register_defaults() -> None:
    from app.brokers.mock import MockBroker

    def _mock(config: AppConfig, env: EnvSettings) -> MockBroker:
        return MockBroker(
            starting_equity=config.backtesting.initial_capital,
            fee_percent=0.0,
            slippage_percent=config.backtesting.slippage_percent,
        )

    register_broker("mock", _mock, capabilities={})

    def _alpaca(config: AppConfig, env: EnvSettings):
        if not env.has_alpaca_credentials():
            from app.core.errors import ConfigError

            raise ConfigError("Alpaca paper credentials missing")
        from app.brokers.alpaca import AlpacaPaperBroker

        return AlpacaPaperBroker(env.alpaca_api_key, env.alpaca_api_secret, feed=config.market_data.feed)

    register_broker("alpaca", _alpaca, capabilities={"supports_in_kind_fees": True, "requires_authoritative_qty_on_exit": True})


_register_defaults()
