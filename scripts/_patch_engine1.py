#!/usr/bin/env python
"""Patch engine.py — Phase D integration."""
p = 'app/runner/engine.py'
s = open(p, encoding='utf-8').read()

# 1. Replace _build_risk_context freshness block
old = '''    def _build_risk_context(self, now: datetime) -> RiskContext:
        age = self.store.data_age(self.symbol, now)
        # Risk POLICY (fail-closed): the blocking threshold is a risk setting, and
        # `market_data.freshness.stale_action` decides whether losing freshness
        # actually blocks entries or only warns.
        freshness = self.config.market_data.freshness
        if self.config.market_data.bars.enabled and self._last_bar_at is not None:
            # Phase C1: entries are gated on BAR freshness (canonical source), not
            # on sparse trade arrival.
            bar_age = (now - self._last_bar_at).total_seconds()
            measured = bar_age <= self.config.risk.stale_market_data.maximum_age_seconds
        else:
            age = self.store.data_age(self.symbol, now)
            measured = age is not None and age <= self.config.risk.stale_market_data.maximum_age_seconds
        fresh = measured if freshness.stale_action == "block_entries" else True
        if not self._data_gap_ok:
            fresh = False'''
new = '''    def _build_risk_context(self, now: datetime) -> RiskContext:
        # Risk POLICY (fail-closed): cadence-aware freshness. The effective
        # bar threshold is max(configured_maximum_age, expected_interval +
        # grace_period) so a healthy 60 s bar stream is never falsely stale.
        fp = self.freshness_policy
        bar_expected = timeframe_minutes(self.config.market_data.bars.timeframe) * 60
        bar_result = fp.evaluate(
            source="bar",
            last_received=self._last_bar_at,
            now=now,
            expected_interval_seconds=bar_expected,
        )
        fresh = not bar_result.is_stale if fp.stale_action() == "block_entries" else True
        if not self._data_gap_ok:
            fresh = False
        self._emit_freshness(now, {"bar": bar_result})'''
assert old in s, 'risk_context old not found'
s = s.replace(old, new)

# 2. Add helper methods before payload()
old2 = '''    def payload(self) -> dict:'''
new2 = '''    async def _emit_freshness(self, now: datetime, results: dict) -> None:
        \"\"\"Publish FRESHNESS_EVALUATED / FRESHNESS_CHANGED events.\"\"\"
        summary = {}
        for src, r in results.items():
            summary[src] = {
                "age_seconds": r.age_seconds,
                "effective_threshold_seconds": r.effective_threshold_seconds,
                "is_stale": r.is_stale,
                "reason": r.reason,
            }
        await self.bus.emit(EventType.FRESHNESS_EVALUATED, payload=summary)
        if self._last_freshness != summary:
            self._last_freshness = summary
            await self.bus.emit(EventType.FRESHNESS_CHANGED, payload=summary)

    async def _emit_timeframe_selection(self, selection: TimeframeSelection) -> None:
        if selection.blocked:
            await self.bus.emit(
                EventType.TIMEFRAME_SELECTION_BLOCKED,
                payload={"reason": selection.reason, "inputs": selection.inputs},
            )
            logger.info(
                "TIMEFRAME SELECTION BLOCKED",
                extra={"structured": {"event": "TIMEFRAME_SELECTION_BLOCKED", "component": "engine", "reason": selection.reason}},
            )
        else:
            await self.bus.emit(
                EventType.TIMEFRAME_SELECTED,
                payload={
                    "context_timeframe": selection.context_timeframe,
                    "signal_timeframe": selection.signal_timeframe,
                    "execution_timeframe": selection.execution_timeframe,
                    "reason": selection.reason,
                    "method": selection.method,
                },
            )
            logger.info(
                "TIMEFRAME SELECTED",
                extra={"structured": {"event": "TIMEFRAME_SELECTED", "component": "engine", "context": selection.context_timeframe, "signal": selection.signal_timeframe, "execution": selection.execution_timeframe, "reason": selection.reason}},
            )

    async def _emit_trade_plan_event(self, event: str, plan) -> None:
        await self.bus.emit(
            getattr(EventType, event),
            payload={"plan_id": plan.plan_id, "status": plan.status.value, "direction": str(plan.direction) if plan.direction else None},
        )

    async def _evaluate_entry_conditions(self, plan) -> None:
        for c in plan.entry_conditions:
            await self.bus.emit(
                EventType.ENTRY_CONDITION_EVALUATED,
                payload={"condition": c["name"], "met": c["met"], "detail": c.get("detail")},
            )

    async def _evaluate_exit_conditions(self, plan) -> None:
        for c in plan.exit_conditions:
            await self.bus.emit(
                EventType.EXIT_CONDITION_EVALUATED,
                payload={"condition": c["name"], "active": c["active"]},
            )

    async def _update_holding_duration(self, plan) -> None:
        await self.bus.emit(
            EventType.HOLDING_DURATION_UPDATED,
            payload={"expected_minutes": plan.expected_holding_minutes, "maximum_minutes": plan.maximum_holding_minutes},
        )

    def payload(self) -> dict:'''
assert old2 in s, 'payload old not found'
s = s.replace(old2, new2, 1)

open(p, 'w', encoding='utf-8').write(s)
print('engine patched step1 OK')
