"""Time helpers: UTC clock, timeframe parsing, and a testable clock seam."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

_TIMEFRAME_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(tz=timezone.utc)


def parse_timeframe(value: str) -> timedelta:
    """Parse a timeframe such as ``15m`` / ``1h`` / ``1d`` into a timedelta."""
    match = _TIMEFRAME_RE.match(value.strip().lower())
    if not match:
        raise ValueError(f"invalid timeframe '{value}' (expected e.g. 1m, 5m, 15m, 1h, 1d)")
    amount = int(match.group("value"))
    unit = match.group("unit")
    return timedelta(seconds=amount * _UNIT_SECONDS[unit])


def timeframe_seconds(value: str) -> int:
    return int(parse_timeframe(value).total_seconds())


def floor_to_timeframe(moment: datetime, timeframe: str) -> datetime:
    """Floor a timestamp to the start of its timeframe bucket (UTC)."""
    seconds = timeframe_seconds(timeframe)
    epoch = int(moment.timestamp())
    floored = epoch - (epoch % seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


@runtime_checkable
class Clock(Protocol):
    """A minimal clock seam so tests can inject deterministic time."""

    def now(self) -> datetime: ...


class SystemClock:
    """Default clock backed by the wall clock."""

    def now(self) -> datetime:
        return utcnow()


class FrozenClock:
    """A controllable clock for deterministic tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or utcnow()

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, moment: datetime) -> None:
        self._now = moment
