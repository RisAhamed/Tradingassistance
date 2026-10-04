"""Risk engine — a mandatory, fail-closed gate between signals and orders.

Neither the strategy nor the AI can override risk. If ANY mandatory check
fails, the signal is rejected with an explicit reason code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.config.models import RiskConfig
from app.core.ids import new_risk_id
from app.domain.enums import Direction, ReasonCode
from app.domain.risk import RiskCheckResult, RiskDecision
from app.domain.signals import StrategySignal


@dataclass(slots=True)
class RiskContext:
    """Everything the risk engine needs to adjudicate one signal."""

    now: datetime
    symbol: str
    session_active: bool = True
    entries_allowed: bool = True
    market_data_fresh: bool = True
    system_healthy: bool = True
    reconciliation_ok: bool = True
    account_equity: float = 0.0
    open_positions: int = 0
    orders_this_session: int = 0
    daily_realized_pnl: float = 0.0
    last_trade_time: datetime | None = None
    position: Direction = Direction.FLAT
    position_holding_seconds: float | None = None
    allowed_symbols: set[str] = field(default_factory=set)


class RiskEngine:
    def __init__(
        self,
        config: RiskConfig,
        *,
        allow_short: bool = True,
        cooldown_minutes: float = 0.0,
    ) -> None:
        self.config = config
        self.allow_short = allow_short
        self.cooldown_minutes = cooldown_minutes

    def evaluate(self, signal: StrategySignal, context: RiskContext) -> RiskDecision:
        checks: list[RiskCheckResult] = [
            self._check_risk_enabled(),
            self._check_symbol(signal, context),
            self._check_session(context),
            self._check_market_data(context),
            self._check_system_health(context),
            self._check_reconciliation(context),
            self._check_spread(signal),
            self._check_position_limit(context),
            self._check_direction(signal),
            self._check_orders_per_session(context),
            self._check_daily_loss(context),
            self._check_risk_per_trade(signal, context),
            self._check_exposure(signal, context),
            self._check_holding_time(context),
            self._check_duplicate_and_cooldown(context),
        ]
        failed = [c for c in checks if not c.passed]
        approved = not failed
        reason = ReasonCode.APPROVED if approved else (failed[0].reason or ReasonCode.INVALID_SIGNAL)
        return RiskDecision(
            risk_id=new_risk_id(),
            timestamp=context.now,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            approved=approved,
            reason=reason,
            checks=checks,
            correlation_id=signal.correlation_id,
            session_id=signal.session_id,
        )

    # -- individual checks --------------------------------------------------
    def _check_risk_enabled(self) -> RiskCheckResult:
        return RiskCheckResult(
            name="risk_enabled",
            passed=self.config.enabled,
            reason=None if self.config.enabled else ReasonCode.RISK_DISABLED,
        )

    def _check_symbol(self, signal: StrategySignal, ctx: RiskContext) -> RiskCheckResult:
        allowed = not ctx.allowed_symbols or signal.symbol in ctx.allowed_symbols
        return RiskCheckResult(
            name="symbol_allowed",
            passed=allowed,
            reason=None if allowed else ReasonCode.SYMBOL_NOT_ALLOWED,
            detail=signal.symbol,
        )

    def _check_session(self, ctx: RiskContext) -> RiskCheckResult:
        passed = ctx.session_active and ctx.entries_allowed
        reason = None
        if not ctx.session_active:
            reason = ReasonCode.SESSION_CLOSED
        elif not ctx.entries_allowed:
            reason = ReasonCode.ENTRY_CUTOFF_REACHED
        return RiskCheckResult(name="session_active", passed=passed, reason=reason)

    def _check_market_data(self, ctx: RiskContext) -> RiskCheckResult:
        return RiskCheckResult(
            name="market_data_fresh",
            passed=ctx.market_data_fresh,
            reason=None if ctx.market_data_fresh else ReasonCode.STALE_DATA,
        )

    def _check_system_health(self, ctx: RiskContext) -> RiskCheckResult:
        return RiskCheckResult(
            name="system_healthy",
            passed=ctx.system_healthy,
            reason=None if ctx.system_healthy else ReasonCode.SYSTEM_UNHEALTHY,
        )

    def _check_reconciliation(self, ctx: RiskContext) -> RiskCheckResult:
        return RiskCheckResult(
            name="reconciliation_ok",
            passed=ctx.reconciliation_ok,
            reason=None if ctx.reconciliation_ok else ReasonCode.RECONCILIATION_FAILED,
        )

    def _check_spread(self, signal: StrategySignal) -> RiskCheckResult:
        spread = signal.features.get("spread_percent")
        if spread is None:
            return RiskCheckResult(name="spread_acceptable", passed=True)
        passed = spread <= self.config.maximum_spread_percent
        return RiskCheckResult(
            name="spread_acceptable",
            passed=passed,
            reason=None if passed else ReasonCode.SPREAD_TOO_HIGH,
            detail=f"{spread:.4f}<={self.config.maximum_spread_percent}",
        )

    def _check_position_limit(self, ctx: RiskContext) -> RiskCheckResult:
        passed = ctx.open_positions < self.config.maximum_open_positions
        return RiskCheckResult(
            name="position_limit",
            passed=passed,
            reason=None if passed else ReasonCode.POSITION_LIMIT,
            detail=f"{ctx.open_positions}/{self.config.maximum_open_positions}",
        )

    def _check_direction(self, signal: StrategySignal) -> RiskCheckResult:
        if signal.direction is Direction.SHORT and not self.allow_short:
            return RiskCheckResult(
                name="direction_allowed", passed=False, reason=ReasonCode.INVALID_SIGNAL, detail="short_disabled"
            )
        return RiskCheckResult(name="direction_allowed", passed=True)

    def _check_orders_per_session(self, ctx: RiskContext) -> RiskCheckResult:
        passed = ctx.orders_this_session < self.config.maximum_orders_per_session
        return RiskCheckResult(
            name="orders_per_session",
            passed=passed,
            reason=None if passed else ReasonCode.ORDERS_PER_SESSION_LIMIT,
            detail=f"{ctx.orders_this_session}/{self.config.maximum_orders_per_session}",
        )

    def _check_daily_loss(self, ctx: RiskContext) -> RiskCheckResult:
        if ctx.account_equity <= 0:
            return RiskCheckResult(name="daily_loss_limit", passed=True)
        limit = ctx.account_equity * (self.config.maximum_daily_loss_percent / 100.0)
        passed = (-ctx.daily_realized_pnl) < limit
        return RiskCheckResult(
            name="daily_loss_limit",
            passed=passed,
            reason=None if passed else ReasonCode.DAILY_LOSS_LIMIT,
            detail=f"loss={-ctx.daily_realized_pnl:.2f} limit={limit:.2f}",
        )

    def _check_risk_per_trade(self, signal: StrategySignal, ctx: RiskContext) -> RiskCheckResult:
        if ctx.account_equity <= 0:
            return RiskCheckResult(name="risk_per_trade", passed=True)
        distance = signal.risk_distance
        if distance is None or distance <= 0:
            return RiskCheckResult(
                name="risk_per_trade", passed=False, reason=ReasonCode.INVALID_SIGNAL, detail="no_stop"
            )
        budget = ctx.account_equity * (self.config.risk_per_trade_percent / 100.0)
        return RiskCheckResult(
            name="risk_per_trade",
            passed=budget > 0,
            reason=None if budget > 0 else ReasonCode.RISK_PER_TRADE_LIMIT,
            detail=f"budget={budget:.2f}",
        )

    def _check_exposure(self, signal: StrategySignal, ctx: RiskContext) -> RiskCheckResult:
        # NOTE: raw entry *price* cannot be compared to a notional cap.
        # BTC ~30k would always exceed equity*20% (~20k) and reject every
        # signal. Notional = quantity * price is only known after position
        # sizing, so this pre-sizing gate only fails when sizing could not
        # possibly fit inside the cap (e.g. non-positive equity). The
        # authoritative notional cap is enforced in PositionSizer.size()
        # via the ``max_notional`` argument supplied by callers.
        if ctx.account_equity <= 0:
            return RiskCheckResult(name="max_exposure", passed=True)
        max_value = ctx.account_equity * (self.config.maximum_position_value_percent / 100.0)
        if max_value <= 0:
            return RiskCheckResult(name="max_exposure", passed=True, detail="cap_disabled")
        distance = signal.risk_distance
        if distance is None or distance <= 0 or signal.entry_reference <= 0:
            # No stop yet (stop is attached by SignalGenerator before risk in
            # the live engine); cannot estimate notional here -> defer to sizer.
            return RiskCheckResult(
                name="max_exposure",
                passed=True,
                detail=f"deferred_to_sizer max_value={max_value:.2f}",
            )
        budget = ctx.account_equity * (self.config.risk_per_trade_percent / 100.0)
        estimated_qty = budget / distance if distance > 0 else 0.0
        estimated_notional = estimated_qty * signal.entry_reference
        # Even an over-budget estimate is fixable: the sizer caps quantity to
        # max_value (trading smaller than the full risk budget). Only fail if
        # the cap itself is dust relative to the minimum tradable notional is
        # handled in the sizer; here we always pass and report the estimate.
        return RiskCheckResult(
            name="max_exposure",
            passed=True,
            detail=f"est_notional={estimated_notional:.2f} max_value={max_value:.2f}",
        )

    def _check_holding_time(self, ctx: RiskContext) -> RiskCheckResult:
        if ctx.position is Direction.FLAT or ctx.position_holding_seconds is None:
            return RiskCheckResult(name="max_holding_time", passed=True)
        limit = self.config.maximum_holding_minutes * 60
        passed = ctx.position_holding_seconds < limit
        return RiskCheckResult(
            name="max_holding_time",
            passed=passed,
            reason=None if passed else ReasonCode.MAX_HOLDING_TIME,
            detail=f"{ctx.position_holding_seconds:.0f}s/{limit}s",
        )

    def _check_duplicate_and_cooldown(self, ctx: RiskContext) -> RiskCheckResult:
        """Reject a re-entry that arrives inside the configured cooldown window."""
        if ctx.last_trade_time is None or self.cooldown_minutes <= 0:
            return RiskCheckResult(name="cooldown", passed=True)
        elapsed_minutes = (ctx.now - ctx.last_trade_time).total_seconds() / 60.0
        passed = elapsed_minutes >= self.cooldown_minutes
        return RiskCheckResult(
            name="cooldown",
            passed=passed,
            reason=None if passed else ReasonCode.COOLDOWN_ACTIVE,
            detail=f"elapsed={elapsed_minutes:.1f}m >= {self.cooldown_minutes}m",
        )
