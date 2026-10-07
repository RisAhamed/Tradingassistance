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
    from app.market_data.registry import build_provider

    return build_provider(config, env, bus)


def create_broker(config: AppConfig, env: EnvSettings) -> BrokerAdapter:
    # Execution-disabled runtimes are market-data diagnostics only. Never
    # instantiate an Alpaca trading client when the order gate is closed.
    if not config.execution.enabled:
        return MockBroker(
            starting_equity=config.backtesting.initial_capital,
            fee_percent=0.0,
            slippage_percent=config.backtesting.slippage_percent,
        )
    from app.brokers.registry import build_broker

    return build_broker(config, env)


def create_storage(config: AppConfig, env: EnvSettings) -> tuple[Database | None, Repository]:
    """Build the database and repository (never raises for optional storage)."""
    if not config.storage.enabled:
        return None, Repository(None)
    database = Database(config.storage, project_root=PROJECT_ROOT)
    return database, Repository(database.session_factory, database=database)


def create_sentiment_provider(config: AppConfig, env: EnvSettings):
    """Build the configured observation-only sentiment provider.

    Selection goes through the sentiment registry (decorator + dict), never
    an ``if/elif`` chain on the provider name. Returns ``None`` when the
    layer is disabled — the engine then skips polling entirely.
    """
    from app.sentiment import mock as _sentiment_mock  # noqa: F401  (registration side effects)
    from app.sentiment.providers import fear_greed as _sentiment_fg  # noqa: F401
    from app.sentiment.providers import none as _sentiment_none  # noqa: F401
    from app.sentiment.registry import build_sentiment_provider as _build

    sentiment = config.sentiment
    if not sentiment.enabled or sentiment.provider == "none":
        return None
    # Per-provider constructor kwargs (registry still owns selection).
    kwargs: dict = {}
    if sentiment.provider == "mock":
        kwargs["seed"] = sentiment.mock_seed
    elif sentiment.provider == "fear_greed":
        kwargs["http_timeout_seconds"] = sentiment.http_timeout_seconds
    return _build(sentiment.provider, **kwargs)


__all__ = ["create_provider", "create_broker", "create_storage", "create_sentiment_provider"]
