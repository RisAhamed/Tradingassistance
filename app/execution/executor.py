"""Order execution: submits validated orders through the OMS.

A network failure during submission is *ambiguous* — the broker may or may not
have accepted the order. The correct response is therefore NOT a blind retry
(which could create two trades) but to flag reconciliation and block new
entries until broker state is verified.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.core.errors import OrderRejected
from app.orders.oms import DuplicateOrderError, OMS
from app.domain.orders import Fill, Order

logger = logging.getLogger("app.execution")


@dataclass(slots=True)
class ExecutionResult:
    order: Order | None = None
    fills: list[Fill] = field(default_factory=list)
    rejected: bool = False
    ambiguous: bool = False
    reason: str = ""

    @property
    def submitted_ok(self) -> bool:
        return self.order is not None and not self.rejected and not self.ambiguous


class OrderExecutor:
    def __init__(self, oms: OMS) -> None:
        self.oms = oms

    async def submit(self, order: Order) -> ExecutionResult:
        if not self.oms.validate(order):
            return ExecutionResult(order=order, rejected=True, reason=order.reject_reason or "invalid")
        try:
            order, execution = await self.oms.submit(order)
        except DuplicateOrderError as exc:
            logger.error(
                "duplicate submission blocked",
                extra={"structured": {"event": "ORDER_DUPLICATE_BLOCKED", "component": "execution", "order_id": order.order_id, "error": str(exc)}},
            )
            return ExecutionResult(order=order, rejected=True, reason="duplicate_submission")
        except Exception as exc:  # noqa: BLE001 - ambiguous network failure
            logger.critical(
                "ambiguous order submission failure",
                extra={"structured": {"event": "ORDER_SUBMISSION_AMBIGUOUS", "component": "execution", "order_id": order.order_id, "error": str(exc)}},
            )
            order.status = order.status  # unchanged: outcome unknown
            return ExecutionResult(order=order, ambiguous=True, reason=str(exc))

        if execution.rejected:
            raise OrderRejected(execution.reject_reason or "rejected")
        return ExecutionResult(order=order, fills=list(execution.fills), rejected=False)
