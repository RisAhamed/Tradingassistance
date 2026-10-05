"""PHASE D.1.3: recovery state machine, fault injection, resync, entry blocking."""
from datetime import timedelta

from app.config.loader import get_env, load_config
from app.config.models import CoveragePolicyConfig
from app.core.clock import utcnow
from app.domain.market import Candle, Quote, Trade
from app.events.types import EventType
from app.market_data.history import compute_warmup_requirement
from app.recovery import RecoveryState, RecoveryStateMachine
from app.runtime import build_runtime
from tests.support import SYMBOL, make_signal


def _config(**overrides):
    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.execution.enabled = False
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _engine(**overrides):
    return build_runtime(_config(**overrides), get_env()).engine


def _bar(minutes: int, *, base: float = 30000.0, symbol: str = SYMBOL, tf: str = "1m") -> Candle:
    return Candle(
        timestamp=(utcnow() - timedelta(hours=24)).replace(second=0, microsecond=0) + timedelta(minutes=minutes),
        symbol=symbol, open=base, high=base * 1.001, low=base * 0.999,
        close=base * 1.0005, volume=1.0, timeframe=tf,
    )


async def _watch(engine):
    events: list = []
    engine.bus.subscribe_all(events.append)
    return events


async def _warmed_engine():
    engine = _engine()
    engine.config.market_data.history.provider = "alpaca"
    engine.config.market_data.history.coverage = CoveragePolicyConfig(
        minimum_percent=50.0, maximum_gap_candles=5, on_insufficient="warn"
    )
    requirement = compute_warmup_requirement(engine.config)
    data = []
    for m in range(requirement.history_bars_needed + 2):
        data.append(_bar(m - requirement.history_bars_needed - 2, base=30000.0 + m))

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        return data

    engine.history_client.fetch_candles = _fetch
    assert await engine.warm_up() is True
    return engine


# 1. disconnect detection
async def test_disconnect_detection_moves_to_degraded():
    engine = _engine()
    await engine._on_provider_disconnected(None)
    assert engine.recovery_sm.state is RecoveryState.DEGRADED
    assert engine.recovery_sm.is_trade_allowed() is False
    # Fail-closed gate: risk context fresh=False during degradation.
    assert engine._build_risk_context(utcnow()).market_data_fresh is False


# 2. reconnect (connection != readiness)
async def test_reconnect_event_does_not_restore_readiness():
    engine = _engine()
    await engine._on_provider_disconnected(None)
    await engine._on_provider_reconnected(None)
    assert engine.recovery_sm.state is RecoveryState.RECOVERING
    assert engine.recovery_sm.is_trade_allowed() is False
    assert engine._build_risk_context(utcnow()).market_data_fresh is False


# 3. failed reconnect
async def test_failed_reconnect_blocks_entries():
    engine = _engine()
    await engine._on_provider_reconnect_failed(None)
    assert engine.recovery_sm.state is RecoveryState.RECOVERY_FAILED
    assert engine._build_risk_context(utcnow()).market_data_fresh is False
    assert engine.oms.all_orders() == []


# 4. recovery timeout
async def test_recovery_timeout_fails_closed():
    engine = await _warmed_engine()

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        raise TimeoutError("simulated recovery timeout")

    engine.history_client.fetch_candles = _fetch
    ok = await engine.resync(reason="timeout_test")
    assert ok is False
    assert engine.recovery_sm.state is RecoveryState.RECOVERY_FAILED
    assert engine._data_gap_ok is False
    assert engine.oms.all_orders() == []


# 5. stale data
async def test_stale_data_blocks_entries():
    engine = _engine()
    engine._last_bar_at = utcnow() - timedelta(hours=1)
    ctx = engine._build_risk_context(utcnow())
    assert ctx.market_data_fresh is False


