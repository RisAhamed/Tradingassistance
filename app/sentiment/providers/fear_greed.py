"""Fear & Greed sentiment provider (free, no key required).

Official docs: https://api.alternative.me/doc/  (endpoint
``https://api.alternative.me/fng/``).

The upstream index is a market-wide gauge, not an instrument quote: this
provider therefore accepts any non-empty symbol and annotates the returned
snapshot with it, so the observation layer stays symbol-parameterized while
the underlying gauge is market-wide. ``score`` is the normalized mapping of
the 0–100 index value onto ``[-1, +1]``; ``fear_greed`` carries the raw
index value. HTTP goes through :mod:`httpx` only — no vendor SDK.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.core.clock import utcnow
from app.sentiment.errors import SentimentError, UnsupportedSymbolError
from app.sentiment.models import SentimentSnapshot
from app.sentiment.provider import ProviderHealth, SentimentProvider
from app.sentiment.registry import register

logger = logging.getLogger("app.sentiment.fear_greed")

_FNG_URL = "https://api.alternative.me/fng/"
_CLASSIFICATIONS = ("extreme fear", "fear", "neutral", "greed", "extreme greed")


def _sanitize_raw(value: object) -> dict:
    """Keep only small JSON scalars from upstream; drop secrets-shaped keys."""
    if not isinstance(value, dict):
        return {}
    clean: dict = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 64:
            continue
        lowered = key.lower()
        if any(token in lowered for token in ("key", "secret", "token", "password", "auth")):
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            text = str(item)
            if len(text) <= 256:
                clean[key] = item
        if len(clean) >= 20:
            break
    return clean


@register("fear_greed")
class FearGreedProvider(SentimentProvider):
    name = "fear_greed"

    def __init__(self, *, http_timeout_seconds: float = 10.0, base_url: str = _FNG_URL) -> None:
        self._timeout = float(http_timeout_seconds)
        self._base_url = base_url
        self._last_at = None
        self._last_error: str | None = None

    async def fetch(self, symbol: str) -> SentimentSnapshot:
        if not symbol or not symbol.strip():
            raise UnsupportedSymbolError("fear_greed provider requires a non-empty symbol")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(self._base_url, params={"limit": 1, "format": "json"})
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 - wrapped, never fabricated
            self._last_error = str(exc)
            raise SentimentError(f"fear_greed fetch failed: {exc}") from exc
        try:
            entry = (payload.get("data") or [])[0]
            value = float(entry["value"])
            stamp = int(entry["timestamp"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            self._last_error = str(exc)
            raise SentimentError(f"fear_greed unexpected response shape: {exc}") from exc
        if not 0.0 <= value <= 100.0:
            raise SentimentError(f"fear_greed value out of range [0, 100]: {value!r}")
        classification = str(entry.get("value_classification", "")).lower() or None
        if classification is not None and classification not in _CLASSIFICATIONS:
            classification = None
        score = round((value - 50.0) / 50.0, 4)
        now = utcnow()
        self._last_at = now
        self._last_error = None
        observed = datetime.fromtimestamp(stamp, tz=timezone.utc)
        return SentimentSnapshot(
            timestamp=now,
            symbol=symbol.strip(),
            source="fear_greed",
            score=score,
            fear_greed=value,
            funding_rate=None,
            open_interest_change_24h=None,
            news_volume=None,
            raw={
                "observed_at": observed.isoformat(),
                **_sanitize_raw({"value_classification": classification, "value": value}),
            },
        )

    def health(self) -> ProviderHealth:
        detail = "fear_greed feed" if self._last_error is None else f"fear_greed error: {self._last_error}"
        return ProviderHealth(
            connected=self._last_error is None,
            detail=detail,
            last_message_at=self._last_at,
        )
