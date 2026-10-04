"""Health registry backing /health, /health/ready and /health/live."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.core.clock import utcnow
from app.domain.enums import HealthState


@dataclass(slots=True)
class CheckResult:
    name: str
    healthy: bool
    detail: str = ""
    critical: bool = True
    checked_at: datetime = field(default_factory=utcnow)


class HealthRegistry:
    """Register named, side-effect-free health checks."""

    def __init__(self) -> None:
        self._checks: dict[str, tuple[Callable[[], bool], str, bool]] = {}

    def register(
        self,
        name: str,
        check: Callable[[], bool],
        *,
        detail: str = "",
        critical: bool = True,
    ) -> None:
        self._checks[name] = (check, detail, critical)

    def run(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        for name, (check, detail, critical) in self._checks.items():
            try:
                healthy = bool(check())
                results.append(CheckResult(name, healthy, detail, critical))
            except Exception as exc:  # noqa: BLE001 - health must never raise
                results.append(CheckResult(name, False, f"check_error: {exc}", critical))
        return results

    def overall(self) -> HealthState:
        results = self.run()
        if not results:
            return HealthState.UNKNOWN
        if any(not r.healthy for r in results if r.critical):
            return HealthState.ERROR
        if any(not r.healthy for r in results):
            return HealthState.WARNING
        return HealthState.HEALTHY

    def ready(self) -> bool:
        return all(r.healthy for r in self.run() if r.critical)

    def report(self) -> dict:
        results = self.run()
        overall = self.overall().value
        return {
            "status": overall,
            "ready": self.ready(),
            "checks": [
                {
                    "name": r.name,
                    "healthy": r.healthy,
                    "critical": r.critical,
                    "detail": r.detail,
                    "checked_at": r.checked_at.isoformat(),
                }
                for r in results
            ],
        }
