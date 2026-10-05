"""Soak / fault-injection / readiness tooling for verifying Phase C1 reliability.

These helpers are deliberately *observers and simulators*:

* :class:`SoakCollector` only counts events — it never influences trading.
* :class:`FaultInjectingProvider` wraps a real provider and injects deterministic
  faults locally. It never touches the Alpaca server.
* :func:`build_readiness_matrix` turns live engine state into an explicit,
  machine-checkable readiness table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.events.types import EventType

# Events the soak test counts. Anything unexpected is also counted.
_COUNTED = (
    EventType.BAR_RECEIVED,
    EventType.BAR_REJECTED,
    EventType.BAR_DUPLICATE,
    EventType.BAR_OUT_OF_ORDER,
    EventType.BAR_GAP_DETECTED,
    EventType.DATA_GAP,
    EventType.HISTORICAL_COVERAGE_CHECKED,
    EventType.HISTORICAL_COVERAGE_FAILED,
    EventType.CANDLE_COMPLETED,
    EventType.FEATURES_UPDATED,
    EventType.REGIME_CHANGED,
    EventType.STRATEGY_EVALUATED,
    EventType.SIGNAL_GENERATED,
    EventType.RISK_EVALUATED,
    EventType.RISK_APPROVED,
    EventType.RISK_REJECTED,
    EventType.ENTRY_BLOCKED,
    EventType.EXECUTION_BLOCKED,
    EventType.ORDER_CREATED,
    EventType.ORDER_SUBMITTED,
    EventType.ORDER_FILLED,
    EventType.RECOVERY_STARTED,
    EventType.RECOVERY_COMPLETED,
    EventType.RECOVERY_FAILED,
    EventType.SYSTEM_ERROR,
    EventType.ALPACA_CONNECTED,
    EventType.ALPACA_DISCONNECTED,
    EventType.ALPACA_RECONNECTED,
    EventType.READINESS_CHANGED,
)

# Events that must never occur while execution is disabled.
TRACKED_EVENT_NAMES = {event.value for event in _COUNTED}

FORBIDDEN_WHILE_DISABLED = (
    EventType.ORDER_CREATED,
    EventType.ORDER_SUBMITTED,
    EventType.ORDER_FILLED,
    EventType.ORDER_REJECTED,
)


@dataclass
class SoakResult:
    duration_seconds: float
    counts: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    passed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "duration_seconds": round(self.duration_seconds, 2),
            "counts": dict(sorted(self.counts.items())),
            "failures": list(self.failures),
            "passed": self.passed,
        }


class SoakCollector:
    """Counts pipeline events for the duration of a soak run."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.other = 0  # informational events outside the tracked set
        self._handler = None

    def attach(self, bus) -> None:
        def _handler(event) -> None:
            name = event.type.value
            self.counts[name] = self.counts.get(name, 0) + 1
            if name not in TRACKED_EVENT_NAMES:
                self.other += 1

        self._handler = _handler
        bus.subscribe_all(self._handler)

    def detach(self, bus) -> None:
        if self._handler is not None:
            bus.remove_all(self._handler)
            self._handler = None

    def get(self, event_type: EventType) -> int:
        return self.counts.get(event_type.value, 0)

    def verify(
        self,
        *,
        config,
        engine,
        duration_seconds: float,
        execution_disabled: bool,
    ) -> SoakResult:
        """Apply the configured soak policy to the collected counts."""
        soak = config.testing.soak
        failures: list[str] = []

        if soak.fail_on_unexpected_state:
            unexpected = self.get(EventType.SYSTEM_ERROR)
            if unexpected:
                failures.append(f"unexpected system errors: {unexpected}")

        gaps = self.get(EventType.BAR_GAP_DETECTED)
        if gaps:
            failures.append(f"bar gaps detected: {gaps}")

        missing_ok = gaps <= soak.maximum_missing_bars
        if not missing_ok:
            failures.append(f"gaps {gaps} exceed maximum_missing_bars {soak.maximum_missing_bars}")

        if execution_disabled:
            for event_type in FORBIDDEN_WHILE_DISABLED:
                if self.get(event_type):
                    failures.append(f"FORBIDDEN event while execution disabled: {event_type.value}")
        if engine.oms.all_orders():
            failures.append("orders exist while execution is disabled")
        if not engine.position_manager.is_flat:
            failures.append("position is not flat while execution is disabled")

        return SoakResult(
            duration_seconds=duration_seconds,
            counts=dict(self.counts),
            failures=failures,
            passed=not failures,
        )


@dataclass
class Row:
    area: str
    status: str
    evidence: str
    blocking: bool

    def as_dict(self) -> dict[str, Any]:
        return {"area": self.area, "status": self.status, "evidence": self.evidence, "blocking": self.blocking}


