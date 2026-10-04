"""Typed application exceptions.

Explicit exception types keep the ``except SpecificException`` rule from the
spec enforceable and avoid broad silent handling.
"""
from __future__ import annotations


class TradingAgentError(Exception):
    """Base class for all application errors."""


class ConfigError(TradingAgentError):
    """Raised when configuration is missing or invalid."""


class PaperOnlyViolation(TradingAgentError):
    """Raised when a live endpoint/credential is detected. Fails safe."""


class MarketDataError(TradingAgentError):
    """Raised for market-data connectivity or validation problems."""


class StaleDataError(MarketDataError):
    """Raised when market data is older than the configured freshness limit."""


class BrokerError(TradingAgentError):
    """Raised for broker connectivity or order problems."""


class OrderRejected(BrokerError):
    """Raised when the broker rejects an order."""


class RiskRejected(TradingAgentError):
    """Raised/returned when the risk engine rejects a signal."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReconciliationError(TradingAgentError):
    """Raised when internal state disagrees with the broker."""


class PersistenceError(TradingAgentError):
    """Raised when a required persistence operation fails."""


class SessionError(TradingAgentError):
    """Raised for session lifecycle problems."""


class AIError(TradingAgentError):
    """Raised for AI provider problems."""


class ToolPermissionError(TradingAgentError):
    """Raised when an AI/human tool call is not permitted."""


class UnknownToolError(TradingAgentError):
    """Raised when an AI requests a tool that does not exist."""
