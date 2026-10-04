"""Session lifecycle domain models."""
from __future__ import annotations

from datetime import datetime, time

from pydantic import BaseModel

from app.domain.enums import SessionState


class SessionSnapshot(BaseModel):
    session_id: str
    state: SessionState = SessionState.OFFLINE
    started_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str = "UTC"
    entry_cutoff: time | None = None
    flatten_deadline: time | None = None
    entries_allowed: bool = False
    closeout_started: bool = False
    is_flat: bool = True
    seconds_remaining: float | None = None
    note: str | None = None


class SessionSummary(BaseModel):
    session_id: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    final_state: SessionState = SessionState.COMPLETED
    flattened: bool = False
    trades: int = 0
    orders: int = 0
    signals: int = 0
    rejections: int = 0
    realized_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