def build_readiness_matrix(engine) -> dict[str, Any]:
    """Turn live engine state into an explicit readiness table (JSON-serialisable)."""
    readiness = engine.readiness()
    streams = engine.stream_state()
    coverage = engine.coverage or {}
    warmup = engine.warmup
    counts = {tf: len(engine.store.candles(engine.symbol, tf)) for tf in engine.timeframes}
    execution_enabled = engine.execution_enabled
    rows: list[Row] = []

    def add(area: str, ok: bool, evidence: str, blocking: bool = False) -> None:
        rows.append(Row(area, "PASS" if ok else "FAIL", evidence, blocking))

    add("Market data", engine.provider.health().connected, f"provider={engine.provider.name}")
    add("1m bar subscription", streams["bars"]["last_at"] is not None, f"last_bar={streams['bars']['last_at']}")
    add("Quote stream", streams["quotes"]["last_at"] is not None, f"last_quote={streams['quotes']['last_at']}")
    add("Trade stream", streams["trades"]["last_at"] is not None, f"last_trade={streams['trades']['last_at']}")
    add("Bar freshness", bool(streams["bars"]["fresh"]), f"age={streams['bars']['age_seconds']}s")
    add("Historical warm-up", warmup.get("status") == "completed", f"status={warmup.get('status')}")
    add("Historical coverage", coverage.get("status") in (None, "PASS", "WARNING"),
        f"{coverage.get('received')}/{coverage.get('requested')}={coverage.get('coverage_percent')}% "
        f"gaps={coverage.get('gap_count')} largest={coverage.get('largest_gap')}")
    for timeframe in engine.timeframes:
        add(f"Candles {timeframe}", counts.get(timeframe, 0) > 0, f"count={counts.get(timeframe)}")
    add("Features ready", bool(readiness["features_ready"]), f"missing={engine.state.features.missing if engine.state.features else 'n/a'}")
    add("Regime classified", bool(readiness["regime_ready"]), f"regime={readiness['regime']}")
    add("Strategy ready", bool(readiness["strategy_ready"]), f"ready={readiness['strategy_ready']}")
    add("Data integrity", bool(readiness["data_integrity_ok"]), f"ok={readiness['data_integrity_ok']}")
    add("Recovery state", True, f"state={readiness['recovery_state']}")
    add("Execution disabled", not execution_enabled, f"execution={readiness['execution']}")
    add("Zero orders", not engine.oms.all_orders(), f"orders={len(engine.oms.all_orders())}")
    add("Position flat", engine.position_manager.is_flat, f"flat={engine.position_manager.is_flat}")

    return {
        "symbol": engine.symbol,
        "execution_enabled": execution_enabled,
        "readiness": readiness,
        "streams": streams,
        "coverage": coverage,
        "candles": counts,
        "rows": [row.as_dict() for row in rows],
        "failed": [row.area for row in rows if row.status == "FAIL"],
        "blocking": [row.area for row in rows if row.status == "FAIL" and row.blocking],
    }


def render_markdown(matrix: dict[str, Any]) -> str:
    """Human-readable rendering of the readiness matrix."""
    lines = ["| Area | Status | Evidence | Blocking? |", "|---|---|---|---|"]
    for row in matrix["rows"]:
        lines.append(
            f"| {row['area']} | {row['status']} | {row['evidence']} | {'YES' if row['blocking'] else 'NO'} |"
        )
    lines.append("")
    lines.append(f"**FAILED:** {', '.join(matrix['failed']) or 'none'}")
    return "\n".join(lines)


class FaultInjectingProvider:
    """Wraps a provider and injects deterministic faults LOCALLY.

    Never touches the Alpaca server. Faults are applied to the update stream only
    (drop / delay / duplicate / reorder / disconnect / silence), so recovery can be
    exercised deterministically in tests.
    """

    def __init__(self, inner, scenario: str = "none", **params: int) -> None:
        self.inner = inner
        self.name = inner.name
        self.scenario = scenario
        self.params = params
        self._handler = None
        self._delayed: list = []
        self._seen = 0
        self._previous = None
        self._block_left = 0
        self._block_done = False

    # -- provider interface -------------------------------------------------
    def set_handler(self, handler) -> None:
        self._handler = handler
        self.inner.set_handler(self._apply)

    async def connect(self, symbols) -> None:
        await self.inner.connect(symbols)

    async def disconnect(self) -> None:
        await self.inner.disconnect()

    def health(self):
        return self.inner.health()

    # -- fault logic --------------------------------------------------------
    async def _apply(self, update) -> None:
        from datetime import timedelta

        from app.domain.market import Candle

        scenario = self.scenario
        is_bar = isinstance(update, Candle)

        if scenario == "stale_data":
            return  # silence everything -> data goes stale
        if scenario == "disconnect":
            return  # simulate a dead stream
        if scenario == "delayed_bars" and is_bar:
            self._delayed.append(update)
            if len(self._delayed) <= self.params.get("delay_bars", 3):
                return
            for held in self._delayed:
                await self._emit(held)
            self._delayed.clear()
            return
        if scenario == "missing_bars" and is_bar:
            self._seen += 1
            size = max(2, self.params.get("missing_bars", 10))
            if self._block_left > 0:
                self._block_left -= 1
                return
            # Wait until the stream is running, then drop ONE contiguous block so a
            # real outage-sized gap is produced (isolated drops would be tolerated).
            if self._seen > size * 2 and not self._block_done:
                self._block_left = size - 1
                self._block_done = True
                return
        if scenario == "duplicate_bars" and is_bar:
            await self._emit(update)
            await self._emit(update)   # exact replay -> engine must reject
            self._previous = update
            return
        if scenario == "out_of_order_bars" and is_bar:
            if self._previous is not None:
                await self._emit(update)           # newer first
                await self._emit(self._previous)  # then older -> OOO
            self._previous = update
            return
        self._seen += 1
        await self._emit(update)

    async def _emit(self, update) -> None:
        if self._handler is not None:
            result = self._handler(update)
            if hasattr(result, "__await__"):
                await result