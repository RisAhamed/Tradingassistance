"""F.1: orchestrator must use FULL StrategyConfig from AppConfig.strategy."""
from app.config.loader import get_env, load_config
from app.config.models import StrategyConfig, StrategySelectionConfig
from app.domain.enums import Regime
from app.strategies.orchestrator import StrategyOrchestrator


def _build(name: str, selection: StrategySelectionConfig, strategy_config: StrategyConfig):
    # config is FULL; requested name must be provisioned via selection map/fallback
    orch = StrategyOrchestrator(selection, strategy_config=strategy_config)
    return orch.strategy_for(name), orch


def test_breakout_buffer_percent_propagates():
    config = load_config(env=get_env())
    config.strategy.breakout.buffer_percent = 0.10
    sel = StrategySelectionConfig(mode="manual", manual_name="breakout_momentum", fallback_name="breakout_momentum", map={})
    orch = StrategyOrchestrator(sel, strategy_config=config.strategy)
    s = orch.strategy_for("breakout_momentum")
    assert s.config.breakout.buffer_percent == 0.10


def test_mean_reversion_stretch_atr_min_propagates():
    config = load_config(env=get_env())
    config.strategy.mean_reversion.stretch_atr_min = 2.5
    sel = StrategySelectionConfig(mode="manual", manual_name="vwap_reversion", fallback_name="vwap_reversion", map={})
    orch = StrategyOrchestrator(sel, strategy_config=config.strategy)
    s = orch.strategy_for("vwap_reversion")
    assert s.config.mean_reversion.stretch_atr_min == 2.5


def test_momentum_rsi_min_long_propagates():
    config = load_config(env=get_env())
    config.strategy.momentum.rsi_min_long = 60
    sel = StrategySelectionConfig(mode="manual", manual_name="breakout_momentum", fallback_name="breakout_momentum", map={})
    orch = StrategyOrchestrator(sel, strategy_config=config.strategy)
    s = orch.strategy_for("breakout_momentum")
    assert s.config.momentum.rsi_min_long == 60


def test_cached_instance_keeps_yaml_values():
    config = load_config(env=get_env())
    config.strategy.breakout.buffer_percent = 0.10
    sel = StrategySelectionConfig(mode="regime_aware", manual_name="breakout_momentum", fallback_name="breakout_momentum", map={"trending_bullish": "breakout_momentum"})
    orch = StrategyOrchestrator(sel, strategy_config=config.strategy)
    a = orch.resolve(Regime.TRENDING_BULLISH)
    b = orch.resolve(Regime.TRENDING_BULLISH)
    assert a is b
    assert a.config.breakout.buffer_percent == 0.10


def test_distinct_names_distinct_instances():
    config = load_config(env=get_env())
    config.strategy.breakout.buffer_percent = 0.10
    config.strategy.mean_reversion.stretch_atr_min = 2.5
    sel = StrategySelectionConfig(mode="regime_aware", manual_name="breakout_momentum", fallback_name="vwap_reversion", map={"trending_bullish": "breakout_momentum", "range_bound": "vwap_reversion"})
    orch = StrategyOrchestrator(sel, strategy_config=config.strategy)
    a = orch.resolve(Regime.TRENDING_BULLISH)
    b = orch.resolve(Regime.RANGE_BOUND)
    assert a is not b
    assert a.config.breakout.buffer_percent == 0.10
    assert b.config.mean_reversion.stretch_atr_min == 2.5
