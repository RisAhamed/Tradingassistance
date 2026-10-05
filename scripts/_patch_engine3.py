p='app/runner/engine.py'
s=open(p,encoding='utf-8').read()

# fix 1: selection -> self._timeframe_selection in _handle_signal
old_sel='context_timeframe=selection.context_timeframe if not selection.blocked else self.config.timeframes.context,\n            signal_timeframe=signal_tf,\n            execution_timeframe=selection.execution_timeframe if not selection.blocked else self.config.timeframes.execution,'
new_sel='context_timeframe=(self._timeframe_selection.context_timeframe if self._timeframe_selection and not self._timeframe_selection.blocked else self.config.timeframes.context),\n            signal_timeframe=signal_tf,\n            execution_timeframe=(self._timeframe_selection.execution_timeframe if self._timeframe_selection and not self._timeframe_selection.blocked else self.config.timeframes.execution),'
assert old_sel in s, 'sel not found'
s=s.replace(old_sel, new_sel)

# fix 2: remove async emit from sync _build_risk_context
old2='        fresh = not bar_result.is_stale if fp.stale_action() == "block_entries" else True\n        if not self._data_gap_ok:\n            fresh = False\n        self._emit_freshness(now, {"bar": bar_result})'
new2='        fresh = not bar_result.is_stale if fp.stale_action() == "block_entries" else True\n        if not self._data_gap_ok:\n            fresh = False'
assert old2 in s, 'freshness emit not found'
s=s.replace(old2, new2)

open(p,'w',encoding='utf-8').write(s)
print('fixed')
