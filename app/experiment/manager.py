"""Controlled paper-experiment lifecycle (first smoke experiment).

Owns the authorization envelope, the supervision state machine, run-scoped
arming (execution + paper caps), automatic expiry, and kill-tripwire
evaluation. Read-only with respect to strategy/risk/accounting: it never
evaluates signals, never sizes orders, never touches fills. Arming mutates
ONLY the in-memory ``execution.enabled`` / ``paper_safety.enabled`` flags
(the yaml file is never written); expiry/disarm restores disabled.

States: PREPARING -> OBSERVING -> READY_FOR_AUTHORIZATION -> AUTHORIZED ->
ARMED -> RUNNING -> COMPLETED, with HALTED/FLATTENING/FAILED as safety
terminals. Transitions are append-only in the audit history.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from app.config.models import AppConfig
from app.core.clock import utcnow


class ExperimentState(str, Enum):
    PREPARING = "preparing"
    OBSERVING = "observing"
    READY_FOR_AUTHORIZATION = "ready_for_authorization"
    AUTHORIZED = "authorized"
    ARMED = "armed"
    RUNNING = "running"
    HALTED = "halted"
    FLATTENING = "flattening"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ExperimentEnvelope:
    """Immutable authorization envelope. Frozen at authorize() time."""

    run_id: str
    config_hash: str
    strategy_name: str
    strategy_version: str
    symbol: str
    broker: str
    trading_mode: str
    window_start: str
    window_end: str
    window_minutes: int
    caps: dict
    ai_read_only: bool
    operator: str
    authorized_at: str


class ExperimentError(Exception):
    """Authorization/transition refusal (auditable, never silent)."""


class ExperimentManager:
    """Experiment lifecycle. Holds an AppConfig reference for run-scoped arming."""

    MIN_WINDOW_MINUTES = 5
    MAX_WINDOW_MINUTES = 240

    def __init__(self, config: AppConfig, *, run_id: str) -> None:
        self.config = config
        self.run_id = run_id
        self.state = ExperimentState.PREPARING
        self.envelope: ExperimentEnvelope | None = None
        self.history: list[dict] = []
        self.kill_reason: str | None = None
        self._record("init", {"state": self.state.value})

    # -- introspection ------------------------------------------------------------
    def snapshot(self, *, now: datetime | None = None) -> dict:
        moment = now or utcnow()
        remaining = None
        if self.envelope is not None and self.state in (
            ExperimentState.AUTHORIZED, ExperimentState.ARMED, ExperimentState.RUNNING,
        ):
            end = datetime.fromisoformat(self.envelope.window_end)
            remaining = max(0.0, (end - moment).total_seconds())
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "envelope": None if self.envelope is None else {
                "run_id": self.envelope.run_id,
                "config_hash": self.envelope.config_hash,
                "strategy": f"{self.envelope.strategy_name} v{self.envelope.strategy_version}",
                "symbol": self.envelope.symbol,
                "broker": self.envelope.broker,
                "trading_mode": self.envelope.trading_mode,
                "window_start": self.envelope.window_start,
                "window_end": self.envelope.window_end,
                "window_minutes": self.envelope.window_minutes,
                "caps": self.envelope.caps,
                "ai_read_only": self.envelope.ai_read_only,
                "operator": self.envelope.operator,
                "authorized_at": self.envelope.authorized_at,
            },
            "seconds_remaining": remaining,
            "execution_enabled": bool(self.config.execution.enabled),
            "kill_reason": self.kill_reason,
            "transitions": len(self.history),
        }

    # -- lifecycle ---------------------------------------------------------------------
    def _record(self, event: str, detail: dict) -> None:
        self.history.append({"at": utcnow().isoformat(), "event": event,
                             "state": self.state.value, **detail})

    def _move(self, new_state: ExperimentState, event: str, **detail) -> None:
        self.state = new_state
        self._record(event, detail)

    def mark_observing(self) -> None:
        if self.state is ExperimentState.PREPARING:
            self._move(ExperimentState.OBSERVING, "observing")

    def mark_ready_for_authorization(self) -> None:
        if self.state is ExperimentState.OBSERVING:
            self._move(ExperimentState.READY_FOR_AUTHORIZATION, "ready_for_authorization")

    def authorize(self, *, operator: str, window_minutes: int) -> ExperimentEnvelope:
        """Freeze the envelope. Validates everything; refuses loudly."""
        if self.state not in (ExperimentState.OBSERVING, ExperimentState.READY_FOR_AUTHORIZATION):
            raise ExperimentError(f"authorize refused in state {self.state.value}")
        if not operator or not operator.strip():
            raise ExperimentError("authorize refused: operator identity required")
        if not (self.MIN_WINDOW_MINUTES <= window_minutes <= self.MAX_WINDOW_MINUTES):
            raise ExperimentError(
                f"authorize refused: window {window_minutes} outside "
                f"[{self.MIN_WINDOW_MINUTES},{self.MAX_WINDOW_MINUTES}] minutes")
        if self.config.trading.mode != "paper":
            raise ExperimentError("authorize refused: trading.mode is not paper")
        if self.config.execution.enabled:
            raise ExperimentError("authorize refused: execution already enabled")
        caps = self.config.paper_safety
        # Values are validated now; the enabled flag is set run-scoped at
        # arm() time so the repository default stays disabled.
        for name in ("max_qty_per_order", "max_open_qty", "max_notional_exposure"):
            value = getattr(caps, name)
            if not (isinstance(value, (int, float)) and value > 0):
                raise ExperimentError(f"authorize refused: invalid cap {name}")
        orders = caps.max_orders_per_session
        if not isinstance(orders, int) or isinstance(orders, bool) or orders < 1:
            raise ExperimentError("authorize refused: invalid cap max_orders_per_session")
        start = utcnow()
        end = start + timedelta(minutes=window_minutes)
        envelope = ExperimentEnvelope(
            run_id=self.run_id,
            config_hash=self._config_hash(),
            strategy_name=self.config.strategy.name,
            strategy_version=self.config.strategy.version,
            symbol=self.config.trading.symbol,
            broker=self.config.trading.broker,
            trading_mode=self.config.trading.mode,
            window_start=start.isoformat(),
            window_end=end.isoformat(),
            window_minutes=window_minutes,
            caps={"max_qty_per_order": caps.max_qty_per_order,
                  "max_open_qty": caps.max_open_qty,
                  "max_orders_per_session": caps.max_orders_per_session,
                  "max_notional_exposure": caps.max_notional_exposure},
            ai_read_only=bool(self.config.ai.permissions.read_only),
            operator=operator.strip(),
            authorized_at=start.isoformat(),
        )
        self.envelope = envelope
        self._move(ExperimentState.AUTHORIZED, "authorized",
                   operator=envelope.operator, window_minutes=window_minutes)
        return envelope

    def arm(self) -> None:
        """Run-scoped arming (in-memory flags only; yaml never written)."""
        if self.state is not ExperimentState.AUTHORIZED:
            raise ExperimentError(f"arm refused in state {self.state.value}")
        if self._expired(utcnow()):
            raise ExperimentError("arm refused: authorization window already elapsed")
        self.config.execution.enabled = True
        self.config.paper_safety.enabled = True
        self._move(ExperimentState.ARMED, "armed")

    def mark_running(self) -> None:
        if self.state is ExperimentState.ARMED:
            self._move(ExperimentState.RUNNING, "running")

    def disarm(self, *, reason: str) -> None:
        """Return execution AND caps to disabled. Never touches the yaml file."""
        self.config.execution.enabled = False
        self.config.paper_safety.enabled = False
        self._record("disarmed", {"reason": reason})

    def check_expiry(self, now: datetime) -> bool:
        """True when an armed/running window elapsed (caller ends the run)."""
        if self.envelope is None:
            return False
        if self.state not in (ExperimentState.AUTHORIZED, ExperimentState.ARMED,
                              ExperimentState.RUNNING):
            return False
        return self._expired(now)

    def complete(self) -> None:
        self.disarm(reason="window_complete")
        self._move(ExperimentState.COMPLETED, "completed")

    def halt(self, reason: str) -> None:
        """Safety halt: disarm first, then terminal state. No auto re-arm."""
        self.disarm(reason=f"halt:{reason}")
        self.kill_reason = reason
        self._move(ExperimentState.HALTED, "halted", reason=reason)

    def fail(self, reason: str) -> None:
        self.disarm(reason=f"fail:{reason}")
        self.kill_reason = reason
        self._move(ExperimentState.FAILED, "failed", reason=reason)

    # -- kill tripwires (read-only evaluation over caller-built snapshots) ----
    def evaluate_tripwires(self, snapshot: dict) -> str | None:
        """Return a kill reason, or None when all clear. No mutation."""
        if snapshot.get("paper_mode") is False:
            return "paper_live_mismatch"
        if snapshot.get("cap_breach"):
            return f"cap_breach:{snapshot['cap_breach']}"
        if snapshot.get("broker_unverifiable"):
            return "broker_unverifiable"
        if snapshot.get("reconciliation_ok") is False and (
                snapshot.get("has_exposure") or snapshot.get("open_orders", 0) > 0):
            return "unreconciled_with_exposure"
        if snapshot.get("stale_entry_attempted"):
            return "stale_data_entry"
        if snapshot.get("duplicate_order"):
            return "duplicate_order"
        if snapshot.get("ambiguous_unresolved"):
            return "ambiguous_unresolved"
        return None

    # -- internals ----------------------------------------------------------------------
    def _expired(self, now: datetime) -> bool:
        if self.envelope is None:
            return False
        return now >= datetime.fromisoformat(self.envelope.window_end)

    def _config_hash(self) -> str:
        payload = self.config.model_dump_json()
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
