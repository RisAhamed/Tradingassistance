"""Session manager: enforces the flat-at-session-end invariant.

States: OFFLINE, STARTING, READY, TRADING, PAUSED, CLOSEOUT, HALTED,
RECONCILIATION, COMPLETED.

The manager owns *policy* (when entries stop, when closeout must begin, whether
we are flat). The engine performs the actual flatten against the broker and
reports results back via :meth:`mark_flat` / :meth:`halt`.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.clock import utcnow
from app.core.ids import new_session_id
from app.domain.enums import SessionState
from app.domain.session import SessionSnapshot, SessionSummary

# Explicit allowed transitions (D.1.4). Anything not listed fails closed:
# the session is HALTED and entries are blocked.
_ALLOWED_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.OFFLINE: {SessionState.STARTING, SessionState.TRADING, SessionState.HALTED},
    SessionState.STARTING: {SessionState.READY, SessionState.TRADING, SessionState.HALTED},
    SessionState.READY: {SessionState.TRADING, SessionState.HALTED},
    SessionState.TRADING: {
        SessionState.PAUSED,
        SessionState.CLOSEOUT,
        SessionState.RECONCILIATION,
        SessionState.HALTED,
        SessionState.COMPLETED,
    },
    SessionState.PAUSED: {SessionState.TRADING, SessionState.CLOSEOUT, SessionState.HALTED},
    SessionState.CLOSEOUT: {SessionState.RECONCILIATION, SessionState.COMPLETED, SessionState.HALTED},
    SessionState.RECONCILIATION: {SessionState.COMPLETED, SessionState.HALTED, SessionState.TRADING},
    SessionState.COMPLETED: set(),
    SessionState.HALTED: set(),
}


class SessionManager:
    def __init__(self, config, closeout) -> None:
        self.config = config
        self.closeout = closeout
        self.state = SessionState.OFFLINE
        self.session_id: str | None = None
        self.started_at: datetime | None = None
        self.ends_at: datetime | None = None
        self.entries_allowed = False
        self.closeout_started = False
        self.is_flat = True
        self.note: str | None = None
        self.counters = {"signals": 0, "orders": 0, "rejections": 0, "trades": 0}
        self.history: list[dict] = []
        self.entry_cutoff_announced = False

    # -- lifecycle ----------------------------------------------------------
    def _transition(self, new_state: SessionState, *, reason: str = "", now: datetime | None = None) -> bool:
        """Validate and record a state transition. Invalid => fail closed."""
        if new_state is self.state:
            return True
        allowed = _ALLOWED_TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            previous = self.state
            self.state = SessionState.HALTED
            self.entries_allowed = False
            self.note = f"invalid_transition:{previous.value}->{new_state.value}"
            self.history.append(
                {
                    "timestamp": (now or utcnow()).isoformat(),
                    "previous_state": previous.value,
                    "new_state": SessionState.HALTED.value,
                    "reason": f"invalid_transition:{previous.value}->{new_state.value}",
                }
            )
            return False
        previous = self.state
        self.state = new_state
        self.history.append(
            {
                "timestamp": (now or utcnow()).isoformat(),
                "previous_state": previous.value,
                "new_state": new_state.value,
                "reason": reason,
            }
        )
        return True

    def start(self, now: datetime) -> str:
        self.session_id = new_session_id()
        self.started_at = now
        self.ends_at = self._at(now, self.config.end_time)
        if self.ends_at is not None and self.ends_at <= now:
            self.ends_at = self.ends_at + timedelta(days=1)
        self._transition(SessionState.TRADING, reason="session_start", now=now)
        self.entries_allowed = True
        self.closeout_started = False
        self.is_flat = True
        self.note = None
        return self.session_id

    def update(self, now: datetime) -> SessionState:
        """Advance the session clock; return the resulting state."""
        if self.state in (SessionState.OFFLINE, SessionState.COMPLETED, SessionState.HALTED, SessionState.PAUSED):
            return self.state

        if self._within_session(now):
            cutoff = self._at(now, self.config.entry_cutoff_time)
            stop_before = self._closeout_stop_time(now)
            limits = [t for t in (cutoff, stop_before) if t is not None]
            entry_limit = min(limits) if limits else None
            self.entries_allowed = entry_limit is None or now <= entry_limit
            # D.1.4: explicit one-shot announcement when the cutoff is crossed.
            if cutoff is not None and now >= cutoff and not self.entry_cutoff_announced:
                self.entry_cutoff_announced = True
                self.history.append(
                    {"timestamp": now.isoformat(), "event": "ENTRY_CUTOFF_REACHED", "reason": "cutoff_crossed"}
                )
        else:
            self.entries_allowed = False

        deadline = self._at(now, self.config.flatten_deadline_time)
        if self.closeout.enabled and deadline is not None and now >= deadline:
            self.entries_allowed = False
            if self.state is not SessionState.CLOSEOUT:
                self._transition(SessionState.CLOSEOUT, reason="flatten_deadline_reached", now=now)
                self.closeout_started = True
        return self.state

    def begin_closeout(self, now: datetime) -> None:
        self._transition(SessionState.CLOSEOUT, reason="begin_closeout", now=now)
        self.closeout_started = True
        self.entries_allowed = False

    def pause(self, reason: str = "paused") -> None:
        if self.state is SessionState.TRADING:
            self._transition(SessionState.PAUSED, reason=reason)
            self.entries_allowed = False
            self.note = reason

    def resume(self) -> None:
        if self.state is SessionState.PAUSED:
            self._transition(SessionState.TRADING, reason="resume")
            self.note = None

    def halt(self, reason: str) -> None:
        self._transition(SessionState.HALTED, reason=reason)
        self.entries_allowed = False
        self.note = reason

    def start_reconciliation(self) -> None:
        self._transition(SessionState.RECONCILIATION, reason="reconciliation_started")

    def mark_flat(self, *, success: bool, note: str | None = None) -> None:
        self.is_flat = success
        self.note = note
        if success:
            self._transition(SessionState.COMPLETED, reason=note or "flat_verified")
        else:
            self._transition(SessionState.HALTED, reason=note or "flatten_failed")
            self.note = note or "flatten_failed"

    def count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    # -- helpers ------------------------------------------------------------
    def _tz(self):
        try:
            return ZoneInfo(self.config.timezone)
        except Exception:  # noqa: BLE001 - fall back to UTC on bad tz config
            return timezone.utc

    def _at(self, now: datetime, value: time) -> datetime | None:
        if value is None:
            return None
        return datetime(now.year, now.month, now.day, value.hour, value.minute, tzinfo=self._tz())

    def _closeout_stop_time(self, now: datetime) -> datetime | None:
        if not self.closeout.enabled:
            return None
        end = self._at(now, self.config.end_time)
        if end is None:
            return None
        return end - timedelta(minutes=self.closeout.stop_new_entries_before_end_minutes)

    def _within_session(self, now: datetime) -> bool:
        start = self._at(now, self.config.start_time)
        end = self._at(now, self.config.end_time)
        if start is None or end is None:
            return True
        if end < start:
            return now >= start or now <= end
        return start <= now <= end

    def snapshot(self, now: datetime) -> SessionSnapshot:
        seconds_remaining = None
        if self.ends_at is not None:
            seconds_remaining = max(0.0, (self.ends_at - now).total_seconds())
        return SessionSnapshot(
            session_id=self.session_id or "none",
            state=self.state,
            started_at=self.started_at,
            ends_at=self.ends_at,
            timezone=self.config.timezone,
            entry_cutoff=self.config.entry_cutoff_time,
            flatten_deadline=self.config.flatten_deadline_time,
            entries_allowed=self.entries_allowed,
            closeout_started=self.closeout_started,
            is_flat=self.is_flat,
            seconds_remaining=seconds_remaining,
            note=self.note,
        )

    def build_summary(self, now: datetime, *, realized: float, fees: float) -> SessionSummary:
        return SessionSummary(
            session_id=self.session_id or "none",
            started_at=self.started_at,
            ended_at=now,
            final_state=self.state,
            flattened=self.is_flat,
            trades=self.counters.get("trades", 0),
            orders=self.counters.get("orders", 0),
            signals=self.counters.get("signals", 0),
            rejections=self.counters.get("rejections", 0),
            realized_pnl=realized,
            fees=fees,
            net_pnl=realized - fees,
        )
