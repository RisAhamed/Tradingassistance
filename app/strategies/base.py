"""Strategy interface.

Strategies are pure evaluators: given a context they return a signal or None.
They must NOT place orders or know about the broker, OMS, or risk engine.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from app.domain.features import FeatureSnapshot
from app.domain.market import MarketSnapshot
from app.domain.regime import RegimeSnapshot
from app.domain.signals import StrategySignal


@dataclass(slots=True)
class StrategyContext:
    symbol: str
    timeframe: str
    features: FeatureSnapshot
    regime: RegimeSnapshot
    now: datetime
    # G.1: regime trend threshold, populated by the caller from
    # config.regime.trending.ema_distance_atr_min. Deliberately NO default —
    # omitting it is a programming error and must fail loudly at
    # construction, never silently fall back to a magic number.
    ema_distance_atr_min: float
    snapshot: MarketSnapshot | None = None
    position_open: bool = False
    correlation_id: str | None = None
    session_id: str | None = None


class Strategy(ABC):
    """Base class for all deterministic strategies."""

    name: str = "base"

    @abstractmethod
    def evaluate(self, context: StrategyContext) -> StrategySignal | None:
        """Return a :class:`StrategySignal` or ``None`` when no signal exists."""
        raise NotImplementedError
