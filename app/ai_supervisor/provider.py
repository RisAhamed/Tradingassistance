"""AI provider abstraction (Ollama HTTP API via httpx).

The provider is optional: if it is unreachable the deterministic trading
engine keeps running and the failure is recorded (never a single point of
failure).
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

logger = logging.getLogger("app.ai.provider")


class AIProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def health(self) -> bool: ...

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        force_json: bool = False,
    ) -> dict[str, Any]:
        """Return ``{"text": str, "tool_calls": list, "raw": dict}``."""
        ...


class UnavailableProvider(AIProvider):
    """Placeholder used when AI is disabled or no credentials exist."""

    name = "unavailable"

    async def health(self) -> bool:
        return False

    async def complete(self, *, system, messages, tools=None, force_json=False):
        return {"text": "", "tool_calls": [], "raw": {}, "error": "unavailable"}


class OllamaProvider(AIProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        model: str = "",
        temperature: float = 0.1,
        max_tokens: int = 2000,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.last_error: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                self.last_error = None
                return True
        except Exception as exc:  # noqa: BLE001 - reported via health status
            self.last_error = str(exc)
            return False

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                return [m.get("name", "") for m in response.json().get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        force_json: bool = False,
    ) -> dict[str, Any]:
        if not self.model:
            models = await self.list_models()
            if models:
                self.model = models[0]
            else:
                self.last_error = "no ollama model available"
                return {"text": "", "tool_calls": [], "raw": {}, "error": self.last_error}

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            "options": {"temperature": self.temperature, "num_predict": self.max_tokens},
        }
        if force_json:
            payload["format"] = "json"
        if tools:
            payload["tools"] = tools

        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # noqa: BLE001 - AI must never break trading
            self.last_error = str(exc)
            logger.warning(
                "ollama call failed",
                extra={"structured": {"event": "AI_UNAVAILABLE", "component": "ai.provider", "error": str(exc)}},
            )
            return {"text": "", "tool_calls": [], "raw": {}, "error": str(exc)}

        self.last_error = None
        message = data.get("message", {}) or {}
        text = message.get("content", "")
        tool_calls = _normalize_tool_calls(message.get("tool_calls") or [])
        if force_json and text:
            try:
                text = json.dumps(json.loads(text))
            except json.JSONDecodeError:
                pass
        return {"text": text, "tool_calls": tool_calls, "raw": data}


def _normalize_tool_calls(calls: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function", {}) or {}
        name = function.get("name")
        if not name:
            continue
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"_raw": arguments}
        normalized.append({"name": name, "arguments": arguments if isinstance(arguments, dict) else {}})
    return normalized
