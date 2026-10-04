"""Signal generation: enriches a strategy signal with stop/target levels.

Stop distance and reward ratio come from the RISK configuration so that risk
parameters live in exactly one place.
"""
from __future__ import annotations

from app.config.models import RiskConfig
from app.domain.enums import Direction
from app.domain.signals import StrategySignal
from app.strategies.base import Strategy, StrategyContext


class SignalGenerator:
    def __init__(self, strategy: Strategy, risk_config: RiskConfig) -> None:
        self.strategy = strategy
        self.risk_config = risk_config

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        signal = self.strategy.evaluate(context)
        if signal is None:
            return None
        self._apply_risk_levels(signal, context)
        return signal

    def _apply_risk_levels(self, signal: StrategySignal, context: StrategyContext) -> None:
        atr = context.features.atr
        if atr is None or atr <= 0:
            return
        if self.risk_config.stop_loss.enabled:
            distance = atr * self.risk_config.stop_loss.atr_multiplier
            if signal.direction is Direction.LONG:
                signal.stop_reference = signal.entry_reference - distance
            elif signal.direction is Direction.SHORT:
                signal.stop_reference = signal.entry_reference + distance
        if signal.stop_reference is not None and self.risk_config.take_profit.enabled:
            risk = abs(signal.entry_reference - signal.stop_reference)
            reward = risk * self.risk_config.take_profit.risk_reward_ratio
            if signal.direction is Direction.LONG:
                signal.take_profit_reference = signal.entry_reference + reward
            elif signal.direction is Direction.SHORT:
                signal.take_profit_reference = signal.entry_reference - reward
