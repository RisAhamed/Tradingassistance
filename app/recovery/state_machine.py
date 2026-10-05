"""Recovery state machine (Phase D.1.3).

Explicit, logged transitions between market-data health states. The machine
never restores a trade-allowed state on connection alone: readiness requires a
fresh state produced by validation.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from app.core.clock import utcnow


class RecoveryState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    RESYNCING = "RESYNCING"
    VALIDATING = "VALIDATING"
    READY = "READY"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    HALTED = "HALTED"


# States in which new entries may be considered. Everything else fail-closes.
TRADE_ALLOWED_STATES = frozenset({RecoveryState.HEALTHY, RecoveryState.READY})


class RecoveryStateMachine:
    """Deterministic recovery state machine with a bounded history."""

    def __init__(self, *, max_history: int = 200) -> None:
        self.state: RecoveryState = RecoveryState.HEALTHY
        self.reason: str | None = None
        self.attempt: int = 0
        self.started_at: datetime | None = None
        self.updated_at: datetime = utcnow()
        self._history: list[dict] = []
        self._max_history = max_history
        self._record(RecoveryState.HEALTHY, reason="initial")

    def transition(
        self,
        new_state: RecoveryState,
        *,
        reason: str | None = None,
        attempt: int | None = None,
    ) -> RecoveryState:
        previous = self.state
        self.state = new_state
        self.reason = reason
        if attempt is not None:
            self.attempt = attempt
        now = utcnow()
        if new_state in (RecoveryState.RECOVERING, RecoveryState.RESYNCING) and previous in (
            RecoveryState.HEALTHY,
            RecoveryState.DEGRADED,
            RecoveryState.RECOVERY_FAILED,
            RecoveryState.HALTED,
        ):
            self.started_at = now
        self.updated_at = now
        self._record(new_state, previous=previous, reason=reason, attempt=self.attempt)
        return new_state

    def is_trade_allowed(self) -> bool:
        """True only when the machine has actually proven health/readiness."""
        return self.state in TRADE_ALLOWED_STATES

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "attempt": self.attempt,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "updated_at": self.updated_at.isoformat(),
            "trade_allowed": self.is_trade_allowed(),
        }

    def history(self, limit: int = 50) -> list[dict]:
        return list(self._history[-limit:])

    def _record(self, state: RecoveryState, *, previous: RecoveryState | None = None,
                reason: str | None = None, attempt: int | None = None) -> None:
        self._history.append(
            {
                "timestamp": utcnow().isoformat(),
                "state": state.value,
                "previous_state": previous.value if previous else None,
                "reason": reason,
                "attempt": attempt if attempt is not None else self.attempt,
            }
        )
        if len(self._history) > self._max_history:
            del self._history[: len(self._history) - self._max_history]
