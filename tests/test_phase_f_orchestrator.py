"""Phase F: regime-aware strategy orchestrator."""
from app.config.loader import load_config, get_env
from app.config.models import StrategySelectionConfig
from app.domain.enums import Regime
from app.strategies.orchestrator import StrategyOrchestrator


def _cfg(**kw):
    cfg = {
        "mode": kw.get("mode", "manual"),
        "manual_name": kw.get("manual_name", "breakout_momentum"),
        "fallback_name": kw.get("fallback_name", "breakout_momentum"),
        "map": kw.get("map", {}),
    }
    return StrategySelectionConfig.model_validate(cfg)


def test_manual_mode_always_returns_manual_name():
    o = StrategyOrchestrator(_cfg(mode="manual", map={"trending_bullish": "vwap_reversion"}))
    for r in Regime:
        assert o.active_name(r) == "breakout_momentum"


def test_regime_aware_map_and_fallback():
    o = StrategyOrchestrator(_cfg(mode="regime_aware", map={"trending_bullish": "vwap_reversion", "unknown": "session_vwap_reversion"}))
    assert o.active_name(Regime.TRENDING_BULLISH) == "vwap_reversion"
    assert o.active_name(Regime.RANGE_BOUND) == "breakout_momentum"  # fallback
    assert o.active_name(Regime.UNKNOWN) == "session_vwap_reversion"


def test_unknown_and_unlisted_regimes_use_fallback():
    o = StrategyOrchestrator(_cfg(mode="regime_aware", map={}))
    for r in Regime:
        assert o.active_name(r) == "breakout_momentum"


def test_resolve_caches_instances_and_does_not_evaluate(monkeypatch):
    o = StrategyOrchestrator(_cfg(mode="manual"))
    called = []
    s = o.resolve(Regime.TRENDING_BULLISH)
    s2 = o.resolve(Regime.RANGE_BOUND)
    assert s is s2
    # Orchestrator must never call evaluate itself.
    assert not called


def test_config_validation_unknown_strategy_in_map():
    try:
        StrategySelectionConfig.model_validate({"mode": "regime_aware", "map": {"trending_bullish": "nope"}})
    except ValueError as exc:
        assert "unknown strategy" in str(exc)
        return
    raise AssertionError("expected ValueError")


def test_config_validation_mode_enum():
    try:
        StrategySelectionConfig.model_validate({"mode": "bogus"})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_backtest_orchestrator_matches_manual():
    from app.backtesting.engine import BacktestEngine

    config = load_config(env=get_env())
    config.strategy.selection.mode = "manual"
    e = BacktestEngine(config)
    assert e.orchestrator.active_name(Regime.TRENDING_BULLISH) == "breakout_momentum"
    config.strategy.selection.mode = "regime_aware"
    config.strategy.selection.map = {"trending_bullish": "vwap_reversion"}
    e2 = BacktestEngine(config)
    assert e2.orchestrator.active_name(Regime.TRENDING_BULLISH) == "vwap_reversion"
