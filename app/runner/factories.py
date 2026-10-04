"""Factories that build providers, brokers, and storage from configuration.

This keeps concrete integrations (Alpaca, Ollama, SQLite/Postgres) out of the
engine and makes swapping implementations a configuration change.
"""
from __future__ import annotations

from app.brokers.base import BrokerAdapter
from app.brokers.mock import MockBroker
from app.config.loader import PROJECT_ROOT
from app.config.models import AppConfig
from app.config.settings import EnvSettings
from app.core.errors import ConfigError
from app.market_data.base import MarketDataProvider
from app.market_data.mock import MockMarketDataProvider
from app.storage.db import Database
from app.storage.repository import Repository


def create_provider(config: AppConfig, env: EnvSettings, bus=None) -> MarketDataProvider:
    provider = config.market_data.provider
    if provider == "mock":
        # 1 tick = a fixed span of market time (config-driven) so candle
        # timeframes roll quickly enough to warm up the regime classifier.
        mock = config.market_data.mock
        return MockMarketDataProvider(
            config.trading.symbol,
            spread_percent=config.risk.maximum_spread_percent,
            tick_seconds=mock.tick_seconds,
            seed=mock.seed,
        )
    if provider == "alpaca":
        if not env.has_alpaca_credentials():
            raise ConfigError("Alpaca market-data credentials missing (ALPACA_API_KEY / ALPACA_API_SECRET)")
        from app.market_data.alpaca_provider import AlpacaMarketDataProvider

        # The single Alpaca market-data adapter (Phase B). Credentials come from
        # the environment only and are never logged or embedded in events.
        return AlpacaMarketDataProvider(
            env.alpaca_api_key,
            env.alpaca_api_secret,
            feed=config.market_data.feed,
            reconnect=config.market_data.reconnect,
            bus=bus,
        )
    raise ConfigError(f"unsupported market data provider: {provider}")


def create_broker(config: AppConfig, env: EnvSettings) -> BrokerAdapter:
    broker = config.trading.broker
    if broker == "mock":
        return MockBroker(
            starting_equity=config.backtesting.initial_capital,
            fee_percent=0.0,
            slippage_percent=config.backtesting.slippage_percent,
        )
    if broker == "alpaca":
        if not env.has_alpaca_credentials():
            raise ConfigError("Alpaca paper credentials missing (ALPACA_API_KEY / ALPACA_API_SECRET)")
        from app.brokers.alpaca import AlpacaPaperBroker

        return AlpacaPaperBroker(
            env.alpaca_api_key, env.alpaca_api_secret, feed=config.market_data.feed
        )
    raise ConfigError(f"unsupported broker: {broker}")


def create_storage(config: AppConfig, env: EnvSettings) -> tuple[Database | None, Repository]:
    """Build the database and repository (never raises for optional storage)."""
    if not config.storage.enabled:
        return None, Repository(None)
    database = Database(config.storage, project_root=PROJECT_ROOT)
    return database, Repository(database.session_factory, database=database)


__all__ = ["create_provider", "create_broker", "create_storage"]
