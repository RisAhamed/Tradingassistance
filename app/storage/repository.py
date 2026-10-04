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


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)
