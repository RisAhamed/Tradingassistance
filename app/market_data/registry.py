"""Market-data provider registry (D.5.6): vendor names are resolved through
this registry, not schema Literals or factory if/elif chains."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.config.models import AppConfig
from app.config.settings import EnvSettings

Builder = Callable[[AppConfig, EnvSettings, Any], Any]

_BUILDERS: dict[str, Builder] = {}


def register_provider(name: str, builder: Builder) -> None:
    _BUILDERS[name] = builder


def known_providers() -> list[str]:
    return sorted(_BUILDERS)


def build_provider(config: AppConfig, env: EnvSettings, bus=None) -> Any:
    try:
        builder = _BUILDERS[config.market_data.provider]
    except KeyError:
        raise ValueError(
            f"unknown market-data provider '{config.market_data.provider}' (known: {known_providers()})"
        ) from None
    return builder(config, env, bus)


def _register_defaults() -> None:
    from app.market_data.mock import MockMarketDataProvider

    def _mock(config: AppConfig, env: EnvSettings, bus=None):
        mock = config.market_data.mock
        return MockMarketDataProvider(
            config.trading.symbol,
            spread_percent=config.risk.maximum_spread_percent,
            tick_seconds=mock.tick_seconds,
            seed=mock.seed,
            bars_enabled=config.market_data.bars.enabled,
            bar_timeframe=config.market_data.bars.timeframe,
        )

    register_provider("mock", _mock)

    def _alpaca(config: AppConfig, env: EnvSettings, bus=None):
        if not env.has_alpaca_credentials():
            from app.core.errors import ConfigError

            raise ConfigError("Alpaca market-data credentials missing")
        from app.market_data.alpaca_provider import AlpacaMarketDataProvider

        return AlpacaMarketDataProvider(
            env.alpaca_api_key,
            env.alpaca_api_secret,
            feed=config.market_data.feed,
            reconnect=config.market_data.reconnect,
            bus=bus,
            bars_enabled=config.market_data.bars.enabled,
            bar_timeframe=config.market_data.bars.timeframe,
        )

    register_provider("alpaca", _alpaca)


_register_defaults()
