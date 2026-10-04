"""Asynchronous in-process event bus.

Handlers are invoked in registration order. A failing handler is logged and
isolated so that one broken subscriber can never stop the trading engine.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from app.events.types import Event, EventType, make_event

logger = logging.getLogger("app.events.bus")

Handler = Callable[[Event], Awaitable[None] | None]
EventFilter = Callable[[Event], bool]


class EventBus:
    def __init__(self, history_size: int = 500) -> None:
        self._handlers: dict[EventType, list[Handler]] = {}
        self._global: list[Handler] = []
        self._history: deque[Event] = deque(maxlen=history_size)

    # -- registration -------------------------------------------------------
    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def subscribe_all(self, handler: Handler) -> None:
        self._global.append(handler)

    def remove(self, event_type: EventType, handler: Handler) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def remove_all(self, handler: Handler) -> None:
        if handler in self._global:
            self._global.remove(handler)

    # -- publishing ---------------------------------------------------------
    async def publish(self, event: Event) -> None:
        self._history.append(event)
        for handler in [*self._global, *self._handlers.get(event.type, [])]:
            await self._safe_call(handler, event)

    async def emit(
        self,
        event_type: EventType,
        *,
        payload: dict[str, Any] | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
    ) -> Event:
        event = make_event(
            event_type,
            payload=payload,
            session_id=session_id,
            correlation_id=correlation_id,
        )
        await self.publish(event)
        return event

    async def _safe_call(self, handler: Handler, event: Event) -> None:
        try:
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - isolated, logged, never fatal
            logger.exception(
                "event handler failed",
                extra={
                    "structured": {
                        "event": "EVENT_HANDLER_ERROR",
                        "component": "events.bus",
                        "event_type": event.type.value,
                        "handler": getattr(handler, "__qualname__", repr(handler)),
                    }
                },
            )

    # -- introspection ------------------------------------------------------
    def recent(self, limit: int = 100, event_filter: EventFilter | None = None) -> list[Event]:
        events = list(self._history)
        if event_filter is not None:
            events = [event for event in events if event_filter(event)]
        return events[-limit:]

    def recent_public(self, limit: int = 100) -> list[dict[str, Any]]:
        return [event.to_public() for event in self.recent(limit)]
