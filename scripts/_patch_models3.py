p='app/config/models.py'
s=open(p,encoding='utf-8').read()

old = 'class TimeframeCandidateConfig(_Section):\n    """Candidate timeframes for one slot plus the data minimum required."""\n\n    timeframes: list[str] = Field(default_factory=list)\n    min_candles: int = 10'
new = 'class TimeframeCandidateConfig(_Section):\n    """Candidate timeframes for one slot plus the data minimum required."""\n\n    timeframes: list[str] = Field(default_factory=list)\n    min_candles: int = 10\n\n    @model_validator(mode="before")\n    @classmethod\n    def _accept_list(cls, v):\n        if isinstance(v, list):\n            return {"timeframes": v}\n        return v'
assert old in s, 'tcc not found'
s = s.replace(old, new)

old2 = '    storage: StorageConfig = Field(default_factory=StorageConfig)\n    testing: TestingConfig = Field(default_factory=TestingConfig)'
new2 = '    storage: StorageConfig = Field(default_factory=StorageConfig)\n    trade_plan: TradePlanConfig = Field(default_factory=TradePlanConfig)\n    testing: TestingConfig = Field(default_factory=TestingConfig)'
assert old2 in s, 'appcfg not found'
s = s.replace(old2, new2)

open(p,'w',encoding='utf-8').write(s)
print('models fixed')