# 6. stale recovery only after valid data
async def test_stale_recovery_only_after_valid_data():
    engine = await _warmed_engine()
    # Force stale: leave the last bar an hour old.
    engine._last_bar_at = utcnow() - timedelta(hours=1)
    assert engine._build_risk_context(utcnow()).market_data_fresh is False
    # A stream of bars resumes; only a fresh, gap-free bar restores the gate.
    engine._last_bar_at = utcnow()
    fresh = engine.freshness_policy.evaluate(
        source="bar", last_received=engine._last_bar_at, now=utcnow(),
        expected_interval_seconds=60,
    )
    assert fresh.is_stale is False
    # Once validated, the recovery machine must report READY before entries re-open.
    engine.recovery_sm.transition(RecoveryState.READY, reason="validated_by_live_bar")
    assert engine._build_risk_context(utcnow()).market_data_fresh is True


# 7. bar gap
async def test_bar_gap_marks_degraded_and_blocks():
    engine = _engine()
    engine.config.market_data.bars.max_gap_candles = 2
    engine.config.market_data.bars.resync_enabled = False
    events = await _watch(engine)
    await engine.on_market_update(_bar(1))
    await engine.on_market_update(_bar(30))
    types = [e.type for e in events]
    assert EventType.DATA_GAP in types
    assert engine._data_gap_ok is False
    assert engine.recovery_sm.state is RecoveryState.DEGRADED
    assert engine._build_risk_context(utcnow()).market_data_fresh is False


# 8. gap recovery
async def test_gap_recovery_restores_readiness():
    engine = await _warmed_engine()
    repaired = [_bar(m, base=30000.0 + m) for m in range(-20, 3)]

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        return repaired

    engine.history_client.fetch_candles = _fetch
    ok = await engine.resync(reason="gap_test")
    assert ok is True
    assert engine.recovery_sm.state is RecoveryState.READY
    assert engine._data_gap_ok is True
    assert engine.readiness()["recovery_state"] == "completed"
    assert engine.readiness()["data_integrity_ok"] is True
    assert engine.readiness()["strategy_ready"] is True


# 9. duplicate message
async def test_duplicate_message_ignored():
    engine = _engine()
    events = await _watch(engine)
    bar = _bar(1)
    await engine.on_market_update(bar)
    await engine.on_market_update(bar)
    assert EventType.BAR_DUPLICATE in [e.type for e in events]
    assert len(engine.store.candles(SYMBOL, "1m")) == 1
    assert engine.market_stats["bars_duplicate"] == 1


# 10. out-of-order message
async def test_out_of_order_message_rejected():
    engine = _engine()
    events = await _watch(engine)
    await engine.on_market_update(_bar(5))
    await engine.on_market_update(_bar(2))
    assert EventType.BAR_OUT_OF_ORDER in [e.type for e in events]
    assert engine.market_stats["bars_out_of_order"] == 1


# 11. future timestamp
async def test_future_timestamp_rejected_no_state_mutation():
    engine = _engine()
    events = await _watch(engine)
    future = _bar(1)
    future = future.model_copy(update={"timestamp": utcnow() + timedelta(days=3650)})
    await engine.on_market_update(future)
    assert EventType.FUTURE_TIMESTAMP_REJECTED in [e.type for e in events]
    assert engine._last_bar_at is None
    assert engine.store.candles(SYMBOL, "1m") == []


# 12. recovery state machine
def test_recovery_state_machine_transitions_and_history():
    sm = RecoveryStateMachine()
    assert sm.state is RecoveryState.HEALTHY
    sm.transition(RecoveryState.DEGRADED, reason="gap")
    sm.transition(RecoveryState.RESYNCING, reason="resync", attempt=1)
    sm.transition(RecoveryState.VALIDATING, reason="validate", attempt=1)
    sm.transition(RecoveryState.READY, reason="validated", attempt=1)
    assert sm.is_trade_allowed() is True
    hist = sm.history()
    assert [h["state"] for h in hist] == ["HEALTHY", "DEGRADED", "RESYNCING", "VALIDATING", "READY"]
    sm.transition(RecoveryState.RECOVERY_FAILED, reason="timeout")
    assert sm.is_trade_allowed() is False


