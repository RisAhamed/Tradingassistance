"""Controlled tool gateway.

AI -> Tool Gateway -> Permission Check -> Validation -> Tool -> Result

The AI never calls the broker API directly, and it cannot bypass risk, OMS,
session management, or permission checks. Every call is logged.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.config.models import AiConfig
from app.core.errors import ToolPermissionError, UnknownToolError
from app.domain.enums import Permission

logger = logging.getLogger("app.ai.gateway")

Handler = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class ToolSpec:
    name: str
    permission: Permission
    description: str
    handler: Handler
    config_key: str | None = None  # key under ai.permissions for non-read-only
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


class ToolGateway:
    def __init__(self, config: AiConfig, *, requester: str = "ai") -> None:
        self.config = config
        self.requester = requester
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schema(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    def _permitted(self, spec: ToolSpec) -> bool:
        permissions = self.config.permissions
        if spec.permission is Permission.READ_ONLY:
            return permissions.read_only
        if spec.config_key is None:
            return False
        return bool(getattr(permissions, spec.config_key, False))

    async def call(self, name: str, arguments: dict[str, Any] | None = None, *, requester: str) -> Any:
        arguments = arguments or {}
        spec = self._tools.get(name)
        if spec is None:
            logger.error(
                "unknown tool requested",
                extra={"structured": {"event": "AI_ACTION_REJECTED", "component": "ai.gateway", "tool": name, "requester": requester, "reason": "unknown_tool"}},
            )
            raise UnknownToolError(name)

        if not self._permitted(spec):
            logger.warning(
                "tool not permitted",
                extra={"structured": {"event": "AI_ACTION_REJECTED", "component": "ai.gateway", "tool": name, "requester": requester, "reason": "permission_denied"}},
            )
            raise ToolPermissionError(f"{name} not permitted")

        logger.info(
            "tool called",
            extra={"structured": {"event": "AI_TOOL_CALLED", "component": "ai.gateway", "tool": name, "requester": requester, "permission": spec.permission.value}},
        )
        # Validate arguments against the JSON schema (unknown keys rejected).
        allowed = set(spec.parameters.get("properties", {}) or {})
        if allowed:
            extra = set(arguments) - allowed
            if extra:
                raise ToolPermissionError(f"unexpected arguments: {sorted(extra)}")
        return await spec.handler(**arguments)
