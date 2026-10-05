p='app/runner/engine.py'
s=open(p,encoding='utf-8').read()

old='freshness_stale=bar_fresh.is_stale,'
new='freshness_stale=self.freshness_policy.evaluate(source="bar", last_received=self._last_bar_at, now=now).is_stale,'
assert old in s, 'bar_fresh not found'
s=s.replace(old, new)
open(p,'w',encoding='utf-8').write(s)
print('fixed')