# 13. entry blocking during recovery
async def test_entry_blocking_during_recovery():
    engine = _engine()
    engine.recovery_sm.transition(RecoveryState.RECOVERING, reason="test")
    ctx = engine._build_risk_context(utcnow())
    assert ctx.market_data_fresh is False
    # A full signal cannot become executable: risk rejects before OMS.
    signal = make_signal()
    decision = engine.risk_engine.evaluate(signal, ctx)
    assert decision.approved is False


# 14. entry blocking after recovery failure
async def test_entry_blocking_after_recovery_failure():
    engine = await _warmed_engine()

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        raise RuntimeError("down")

    engine.history_client.fetch_candles = _fetch
    ok = await engine.resync(reason="fail_test")
    assert ok is False
    ctx = engine._build_risk_context(utcnow())
    assert ctx.market_data_fresh is False
    signal = make_signal()
    decision = engine.risk_engine.evaluate(signal, ctx)
    assert decision.approved is False


# 15. readiness restoration
async def test_readiness_restored_after_valid_resync():
    engine = await _warmed_engine()
    repaired = [_bar(m, base=30000.0 + m) for m in range(-20, 3)]

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        return repaired

    engine.history_client.fetch_candles = _fetch
    await engine.resync(reason="restore_test")
    readiness = engine.readiness()
    assert readiness["recovery_state_machine"]["state"] == "READY"
    assert readiness["data_integrity_ok"] is True
    assert readiness["features_ready"] is True
    assert readiness["regime_ready"] is True


# 16. feature rebuild
async def test_feature_rebuild_after_recovery():
    engine = await _warmed_engine()
    repaired = [_bar(m, base=30000.0 + m) for m in range(-20, 3)]

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        return repaired

    engine.history_client.fetch_candles = _fetch
    await engine.resync(reason="features_test")
    assert engine.state.features is not None
    assert engine.state.features.candle_count > 0


# 17. regime rebuild
async def test_regime_rebuild_after_recovery():
    engine = await _warmed_engine()
    repaired = [_bar(m, base=30000.0 + m) for m in range(-20, 3)]

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        return repaired
    engine.history_client.fetch_candles = _fetch
    await engine.resync(reason="regime_test")
    assert engine.state.regime is not None
    assert not engine.state.regime.is_unknown


# 18. reconciliation after recovery
async def test_reconciliation_check_stays_clear_after_recovery():
    engine = await _warmed_engine()
    repaired = [_bar(m, base=30000.0 + m) for m in range(-20, 3)]

    async def _fetch(*, symbol, bars, timeframe, timeout_seconds):
        return repaired

    engine.history_client.fetch_candles = _fetch
    await engine.resync(reason="reconcile_test")
    assert engine._need_reconciliation is False
    assert engine.health.ready() is True


# 19. no fabricated market data
async def test_no_fabricated_market_data_under_faults():
    engine = _engine()
    events = await _watch(engine)
    good = _bar(1)
    await engine.on_market_update(good)
    store_count = len(engine.store.candles(SYMBOL, "1m"))
    # Duplicates, out-of-order, future timestamps cannot add candles.
    await engine.on_market_update(good)
    await engine.on_market_update(_bar(0))
    await engine.on_market_update(_bar(1).model_copy(update={"timestamp": utcnow() + timedelta(days=3650)}))
    assert len(engine.store.candles(SYMBOL, "1m")) == store_count


# 20. execution gate remains disabled
async def test_execution_gate_remains_disabled():
    engine = _engine()
    assert engine.execution_enabled is False
    signal = make_signal()
    sizing = engine.sizer.size(signal, 100000.0, reference_price=100.0, max_notional=20000.0)
    await engine._submit_entry(signal, sizing=sizing)
    assert engine.oms.all_orders() == []
    types = [e.type for e in []]
    assert engine.session.state is not None  # engine untouched
