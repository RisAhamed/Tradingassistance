"""Normalized sentiment snapshot (symbol-agnostic, provider-agnostic).

``score`` is the ONLY required numeric: it must be finite and inside
``[-1.0, +1.0]``. A provider that cannot produce a valid score raises
:class:`app.sentiment.errors.SentimentError` — it never fabricates one.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.sentiment.errors import SentimentError


def _check_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise SentimentError("SentimentSnapshot.timestamp must be timezone-aware (UTC)")
    return value


class SentimentSnapshot(BaseModel):
    timestamp: datetime
    symbol: str = Field(min_length=1)
    source: str = Field(min_length=1)
    score: float
    fear_greed: float | None = None
    funding_rate: float | None = None
    open_interest_change_24h: float | None = None
    news_volume: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    _aware = field_validator("timestamp", mode="before")(_check_aware)  # type: ignore[assignment]

    @field_validator("score")
    @classmethod
    def _valid_score(cls, value: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise SentimentError(f"sentiment score must be numeric: {value!r}") from exc
        if not math.isfinite(numeric) or not -1.0 <= numeric <= 1.0:
            raise SentimentError(f"sentiment score out of range [-1, 1]: {value!r}")
        return numeric

    @field_validator("fear_greed")
    @classmethod
    def _valid_fear_greed(cls, value: float | None) -> float | None:
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise SentimentError(f"fear_greed must be numeric: {value!r}") from exc
        if not math.isfinite(numeric) or not 0.0 <= numeric <= 100.0:
            raise SentimentError(f"fear_greed out of range [0, 100]: {value!r}")
        return numeric

    @field_validator("funding_rate", "open_interest_change_24h")
    @classmethod
    def _finite_or_none(cls, value: float | None) -> float | None:
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise SentimentError(f"microstructure field must be numeric: {value!r}") from exc
        if not math.isfinite(numeric):
            raise SentimentError(f"microstructure field must be finite: {value!r}")
        return numeric

    @field_validator("news_volume")
    @classmethod
    def _valid_news_volume(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SentimentError(f"news_volume must be a non-negative int: {value!r}")
        return value

    def age_seconds(self, now: datetime) -> float:
        return max(0.0, (now - self.timestamp).total_seconds())

    def sanitized(self) -> dict[str, Any]:
        """JSON-safe, secret-free view for API / dashboard / events."""
        return self.model_dump(mode="json")
