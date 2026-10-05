p='app/runner/engine.py'
s=open(p,encoding='utf-8').read()

old='context_timeframe=(self._timeframe_selection.context_timeframe if self._timeframe_selection and not self._timeframe_selection.blocked else self.config.timeframes.context),\n            signal_timeframe=signal_tf,\n            execution_timeframe=(self._timeframe_selection.execution_timeframe if self._timeframe_selection and not self._timeframe_selection.blocked else self.config.timeframes.execution),'
new='context_timeframe=(self._timeframe_selection.context_timeframe if self._timeframe_selection and not self._timeframe_selection.blocked else self.config.timeframes.context),\n            signal_timeframe=(self._timeframe_selection.signal_timeframe if self._timeframe_selection else self.config.timeframes.signal),\n            execution_timeframe=(self._timeframe_selection.execution_timeframe if self._timeframe_selection and not self._timeframe_selection.blocked else self.config.timeframes.execution),'
assert old in s, 'sel not found'
s=s.replace(old, new)
open(p,'w',encoding='utf-8').write(s)
print('fixed')
