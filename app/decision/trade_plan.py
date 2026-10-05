"""TradePlan builder — deterministic, risk-driven candidate trading plan."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config.models import TradePlanConfig
from app.core.ids import new_order_id
from app.domain.enums import Direction
from app.domain.trade_plan import TradePlan, TradePlanStatus


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
    ) -> TradePlanBuildResult:
        now = datetime.utcnow()
        events: list[dict[str, Any]] = []
        entry_conditions = self._entry_conditions(
            signal=signal, regime=regime, features=features,
            snapshot=snapshot, freshness_stale=freshness_stale,
        )
        entry_ready = all(c["met"] for c in entry_conditions)
        stop_price, target_price = self._stop_target(signal, features)
        risk_amount = self._risk_amount(signal, features, snapshot)
        position_quantity = None
        if signal.entry_reference and stop_price and risk_amount > 0:
            distance = abs(signal.entry_reference - stop_price)
            if distance > 0:
                position_quantity = risk_amount / distance
        expected_holding = self.config.default_expected_holding_minutes
        maximum_holding = self.config.maximum_holding_minutes
        invalidation = self._invalidation_conditions(regime=regime, features=features, snapshot=snapshot)
        if not entry_ready or signal.direction is None:
            status = TradePlanStatus.WAIT
        else:
            status = TradePlanStatus.READY
        reasons = [signal.reason] if signal.reason else []
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
            expected_holding_minutes=expected_holding,
            maximum_holding_minutes=maximum_holding,
            entry_conditions=entry_conditions,
            exit_conditions=self._exit_conditions(),
            invalidation_conditions=invalidation,
            reasons=reasons,
            status=status,
            correlation_id=correlation_id,
            session_id=session_id,
        )
        events.append({"event": "TRADE_PLAN_CREATED" if status == TradePlanStatus.READY else "TRADE_PLAN_UPDATED", "plan_id": plan.plan_id, "status": status.value, "entry_ready": entry_ready})
        return TradePlanBuildResult(plan=plan, events=events)

    def invalidate(self, plan: TradePlan, *, reason: str, now: datetime | None = None) -> TradePlanBuildResult:
        now = now or datetime.utcnow()
        updated = TradePlan(**{**plan.model_dump(), "status": TradePlanStatus.INVALIDATED, "timestamp": now, "reasons": plan.reasons + [f"invalidated:{reason}"]})
        return TradePlanBuildResult(plan=updated, events=[{"event": "TRADE_PLAN_INVALIDATED", "reason": reason}])

    def _entry_conditions(self, *, signal, regime, features, snapshot, freshness_stale):
        return [
            {"name": "regime_known", "met": not regime.is_unknown, "detail": regime.regime.value},
            {"name": "features_ready", "met": features.ready, "detail": "ready" if features.ready else str(features.missing)},
            {"name": "atr_valid", "met": features.atr is not None and features.atr > 0, "detail": str(features.atr)},
            {"name": "market_data_fresh", "met": not freshness_stale, "detail": "stale" if freshness_stale else "fresh"},
            {"name": "spread_acceptable", "met": snapshot is None or snapshot.spread_percent is None or snapshot.spread_percent <= 0.10, "detail": str(snapshot.spread_percent if snapshot else None)},
            {"name": "signal_actionable", "met": signal.direction in (Direction.LONG, Direction.SHORT), "detail": str(signal.direction.value if signal.direction else None)},
        ]

    def _stop_target(self, signal, features):
        atr = features.atr
        if atr is None or atr <= 0 or signal.entry_reference is None:
            return None, None
        stop = signal.entry_reference - self.config.stop_atr_multiplier * atr
        target = signal.entry_reference + self.config.target_atr_multiplier * atr
        return stop, target

    def _risk_amount(self, signal, features, snapshot):
        notional = 100_000.0
        risk_pct = 0.01
        if snapshot and snapshot.price:
            notional = snapshot.price * 1.0
        return notional * risk_pct

    def _exit_conditions(self):
        return [{"name": "target_reached", "active": True}, {"name": "stop_reached", "active": True}, {"name": "regime_change", "active": True}, {"name": "invalidation", "active": True}, {"name": "maximum_holding", "active": True}, {"name": "spread_deterioration", "active": True}]

    def _invalidation_conditions(self, *, regime, features, snapshot):
        inv = [{"name": "regime_change", "active": True}, {"name": "atr_invalid", "active": features.atr is None or features.atr <= 0}]
        if snapshot and snapshot.spread_percent is not None:
            inv.append({"name": "spread_widened", "active": snapshot.spread_percent > 0.15, "detail": str(snapshot.spread_percent)})
        return inv
