p='app/runner/engine.py'
s=open(p,encoding='utf-8').read()
old='data["timeframe_selection"] = (\n            self._timeframe_selection.model_dump(mode="json")\n            if self._timeframe_selection\n            else None\n        )'
new='data["timeframe_selection"] = (\n            dataclasses.asdict(self._timeframe_selection)\n            if self._timeframe_selection\n            else None\n        )'
assert old in s, 'ts not found'
s=s.replace(old,new)
open(p,'w',encoding='utf-8').write(s)
print('fixed')