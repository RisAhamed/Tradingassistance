"""Deterministic, risk-based position sizing.

Conceptually: ``risk_amount / stop_distance``, then normalised to the broker's
quantity constraints. Every intermediate value is logged by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config.models import PositionSizingConfig
from app.domain.signals import StrategySignal


@dataclass(slots=True)
class SizingResult:
    equity: float
    risk_budget: float
    stop_distance: float
    raw_quantity: float
    normalized_quantity: float
    final_quantity: float
    rejected_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.rejected_reason is None and self.final_quantity > 0


class PositionSizer:
    def __init__(self, config: PositionSizingConfig) -> None:
        self.config = config

    def size(
        self,
        signal: StrategySignal,
        equity: float,
        *,
        reference_price: float | None = None,
        max_notional: float | None = None,
    ) -> SizingResult:
        stop_distance = signal.risk_distance
        if stop_distance is None or stop_distance <= 0:
            return SizingResult(equity, 0.0, 0.0, 0.0, 0.0, 0.0, "invalid_stop_distance")
        if equity <= 0:
            return SizingResult(equity, 0.0, stop_distance, 0.0, 0.0, 0.0, "non_positive_equity")

        risk_budget = equity * (self.config.risk_per_trade_percent / 100.0)
        raw_quantity = risk_budget / stop_distance
        normalized = self._normalize(raw_quantity)
        if normalized <= 0:
            return SizingResult(
                equity, risk_budget, stop_distance, raw_quantity, normalized, 0.0, "below_minimum_quantity"
            )
        # Authoritative exposure cap: quantity * price must stay within the
        # configured maximum position value. Applied here (post-sizing) where
        # quantity is known — the risk pre-check cannot do this correctly.
        price = reference_price if reference_price and reference_price > 0 else signal.entry_reference
        if max_notional is not None and max_notional > 0 and price > 0:
            cap_qty = self._normalize(max_notional / price)
            if cap_qty <= 0:
                return SizingResult(
                    equity,
                    risk_budget,
                    stop_distance,
                    raw_quantity,
                    normalized,
                    0.0,
                    "max_exposure",
                )
            if normalized * price > max_notional:
                normalized = cap_qty
                if normalized <= 0:
                    return SizingResult(
                        equity,
                        risk_budget,
                        stop_distance,
                        raw_quantity,
                        cap_qty,
                        0.0,
                        "max_exposure",
                    )
        return SizingResult(equity, risk_budget, stop_distance, raw_quantity, normalized, normalized)

    def _normalize(self, quantity: float) -> float:
        precision = max(0, self.config.quantity_precision)
        step = 10 ** (-precision)
        floored = int(quantity / step) * step
        floored = round(floored, precision)
        if floored < self.config.minimum_quantity:
            return 0.0
        if self.config.maximum_quantity and floored > self.config.maximum_quantity:
            floored = self.config.maximum_quantity
        return round(floored, precision)
