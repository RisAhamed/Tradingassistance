"""TradePlan builder — deterministic, risk-driven candidate trading plan."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config.models import TradePlanConfig
from app.core.clock import utcnow
from app.core.ids import new_order_id
from app.domain.enums import Direction
from app.domain.trade_plan import TradePlan, TradePlanStatus
from app.portfolio.position_sizing import SizingResult


@dataclass(slots=True)
class TradePlanBuildResult:
    plan: TradePlan
    events: list[dict[str, Any]] = field(default_factory=list)


class TradePlanBuilder:
    def __init__(self, config: TradePlanConfig) -> None:
        self.config = config

    def build_from_signal(
        self,
        signal,
        *,
        regime,
        features,
        snapshot: Any = None,
        context_timeframe: str = "15m",
        signal_timeframe: str = "5m",
        execution_timeframe: str = "1m",
        freshness_stale: bool = False,
        correlation_id: str | None = None,
        session_id: str | None = None,
        sizing: SizingResult | None = None,
        risk_percent: float | None = None,
        maximum_notional: float | None = None,
    ) -> TradePlanBuildResult:
        now = utcnow()
        events: list[dict[str, Any]] = []
        entry_conditions = self._entry_conditions(
            signal=signal, regime=regime, features=features,
            snapshot=snapshot, freshness_stale=freshness_stale,
        )
        entry_ready = all(c["met"] for c in entry_conditions)
        stop_price, target_price = self._stop_target(signal, features, use_signal_levels=sizing is not None)
        risk_amount = sizing.final_risk_amount if sizing is not None else 0.0
        position_quantity = sizing.final_quantity if sizing is not None and sizing.ok else None
        sizing_reason = sizing.rejected_reason if sizing is not None else None
        expected_holding = self._expected_holding(regime, features)
        maximum_holding = self.config.maximum_holding_minutes
        maximum_holding = self.config.maximum_holding_minutes
        invalidation = self._invalidation_conditions(regime=regime, features=features, snapshot=snapshot)
        if not entry_ready or signal.direction is None:
            status = TradePlanStatus.WAIT
        else:
            status = TradePlanStatus.ACTIVE
        reasons = [signal.reason] if signal.reason else []
        if sizing_reason:
            reasons.append(f"sizing_blocked:{sizing_reason}")
        if sizing is not None and not sizing.ok:
            entry_ready = False
            status = TradePlanStatus.WAIT
        final_notional = (
            position_quantity * signal.entry_reference
            if position_quantity is not None and signal.entry_reference is not None
            else None
        )
        risk_reward = (
            abs(target_price - signal.entry_reference) / abs(signal.entry_reference - stop_price)
            if target_price is not None and stop_price is not None and signal.entry_reference is not None
            and abs(signal.entry_reference - stop_price) > 0 else None
        )
        plan = TradePlan(
            plan_id=new_order_id(),
            timestamp=now,
            symbol=signal.symbol,
            market=getattr(regime, "symbol", ""),
            regime=regime.regime,
            strategy=signal.strategy,
            context_timeframe=context_timeframe,
            signal_timeframe=signal_timeframe,
            execution_timeframe=execution_timeframe,
            direction=signal.direction if entry_ready else None,
            entry_reference=signal.entry_reference if entry_ready else None,
            stop_price=stop_price if entry_ready else None,
            target_price=target_price if entry_ready else None,
            risk_amount=risk_amount,
            position_quantity=position_quantity,
            equity_snapshot=sizing.equity if sizing is not None else None,
            risk_percent=risk_percent,
            risk_budget=sizing.risk_budget if sizing is not None else None,
            stop_distance=sizing.stop_distance if sizing is not None else None,
            maximum_notional=maximum_notional,
            final_notional=final_notional,
            final_risk_amount=risk_amount,
            risk_reward=risk_reward,
            expected_holding_minutes=expected_holding,
            maximum_holding_minutes=maximum_holding,
            entry_conditions=entry_conditions,
            exit_conditions=self._exit_conditions(),
            invalidation_conditions=invalidation,
            reasons=reasons,
            status=status,
            correlation_id=correlation_id,
            session_id=session_id,
            # Phase D.1: persist the market context + provenance so the
            # decision can be reconstructed after a restart.
            extra={
                "volatility": features.volatility,
                "spread_percent": features.spread_percent,
                "volume_ratio": features.volume_ratio,
                "atr": features.atr,
                "strategy_version": getattr(signal, "strategy_version", None),
                "source_signal_id": getattr(signal, "signal_id", None),
                "risk_budget_source": "position_sizer" if sizing is not None else None,
                "equity_snapshot_source": "risk_context.account_equity" if sizing is not None else None,
                "risk_percent_source": "position_sizing.risk_per_trade_percent" if sizing is not None else None,
                "stop_distance_source": "position_sizer.signal.risk_distance" if sizing is not None else None,
                "final_risk_source": "position_sizer.final_quantity * stop_distance" if sizing is not None else None,
            },
        )
        events.append({"event": "TRADE_PLAN_CREATED" if status == TradePlanStatus.ACTIVE else "TRADE_PLAN_UPDATED", "plan_id": plan.plan_id, "status": status.value, "entry_ready": entry_ready})
        return TradePlanBuildResult(plan=plan, events=events)

    def invalidate(self, plan: TradePlan, *, reason: str, now: datetime | None = None) -> TradePlanBuildResult:
        now = now or utcnow()
        # Preserve the *incoming* status so transition_to records the real
        # previous_status. Building with status=INVALIDATED first (a bug fixed
        # here) made previous_status report "invalidated" -> "invalidated".
        incoming = plan.status
        updated = TradePlan(**{
            **plan.model_dump(),
            "status": incoming,
            "timestamp": now,
            "reasons": plan.reasons + [f"invalidated:{reason}"],
        })
        updated.invalidation_reason = reason
        updated.transition_to(TradePlanStatus.INVALIDATED, reason=reason, trigger="AUTOMATIC", source_event="INVALIDATION_CONDITION")
        return TradePlanBuildResult(plan=updated, events=[{"event": "TRADE_PLAN_INVALIDATED", "reason": reason}])

    def reactivate(self, plan: TradePlan, *, reason: str = "", now: datetime | None = None) -> TradePlanBuildResult:
        """Reactivate a WAIT plan when conditions improve."""
        now = now or utcnow()
        # Preserve the incoming status so previous_status is recorded truthfully.
        incoming = plan.status
        updated = TradePlan(**{**plan.model_dump(), "status": incoming, "timestamp": now})
        updated.transition_to(TradePlanStatus.ACTIVE, reason=reason or "conditions_improved", trigger="REACTIVATION", source_event="MARKET_UPDATE")
        return TradePlanBuildResult(plan=updated, events=[{"event": "TRADE_PLAN_UPDATED", "plan_id": updated.plan_id, "status": updated.status.value}])

    def _expected_holding(self, regime, features) -> float:
        base = self.config.default_expected_holding_minutes
        if regime.is_trending and not regime.is_unknown:
            return base * 1.5
        if features and features.volatility is not None and features.volatility > 0.02:
            return base * 0.5
        return base

    def _entry_conditions(self, *, signal, regime, features, snapshot, freshness_stale):
        return [
            {"name": "regime_known", "met": not regime.is_unknown, "detail": regime.regime.value},
            {"name": "features_ready", "met": features.ready, "detail": "ready" if features.ready else str(features.missing)},
            {"name": "atr_valid", "met": features.atr is not None and features.atr > 0, "detail": str(features.atr)},
            {"name": "market_data_fresh", "met": not freshness_stale, "detail": "stale" if freshness_stale else "fresh"},
            {"name": "spread_acceptable", "met": snapshot is None or snapshot.spread_percent is None or snapshot.spread_percent <= 0.10, "detail": str(snapshot.spread_percent if snapshot else None)},
            {"name": "signal_actionable", "met": signal.direction in (Direction.LONG, Direction.SHORT), "detail": str(signal.direction.value if signal.direction else None)},
        ]

    def _stop_target(self, signal, features, *, use_signal_levels: bool = False):
        atr = features.atr
        if signal.entry_reference is None:
            return None, None
        if use_signal_levels and signal.stop_reference is not None and abs(signal.entry_reference - signal.stop_reference) > 0:
            stop = signal.stop_reference
        elif atr is not None and atr > 0:
            stop = signal.entry_reference - self.config.stop_atr_multiplier * atr
        else:
            return None, None
        target = signal.take_profit_reference if use_signal_levels else None
        if target is None:
            direction = getattr(signal.direction, "value", signal.direction)
            multiplier = self.config.target_atr_multiplier * atr if atr is not None and atr > 0 else None
            if multiplier is None:
                return stop, None
            target = signal.entry_reference + multiplier if direction == "long" else signal.entry_reference - multiplier
        return stop, target

    def _exit_conditions(self):
        return [{"name": "target_reached", "active": True}, {"name": "stop_reached", "active": True}, {"name": "regime_change", "active": True}, {"name": "invalidation", "active": True}, {"name": "maximum_holding", "active": True}, {"name": "spread_deterioration", "active": True}]

    def _invalidation_conditions(self, *, regime, features, snapshot):
        inv = [{"name": "regime_change", "active": True}, {"name": "atr_invalid", "active": features.atr is None or features.atr <= 0}]
        if snapshot and snapshot.spread_percent is not None:
            inv.append({"name": "spread_widened", "active": snapshot.spread_percent > 0.15, "detail": str(snapshot.spread_percent)})
        return inv
