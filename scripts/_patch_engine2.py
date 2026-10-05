"""Patch engine.py — Phase D evaluate() + _handle_signal() + payload()."""
p = 'app/runner/engine.py'
s = open(p, encoding='utf-8').read()

# 1. Evaluate(): add freshness + timeframe selection at the start
old_eval = '''    async def evaluate(self, now: datetime | None = None) -> StrategySignal | None:
        """Run features -> regime -> strategy for the signal timeframe."""
        now = now or utcnow()
        features = self._features_for(self.config.timeframes.signal, now)'''
new_eval = '''    async def evaluate(self, now: datetime | None = None) -> StrategySignal | None:
        """Run features -> regime -> strategy for the signal timeframe."""
        now = now or utcnow()
        # Phase D: cadence-aware freshness evaluation (every evaluation cycle)
        fp = self.freshness_policy
        bar_expected = timeframe_minutes(self.config.market_data.bars.timeframe) * 60
        bar_fresh = fp.evaluate(source="bar", last_received=self._last_bar_at, now=now, expected_interval_seconds=bar_expected)
        await self._emit_freshness(now, {"bar": bar_fresh})
        # Phase D: dynamic timeframe selection
        regime_for_select = self.state.regime
        if regime_for_select is None:
            regime_for_select = self.regime_engine.classify(self.state.features or self._features_for(self.config.timeframes.context, now), now=now)
        selection = self.timeframe_selector.select(
            regime=regime_for_select,
            features=self.state.features or self._features_for(self.config.timeframes.signal, now),
            snapshot=self.state.latest_snapshot,
            now=now,
        )
        self._timeframe_selection = selection
        await self._emit_timeframe_selection(selection)
        # Use selected timeframes when available; fall back to config.
        signal_tf = selection.signal_timeframe if not selection.blocked else self.config.timeframes.signal
        context_tf = selection.context_timeframe if not selection.blocked else self.config.timeframes.context
        features = self._features_for(signal_tf, now)'''
assert old_eval in s
s = s.replace(old_eval, new_eval)

# 2. _handle_signal(): build TradePlan after risk approval, before submit
old_hs = '''        logger.info(
            "RISK CHECK PASSED",
            extra={"structured": {"event": "RISK_APPROVED", "component": "risk", "signal_id": signal.signal_id}},
        )
        await self.bus.emit(
            EventType.RISK_APPROVED,
            payload={"signal_id": signal.signal_id, "risk_id": decision.risk_id},
            correlation_id=signal.correlation_id,
        )
        await self._submit_entry(signal)'''
new_hs = '''        logger.info(
            "RISK CHECK PASSED",
            extra={"structured": {"event": "RISK_APPROVED", "component": "risk", "signal_id": signal.signal_id}},
        )
        await self.bus.emit(
            EventType.RISK_APPROVED,
            payload={"signal_id": signal.signal_id, "risk_id": decision.risk_id},
            correlation_id=signal.correlation_id,
        )
        # Phase D: TradePlan
        tp_result = self.trade_plan_builder.build_from_signal(
            signal,
            regime=self.state.regime or regime_for_select,
            features=self.state.features or features,
            snapshot=self.state.latest_snapshot,
            context_timeframe=selection.context_timeframe if not selection.blocked else self.config.timeframes.context,
            signal_timeframe=signal_tf,
            execution_timeframe=selection.execution_timeframe if not selection.blocked else self.config.timeframes.execution,
            freshness_stale=bar_fresh.is_stale,
            correlation_id=signal.correlation_id,
            session_id=signal.session_id,
        )
        self._trade_plan = tp_result.plan
        for ev in tp_result.events:
            await self.bus.emit(getattr(EventType, ev["event"]), payload=ev)
        await self._evaluate_entry_conditions(tp_result.plan)
        await self._update_holding_duration(tp_result.plan)
        await self._submit_entry(signal)'''
assert old_hs in s
s = s.replace(old_hs, new_hs)

open(p, 'w', encoding='utf-8').write(s)
print('engine patched step2 OK')
