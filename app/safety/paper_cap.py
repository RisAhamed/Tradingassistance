"""Independent paper-experiment safety cap (GAP-1).

A rejection-only gate enforced at the single order-submission funnel,
downstream of strategy, risk engine, and position sizer. Semantics:

- ENTRY (exposure-increasing) orders: quantity, open-position, order-count,
  and notional caps all apply. Breach => REJECT (never clip, never scale).
- EXIT (exposure-reducing) orders: bypass the caps entirely so a flatten can
  never be blocked by experimental limits (flatten-safe direction).
- Disabled cap: inactive — order flow is exactly unchanged — but the
  configured values stay visible (describe()) and hashed (part of AppConfig).
- Invalid cap while enabled (missing/non-finite/non-positive numbers,
  non-paper mode): fail closed — every gated order is rejected.
- The cap holds no reference to any strategy and exposes no mutator usable
  by strategy code; strategies receive only their StrategyConfig subsection.

State (entries_this_session, seen order ids) lives in the instance, which
the runner owns; ``reset_session()`` is called on session start.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.config.models import PaperSafetyConfig


@dataclass(slots=True)
class CapDecision:
    allowed: bool
    reason: str
    active: bool
    detail: dict = field(default_factory=dict)


class PaperSafetyCap:
    """Stateful paper-cap gate. Pure evaluation + explicit bookkeeping."""

    def __init__(self, config: PaperSafetyConfig, *, trading_mode: str) -> None:
        self.config = config
        self.trading_mode = trading_mode
        self.entries_this_session = 0
        self._seen_order_ids: set[str] = set()

    # -- audit -----------------------------------------------------------------
    def describe(self) -> dict:
        """Auditable view of the cap (startup logging, run metadata)."""
        cfg = self.config
        return {
            "enabled": bool(cfg.enabled),
            "paper_mode": self.trading_mode == "paper",
            "max_qty_per_order": cfg.max_qty_per_order,
            "max_open_qty": cfg.max_open_qty,
            "max_orders_per_session": cfg.max_orders_per_session,
            "max_notional_exposure": cfg.max_notional_exposure,
            "config_valid": self._config_error() is None,
            "entries_this_session": self.entries_this_session,
        }

    def reset_session(self) -> None:
        self.entries_this_session = 0
        self._seen_order_ids.clear()

    # -- enforcement --------------------------------------------------------------
    def check(
        self,
        *,
        order_id: str,
        quantity: float,
        price: float,
        is_entry: bool,
        open_qty: float = 0.0,
    ) -> CapDecision:
        """Evaluate one order. No mutation (call note_submitted on pass)."""
        if not self.config.enabled:
            return CapDecision(True, "cap_inactive", False)
        if self.trading_mode != "paper":
            return CapDecision(False, "paper_mode_required", True)
        error = self._config_error()
        if error is not None:
            return CapDecision(False, f"invalid_cap:{error}", True)
        if not is_entry:
            # Flatten-safe: exits are never blocked by experimental caps.
            return CapDecision(True, "exit_bypass", True)
        cfg = self.config
        if not (quantity > 0) or not math.isfinite(quantity):
            return CapDecision(False, "invalid_quantity", True)
        if quantity > cfg.max_qty_per_order:
            return CapDecision(False, "qty_per_order_exceeded", True,
                               {"quantity": quantity, "cap": cfg.max_qty_per_order})
        if open_qty + quantity > cfg.max_open_qty:
            return CapDecision(False, "open_qty_exceeded", True,
                               {"open_qty": open_qty, "quantity": quantity, "cap": cfg.max_open_qty})
        if order_id not in self._seen_order_ids and self.entries_this_session >= cfg.max_orders_per_session:
            return CapDecision(False, "orders_per_session_exceeded", True,
                               {"entries": self.entries_this_session, "cap": cfg.max_orders_per_session})
        if not (price > 0) or not math.isfinite(price):
            return CapDecision(False, "invalid_price", True)
        if quantity * price > cfg.max_notional_exposure:
            return CapDecision(False, "notional_exceeded", True,
                               {"notional": quantity * price, "cap": cfg.max_notional_exposure})
        return CapDecision(True, "within_cap", True)

    def note_submitted(self, order_id: str) -> None:
        """Record a passed entry order. Retries reuse the id: no double count."""
        if order_id not in self._seen_order_ids:
            self._seen_order_ids.add(order_id)
            self.entries_this_session += 1

    # -- internals -------------------------------------------------------------------
    def _config_error(self) -> str | None:
        cfg = self.config
        for name in ("max_qty_per_order", "max_open_qty", "max_notional_exposure"):
            value = getattr(cfg, name, None)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                return f"{name}_invalid"
        orders = cfg.max_orders_per_session
        if not isinstance(orders, int) or isinstance(orders, bool) or orders < 1:
            return "max_orders_per_session_invalid"
        return None
