"""Event-driven AI supervisor.

AI runs only on MEANINGFUL events (order rejected, risk rejection,
reconciliation failure, system error, flatten failure) — never on ticks. It
interacts exclusively through the permission-checked tool gateway, and it is
never a single point of failure for the deterministic trading engine.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.events.types import Event, EventType

logger = logging.getLogger("app.ai.supervisor")

SYSTEM_PROMPT = (
    "You are the supervisory agent for a PAPER-TRADING system. "
    "You are read-mostly: investigate incidents using the provided tools and "
    "return a short, factual summary with cause and recommended next action. "
    "You must NEVER claim to have executed trades, and you must not request "
    "unauthorized actions. If a tool is denied, report that clearly. "
    "Keep answers under 120 words."
)


class AISupervisor:
    def __init__(self, config, provider, gateway, engine, *, repository=None) -> None:
        self.config = config
        self.provider = provider
        self.gateway = gateway
        self.engine = engine
        self.repository = repository
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._investigations = 0
        self._tool_calls = 0
        self._rejected = 0

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if not self.config.ai.enabled:
            self._set_status("disabled", detail="ai.enabled=false")
            return
        healthy = await self.provider.health()
        self._set_status("healthy" if healthy else "unavailable")
        if not healthy:
            logger.warning(
                "AI_UNAVAILABLE (trading continues)",
                extra={
                    "structured": {
                        "event": "AI_UNAVAILABLE",
                        "component": "ai.supervisor",
                        "detail": self.provider.last_error,
                    }
                },
            )
        self.engine.bus.subscribe_all(self._on_event)

    def _set_status(self, status: str, detail: str | None = None) -> None:
        self.engine.state.ai["status"] = status
        if detail:
            self.engine.state.ai["detail"] = detail

    # -- triggers -----------------------------------------------------------
    def _triggers(self) -> dict[EventType, str]:
        behavior = self.config.ai.behavior
        triggers: dict[EventType, str] = {}
        if behavior.investigate_order_rejection:
            triggers[EventType.ORDER_REJECTED] = "order_rejection"
        if behavior.investigate_risk_failure:
            triggers[EventType.RISK_REJECTED] = "risk_failure"
        if behavior.investigate_reconciliation_failure:
            triggers[EventType.RECONCILIATION_FAILED] = "reconciliation_failure"
        if behavior.investigate_system_error:
            triggers[EventType.SYSTEM_ERROR] = "system_error"
        if behavior.investigate_unexpected_position:
            triggers[EventType.FLATTEN_FAILED] = "unexpected_position"
        return triggers

    async def _on_event(self, event: Event) -> None:
        kind = self._triggers().get(event.type)
        if kind is None or not self.config.ai.enabled:
            return
        # Run the investigation in the background so the deterministic engine
        # is never blocked by LLM latency.
        task = asyncio.create_task(self.investigate(event, kind))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- investigation ------------------------------------------------------
    async def investigate(self, event: Event, kind: str) -> None:
        if self._lock.locked():
            return  # selective and non-bursting: one investigation at a time
        async with self._lock:
            self._investigations += 1
            self.engine.state.ai["investigations"] = self._investigations
            await self.engine.bus.emit(
                EventType.AI_INVESTIGATION_STARTED,
                payload={"kind": kind, "event": event.type.value},
                correlation_id=event.correlation_id,
            )
            try:
                summary = await self._run(event, kind)
            except Exception as exc:  # noqa: BLE001 - AI failure must not affect trading
                summary = {"error": str(exc)}
                self._set_status("unavailable", detail=str(exc))
                logger.warning(
                    "AI investigation failed (trading continues)",
                    extra={
                        "structured": {
                            "event": "AI_UNAVAILABLE",
                            "component": "ai.supervisor",
                            "error": str(exc),
                        }
                    },
                )
            self.engine.state.ai["last_event"] = summary
            await self.engine.bus.emit(
                EventType.AI_INVESTIGATION_COMPLETED,
                payload={"kind": kind, "summary": summary},
                correlation_id=event.correlation_id,
            )

    async def _run(self, event: Event, kind: str) -> dict[str, Any]:
        limit = self.config.ai.limits.maximum_tool_calls_per_event
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"Investigate a {kind} event.\n"
                    f"Event type: {event.type.value}\n"
                    f"Payload: {json.dumps(event.payload, default=str)}\n"
                    f"Timestamp: {event.timestamp.isoformat()}\n"
                    "Use read-only tools to gather evidence, then summarize cause and recommended action."
                ),
            }
        ]
        tools = self.gateway.schema()
        used = 0
        final_text = ""
        while used < limit:
            result = await self.provider.complete(system=SYSTEM_PROMPT, messages=messages, tools=tools)
            if result.get("error"):
                raise RuntimeError(str(result.get("error")))
            tool_calls = result.get("tool_calls") or []
            if not tool_calls:
                final_text = result.get("text", "")
                break
            messages.append({"role": "assistant", "content": result.get("text", "")})
            for call in tool_calls:
                used += 1
                self._tool_calls += 1
                self.engine.state.ai["tool_calls"] = self._tool_calls
                output = await self._execute_tool(call)
                messages.append({"role": "tool", "content": json.dumps(output, default=str)})
        return {"kind": kind, "tool_calls_used": used, "summary": final_text}

    async def _execute_tool(self, call: dict[str, Any]) -> dict[str, Any]:
        name = call.get("name", "")
        arguments = call.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        try:
            output = await self.gateway.call(name, arguments, requester="ai")
            if self.repository is not None:
                await self.repository.save_ai_action(
                    session_id=self.engine.session.session_id,
                    action="tool_call",
                    tool=name,
                    permitted=True,
                    payload={"arguments": arguments},
                )
            return {"tool": name, "result": output}
        except Exception as exc:  # noqa: BLE001 - denied / unknown / failed tools
            self._rejected += 1
            self.engine.state.ai["rejected_actions"] = self._rejected
            logger.warning(
                "AI tool rejected",
                extra={
                    "structured": {
                        "event": "AI_ACTION_REJECTED",
                        "component": "ai.supervisor",
                        "tool": name,
                        "reason": str(exc),
                    }
                },
            )
            if self.repository is not None:
                await self.repository.save_ai_action(
                    session_id=self.engine.session.session_id,
                    action="tool_call",
                    tool=name,
                    permitted=False,
                    payload={"arguments": arguments, "error": str(exc)},
                )
            return {"tool": name, "error": str(exc)}
