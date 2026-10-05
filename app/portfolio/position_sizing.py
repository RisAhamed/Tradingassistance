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
    def final_risk_amount(self) -> float:
        """Risk represented by the quantity actually allowed to execute."""
        return self.final_quantity * self.stop_distance if self.ok else 0.0

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
        # POSITION-SIZING SAFETY (Phase A #5): reject every malformed input
        # before any arithmetic. No negative / zero / NaN / inf quantity may
        # reach the OMS — fail closed with an explicit rejected_reason.
        stop_distance = signal.risk_distance
        if stop_distance is None or not _finite_positive(stop_distance):
            return SizingResult(equity, 0.0, 0.0, 0.0, 0.0, 0.0, "invalid_stop_distance")
        if equity is None or not _finite_positive(equity):
            return SizingResult(equity if equity == equity else 0.0, 0.0, stop_distance, 0.0, 0.0, 0.0, "non_positive_equity")
        if not _finite_nonnegative(self.config.risk_per_trade_percent):
            return SizingResult(equity, 0.0, stop_distance, 0.0, 0.0, 0.0, "invalid_risk_percent")
        if signal.entry_reference is not None and not _finite_nonnegative(signal.entry_reference):
            return SizingResult(equity, 0.0, stop_distance, 0.0, 0.0, 0.0, "invalid_entry_reference")

        risk_budget = equity * (self.config.risk_per_trade_percent / 100.0)
        if not _finite_nonnegative(risk_budget) or risk_budget <= 0:
            return SizingResult(equity, 0.0, stop_distance, 0.0, 0.0, 0.0, "invalid_risk_budget")
        raw_quantity = risk_budget / stop_distance
        if not _finite_positive(raw_quantity):
            return SizingResult(equity, risk_budget, stop_distance, 0.0, 0.0, 0.0, "invalid_raw_quantity")
        if reference_price is not None and not _finite_nonnegative(reference_price):
            return SizingResult(equity, risk_budget, stop_distance, raw_quantity, 0.0, 0.0, "invalid_reference_price")
        if max_notional is not None and not _finite_nonnegative(max_notional):
            return SizingResult(equity, risk_budget, stop_distance, raw_quantity, 0.0, 0.0, "invalid_max_notional")
        normalized = self._normalize(raw_quantity)
        if normalized <= 0:
            return SizingResult(
                equity, risk_budget, stop_distance, raw_quantity, normalized, 0.0, "below_minimum_quantity"
            )
        # Authoritative exposure cap: quantity * price must stay within the
        # configured maximum position value. Applied here (post-sizing) where
        # quantity is known — the risk pre-check cannot do this correctly.
        price = reference_price if reference_price and reference_price > 0 else signal.entry_reference
        if price is not None and not _finite_positive(price):
            return SizingResult(equity, risk_budget, stop_distance, raw_quantity, normalized, 0.0, "invalid_reference_price")
        if max_notional is not None and max_notional > 0 and price is not None and price > 0:
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
        if not _finite_positive(normalized):
            return SizingResult(equity, risk_budget, stop_distance, raw_quantity, 0.0, 0.0, "invalid_normalized_quantity")
        return SizingResult(equity, risk_budget, stop_distance, raw_quantity, normalized, normalized)

    def _normalize(self, quantity: float) -> float:
        if not _finite_nonnegative(quantity):
            return 0.0
        precision = max(0, self.config.quantity_precision)
        step = 10 ** (-precision)
        floored = int(quantity / step) * step
        floored = round(floored, precision)
        if floored < self.config.minimum_quantity:
            return 0.0
        if self.config.maximum_quantity and floored > self.config.maximum_quantity:
            floored = self.config.maximum_quantity
        return round(floored, precision)


def _finite_positive(value: object) -> bool:
    import math

    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def _finite_nonnegative(value: object) -> bool:
    import math

    return isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
