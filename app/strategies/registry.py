"""Strategy registry: selects the configured strategy implementation.

Both the live runner and the backtesting engine resolve strategies through
:func:`build_strategy` so selection lives in exactly one place and follows
``config.strategy.name``. Adding a strategy means registering its builder
here — never editing the call sites.
"""
from __future__ import annotations

from collections.abc import Callable

from app.config.models import StrategyConfig
from app.strategies.base import Strategy

Builder = Callable[[StrategyConfig], Strategy]

_BUILDERS: dict[str, Builder] = {}


def register(name: str):
    """Decorator registering a strategy builder under ``name``."""

    def wrap(builder: Builder) -> Builder:
        _BUILDERS[name] = builder
        return builder

    return wrap


def known_strategies() -> list[str]:
    return sorted(_BUILDERS)


def build_strategy(config: StrategyConfig) -> Strategy:
    """Instantiate the configured strategy or fail clearly."""
    try:
        builder = _BUILDERS[config.name]
    except KeyError:
        raise ValueError(
            f"unknown strategy '{config.name}' (known: {known_strategies()})"
        ) from None
    strategy = builder(config)
    strategy.name = config.name
    return strategy


from app.strategies.breakout_momentum import (  # noqa: E402,E501
    BreakoutMomentumStrategy,
)
from app.strategies.vwap_reversion import (  # noqa: E402,E501
    VwapReversionStrategy,
)
from app.strategies.session_vwap_reversion import (  # noqa: E402,E501
    SessionVwapReversionStrategy,
)

register("breakout_momentum")(BreakoutMomentumStrategy)
register("vwap_reversion")(VwapReversionStrategy)
register("session_vwap_reversion")(SessionVwapReversionStrategy)
