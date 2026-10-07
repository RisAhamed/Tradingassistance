"""Deterministic regime-aware strategy orchestrator.

It never evaluates strategies or places orders; it only chooses WHICH
strategy the SignalGenerator should run for a given regime. Strategy
objects are treated as stateless (they keep only their config), so a
single cached instance per name is safe.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config.models import StrategyConfig, StrategySelectionConfig
from app.domain.enums import Regime
from app.strategies.registry import build_strategy, known_strategies


class StrategyOrchestrator:
    def __init__(self, config: StrategySelectionConfig) -> None:
        self.config = config
        self._cache: dict[str, Any] = {}
        self._last: dict[str, Any] = {
            "mode": config.mode,
            "active_name": None,
            "previous_name": None,
            "last_regime": None,
            "last_reason": None,
            "changed_at": None,
        }

    def active_name(self, regime: Regime) -> str:
        if self.config.mode == "manual":
            return self.config.manual_name
        name = self.config.map.get(regime.value)
        if name is not None:
            return name
        return self.config.fallback_name

    def strategy_for(self, name: str) -> Any:
        if name not in self._cache:
            self._cache[name] = build_strategy(StrategyConfig(name=name))
        return self._cache[name]

    def resolve(self, regime: Regime) -> Any:
        name = self.active_name(regime)
        reason = (
            "manual_mode"
            if self.config.mode == "manual"
            else ("map_hit" if regime.value in self.config.map else "fallback_or_unknown")
        )
        previous = self._last["active_name"]
        self._last.update(
            active_name=name,
            previous_name=previous,
            last_regime=regime.value if regime is not None else None,
            last_reason=reason,
            changed_at=datetime.now(timezone.utc).isoformat() if previous != name else self._last["changed_at"],
        )
        return self.strategy_for(name)

    def describe(self) -> dict:
        return {
            "mode": self.config.mode,
            "active_name": self._last["active_name"],
            "previous_name": self._last["previous_name"],
            "last_regime": self._last["last_regime"],
            "last_reason": self._last["last_reason"],
            "changed_at": self._last["changed_at"],
            "mapping": dict(self.config.map),
            "fallback_name": self.config.fallback_name,
        }

    def snapshot(self) -> dict:
        return dict(self._last)
