"""Identifier generation with typed prefixes for end-to-end traceability."""
from __future__ import annotations

import uuid

_PREFIXES = {
    "session": "ses",
    "correlation": "cor",
    "signal": "sig",
    "order": "ord",
    "position": "pos",
    "trade": "trd",
    "event": "evt",
    "risk": "rsk",
    "fill": "fil",
    "ai": "ai",
}


def new_id(kind: str) -> str:
    """Create a new short identifier, e.g. ``ord_1a2b3c4d5e6f``."""
    prefix = _PREFIXES.get(kind, kind[:3] or "id")
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_session_id() -> str:
    return new_id("session")


def new_correlation_id() -> str:
    return new_id("correlation")


def new_signal_id() -> str:
    return new_id("signal")


def new_order_id() -> str:
    return new_id("order")


def new_position_id() -> str:
    return new_id("position")


def new_trade_id() -> str:
    return new_id("trade")


def new_event_id() -> str:
    return new_id("event")


def new_risk_id() -> str:
    return new_id("risk")


def new_fill_id() -> str:
    return new_id("fill")


def new_ai_id() -> str:
    return new_id("ai")
