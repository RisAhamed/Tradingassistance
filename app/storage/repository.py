"""Repository: persists domain entities for traceability.

Traceability chain: Signal -> RiskDecision -> Order -> Fill -> Position ->
Trade -> P&L.

Persistence here is *auxiliary* to safety (safety is guaranteed by broker-side
reconciliation), so failures are logged loudly and counted in
:meth:`Repository.failures` rather than being swallowed silently.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.domain.orders import Fill, Order
from app.domain.pnl import PnlSnapshot, TradeRecord
from app.domain.risk import RiskDecision
from app.domain.signals import StrategySignal

logger = logging.getLogger("app.storage.repository")


class Repository:
    def __init__(
        self,
        session_factory: async_sessionmaker | None,
        *,
        database=None,
        engine: AsyncEngine | None = None,  # kept for future use
    ) -> None:
        self._session_factory = session_factory
        self._database = database
        self.failures = 0

    async def init(self) -> None:
        """Create tables if a database is attached (no-op otherwise)."""
        if self._database is not None:
            await self._database.init()

    @property
    def available(self) -> bool:
        return self._session_factory is not None

    async def _run(self, operation: Callable[[Any], Awaitable[None]], name: str) -> bool:
        if self._session_factory is None:
            return False
        try:
            async with self._session_factory() as session:
                await operation(session)
                await session.commit()
            return True
        except Exception as exc:  # noqa: BLE001 - logged + counted, never fatal
            self.failures += 1
            logger.error(
                "persistence failure",
                extra={
                    "structured": {
                        "event": "PERSISTENCE_ERROR",
                        "component": "storage.repository",
                        "operation": name,
                        "error": str(exc),
                    }
                },
            )
            return False

    # -- entities -----------------------------------------------------------
    async def save_session(
        self, session_id: str, state: str, started_at: datetime | None, summary: dict
    ) -> None:
        from app.storage.models import SessionRow

        async def op(db) -> None:
            row = await db.get(SessionRow, session_id)
            if row is None:
                db.add(
                    SessionRow(
                        session_id=session_id, started_at=started_at, state=state, summary=summary
                    )
                )
            else:
                row.state = state
                row.summary = summary

        await self._run(op, "save_session")

    async def save_signal(self, signal: StrategySignal) -> None:
        from app.storage.models import SignalRow

        async def op(db) -> None:
            db.add(
                SignalRow(
                    signal_id=signal.signal_id,
                    session_id=signal.session_id,
                    correlation_id=signal.correlation_id,
                    created_at=signal.timestamp,
                    symbol=signal.symbol,
                    direction=signal.direction.value,
                    reason_code=signal.reason_code.value,
                    payload=signal.model_dump(mode="json"),
                )
            )

        await self._run(op, "save_signal")

    async def save_risk_decision(self, decision: RiskDecision) -> None:
        from app.storage.models import RiskDecisionRow

        async def op(db) -> None:
            db.add(
                RiskDecisionRow(
                    risk_id=decision.risk_id,
                    signal_id=decision.signal_id,
                    session_id=decision.session_id,
                    correlation_id=decision.correlation_id,
                    created_at=decision.timestamp,
                    symbol=decision.symbol,
                    approved=decision.approved,
                    reason=decision.reason.value,
                    payload=decision.model_dump(mode="json"),
                )
            )

        await self._run(op, "save_risk_decision")

    async def save_order(self, order: Order) -> None:
        from app.storage.models import OrderRow

        async def op(db) -> None:
            row = await db.get(OrderRow, order.order_id)
            payload = order.model_dump(mode="json")
            if row is None:
                db.add(
                    OrderRow(
                        order_id=order.order_id,
                        client_order_id=order.client_order_id,
                        session_id=order.session_id,
                        correlation_id=order.correlation_id,
                        created_at=order.submitted_at or order.updated_at or _now(),
                        symbol=order.symbol,
                        side=order.side.value,
                        status=order.status.value,
                        quantity=order.quantity,
                        filled_quantity=order.filled_quantity,
                        average_fill_price=order.average_fill_price,
                        payload=payload,
                    )
                )
            else:
                # Order lifecycle writes twice (CREATED then SUBMITTED/FILLED /
                # REJECTED) — upsert rather than inserting a duplicate PK row.
                row.status = order.status.value
                row.filled_quantity = order.filled_quantity
                row.average_fill_price = order.average_fill_price
                row.payload = payload

        await self._run(op, "save_order")

    async def save_fill(self, fill: Fill, *, session_id: str | None) -> None:
        from app.storage.models import FillRow

        async def op(db) -> None:
            db.add(
                FillRow(
                    fill_id=fill.fill_id,
                    order_id=fill.order_id,
                    session_id=session_id,
                    created_at=fill.timestamp,
                    symbol=fill.symbol,
                    side=fill.side.value,
                    quantity=fill.quantity,
                    price=fill.price,
                    fee=fill.fee,
                )
            )

        await self._run(op, "save_fill")

    async def save_trade(self, trade: TradeRecord) -> None:
        from app.storage.models import TradeRow

        async def op(db) -> None:
            db.add(
                TradeRow(
                    trade_id=trade.trade_id,
                    session_id=trade.session_id,
                    correlation_id=trade.correlation_id,
                    closed_at=trade.closed_at,
                    symbol=trade.symbol,
                    direction=trade.direction.value,
                    quantity=trade.quantity,
                    realized_pnl=trade.realized_pnl,
                    payload=trade.model_dump(mode="json"),
                )
            )

        await self._run(op, "save_trade")

    async def save_pnl(self, pnl: PnlSnapshot, *, session_id: str | None) -> None:
        from app.storage.models import PnlRecordRow

        async def op(db) -> None:
            db.add(
                PnlRecordRow(
                    session_id=session_id,
                    created_at=pnl.timestamp,
                    symbol=pnl.symbol,
                    realized=pnl.realized,
                    unrealized=pnl.unrealized,
                    fees=pnl.fees,
                    equity=pnl.equity,
                )
            )

        await self._run(op, "save_pnl")

    async def save_event(self, event: Any) -> None:
        from app.storage.models import SystemEventRow

        async def op(db) -> None:
            db.add(
                SystemEventRow(
                    event_id=event.event_id,
                    session_id=event.session_id,
                    correlation_id=event.correlation_id,
                    event_type=event.type.value if hasattr(event.type, "value") else str(event.type),
                    created_at=event.timestamp,
                    payload=event.payload,
                )
            )

        await self._run(op, "save_event")

    async def save_ai_action(
        self, *, session_id: str | None, action: str, tool: str | None, permitted: bool, payload: dict
    ) -> None:
        from app.storage.models import AiActionRow

        async def op(db) -> None:
            db.add(
                AiActionRow(
                    session_id=session_id,
                    created_at=_now(),
                    action=action,
                    tool=tool,
                    permitted=permitted,
                    payload=payload,
                )
            )

        await self._run(op, "save_ai_action")

    # -- TradePlan persistence ---------------------------------------------

    async def save_trade_plan(self, plan: dict, *, session_id: str | None) -> bool:
        """Persist a TradePlan row. Insert or update (upsert on plan_id)."""
        from app.storage.models import TradePlanRow

        async def op(db) -> None:
            row = await db.get(TradePlanRow, plan.get("plan_id"))
            now = _now()
            # Phase D.1 FIX: the domain model names these fields stop_price /
            # target_price / position_quantity. Reading "stop_reference" and
            # "target_reference" (the *column* names) silently persisted NULLs.
            stop_ref = plan.get("stop_price", plan.get("stop_reference"))
            target_ref = plan.get("target_price", plan.get("target_reference"))
            quantity = plan.get("position_quantity", plan.get("quantity"))
            reason_codes = {
                "reasons": plan.get("reasons") or [],
                "entry_conditions": plan.get("entry_conditions") or [],
                "invalidation_conditions": plan.get("invalidation_conditions") or [],
            }
            if row is None:
                db.add(
                    TradePlanRow(
                        plan_id=plan.get("plan_id"),
                        session_id=session_id,
                        correlation_id=plan.get("correlation_id"),
                        created_at=_as_datetime(plan.get("timestamp")) or now,
                        updated_at=now,
                        symbol=plan.get("symbol"),
                        direction=plan.get("direction"),
                        status=plan.get("status"),
                        context_timeframe=plan.get("context_timeframe"),
                        signal_timeframe=plan.get("signal_timeframe"),
                        execution_timeframe=plan.get("execution_timeframe"),
                        entry_reference=plan.get("entry_reference"),
                        stop_reference=stop_ref,
                        target_reference=target_ref,
                        quantity=quantity,
                        risk_amount=plan.get("risk_amount"),
                        expected_holding_minutes=plan.get("expected_holding_minutes"),
                        maximum_holding_minutes=plan.get("maximum_holding_minutes"),
                        regime=plan.get("regime"),
                        volatility_context=(plan.get("extra") or {}).get("volatility"),
                        spread_context=(plan.get("extra") or {}).get("spread_percent"),
                        liquidity_context=(plan.get("extra") or {}).get("volume_ratio"),
                        strategy_name=plan.get("strategy"),
                        strategy_version=(plan.get("extra") or {}).get("strategy_version"),
                        reason_codes=reason_codes,
                        invalidation_reason=plan.get("invalidation_reason"),
                        source_signal_id=(plan.get("extra") or {}).get("source_signal_id"),
                        payload=plan,
                    )
                )
            else:
                row.updated_at = now
                row.status = plan.get("status", row.status)
                row.direction = plan.get("direction", row.direction)
                row.entry_reference = plan.get("entry_reference", row.entry_reference)
                row.stop_reference = stop_ref
                row.target_reference = target_ref
                row.quantity = quantity
                row.invalidation_reason = plan.get(
                    "invalidation_reason", row.invalidation_reason)
                row.reason_codes = reason_codes
                row.payload = plan

        return await self._run(op, "save_trade_plan")

    async def load_trade_plan(self, plan_id: str) -> dict | None:
        """Load a persisted TradePlan by plan_id."""
        from app.storage.models import TradePlanRow

        if self._session_factory is None:
            return None
        try:
            async with self._session_factory() as session:
                result = await session.get(TradePlanRow, plan_id)
                if result is None:
                    return None
                return result.payload
        except Exception as exc:  # noqa: BLE001
            logger.error("load_trade_plan failed: %s", exc)
            return None

    async def load_latest_trade_plan(self) -> dict | None:
        """Load the most recent TradePlan row."""
        from app.storage.models import TradePlanRow

        if self._session_factory is None:
            return None
        try:
            async with self._session_factory() as session:
                result = (
                    await session.execute(
                        select(TradePlanRow).order_by(TradePlanRow.updated_at.desc()).limit(1)
                    )
                ).scalar_one_or_none()
                return result.payload if result else None
        except Exception as exc:  # noqa: BLE001
            logger.error("load_latest_trade_plan failed: %s", exc)
            return None


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _as_datetime(value: Any) -> datetime | None:
    """Accept a datetime or an ISO-8601 string (JSON-dumped models).

    ``model_dump(mode="json")`` renders timestamps as strings, which SQLite's
    DateTime column rejects. Returns None when the value is unusable so the
    caller can fall back rather than failing the whole write.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None
