"""Phase D.1 — decision-engine hardening regression tests.

Covers:
  * freshness consistency (ONE authoritative calculation) + clock-skew states
  * TradePlan lifecycle, invalidation triggers, dynamic parameters
  * TradePlan persistence + reload across a restart
  * decision-trace reconstruction
  * position-sizing guard rails
  * full pipeline reaching (but never passing) the execution gate
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.config.models import DataFreshnessConfig, FreshnessBarConfig, PositionSizingConfig, TradePlanConfig
from app.core.clock import utcnow
from app.core.logging import configure_logging, reset_logging_state
from app.decision.freshness import FreshnessPolicy
from app.decision.trade_plan import TradePlanBuilder
from app.portfolio.position_sizing import PositionSizer
from app.domain.enums import Regime
from app.domain.trade_plan import TradePlanStatus
from app.runtime import build_runtime
from tests.support import NOW, make_features, make_regime, make_signal

SYMBOL = "BTC/USD"

TRACE_STATUSES = {"PASS", "BLOCKED", "REJECTED", "WAIT", "SKIPPED", "ERROR"}
CLOCK_STATES = {"NORMAL", "STALE", "FUTURE_TIMESTAMP", "CLOCK_SKEW", "NO_DATA"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _policy(mode: str = "cadence_aware", **overrides) -> FreshnessPolicy:
    bar = FreshnessBarConfig(
        expected_interval_seconds=overrides.get("interval", 60.0),
        grace_period_seconds=overrides.get("grace", 30.0),
        maximum_age_seconds=overrides.get("max_age", 90.0),
    )
    return FreshnessPolicy(DataFreshnessConfig(mode=mode, bar=bar))


async def _build_engine():
    reset_logging_state()
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.execution.enabled = False  # Phase D.1 safety: execution stays OFF
    configure_logging(config, env, project_root=PROJECT_ROOT)
    runtime = build_runtime(config, env)
    await runtime.engine.start()
    return runtime


async def _pump(engine, ticks: int = 700) -> None:
    provider = engine.provider
    n = 0
    while not provider.exhausted and n < ticks:
        await provider.pump_one()
        n += 1
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


def _storage_config(tmp_path):
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.execution.enabled = False
    config.storage.enabled = True
    config.storage.url = ""
    config.storage.sqlite_path = str(tmp_path / "d1.sqlite3")
    configure_logging(config, env, project_root=PROJECT_ROOT)
    return config, env


def _builder() -> TradePlanBuilder:
    return TradePlanBuilder(TradePlanConfig())


def _active_plan(builder: TradePlanBuilder | None = None):
    b = builder or _builder()
    # NOTE: the plan timestamp must be *now* — a fixture timestamp in the past
    # would legitimately trip MAXIMUM_HOLDING_EXCEEDED / stale-data checks.
    signal = make_signal(timestamp=utcnow())
    sizing = PositionSizer(PositionSizingConfig()).size(
        signal, 100000.0, reference_price=signal.entry_reference, max_notional=20000.0
    )
    return b.build_from_signal(
        signal,
        regime=make_regime(Regime.TRENDING_BULLISH, timestamp=utcnow()),
        features=make_features(timestamp=utcnow()),
        sizing=sizing,
        risk_percent=PositionSizingConfig().risk_per_trade_percent,
        maximum_notional=20000.0,
    ).plan


# ==========================================================================
# 3/4. FRESHNESS CONSISTENCY + CLOCK-SKEW STATES
# ==========================================================================
def test_freshness_normal_timestamp_is_fresh():
    r = _policy().evaluate(
        source="bar", last_received=NOW - timedelta(seconds=5), now=NOW,
        expected_interval_seconds=60,
    )
    assert r.is_stale is False
    assert r.reason == "fresh"
    assert r.age_seconds == 5.0


def test_freshness_stale_timestamp_is_stale():
    r = _policy().evaluate(
        source="bar", last_received=NOW - timedelta(seconds=600), now=NOW,
        expected_interval_seconds=60,
    )
    assert r.is_stale is True
    assert r.reason == "stale"


def test_freshness_future_timestamp_is_flagged_not_silently_fresh():
    r = _policy().evaluate(
        source="bar", last_received=NOW + timedelta(seconds=600), now=NOW,
        expected_interval_seconds=60,
    )
    assert r.future_timestamp is True
    assert r.reason == "future_timestamp"
    assert r.clock_skew_seconds == 600.0
    assert r.age_seconds == 0.0  # clamped for arithmetic, recorded not invented


def test_freshness_small_clock_skew_distinguished_from_future():
    r = _policy().evaluate(
        source="bar", last_received=NOW + timedelta(seconds=5), now=NOW,
        expected_interval_seconds=60,
    )
    assert r.future_timestamp is False
    assert r.clock_skew_seconds == 5.0
    assert r.reason == "clock_skew"


def test_freshness_no_data_is_stale():
    r = _policy().evaluate(source="bar", last_received=None, now=NOW)
    assert r.is_stale is True
    assert r.reason == "no_data"
    assert r.age_seconds is None


def test_freshness_cadence_floor_protects_healthy_stream():
    """A 60 s cadence must NOT be falsely stale against a 30 s flat threshold."""
    r = _policy(interval=60, grace=30, max_age=30).evaluate(
        source="bar", last_received=NOW - timedelta(seconds=75), now=NOW,
        expected_interval_seconds=60,
    )
    assert r.effective_threshold_seconds == 90.0
    assert r.is_stale is False


@pytest.mark.asyncio
async def test_stream_state_uses_same_policy_as_risk_gate():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        engine._last_bar_at = utcnow() - timedelta(seconds=45)
        now = utcnow()
        bar = engine.stream_state(now)["bars"]
        policy = engine.freshness_policy.evaluate(
            source="bar", last_received=engine._last_bar_at, now=now,
            expected_interval_seconds=60,
        )
        assert bar["threshold_seconds"] == policy.effective_threshold_seconds
        assert bar["fresh"] is (not policy.is_stale)
        assert bar["clock_state"] in CLOCK_STATES
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_stream_state_reports_clock_state_for_future_bar():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        engine._last_bar_at = utcnow() + timedelta(seconds=600)
        bar = engine.stream_state(utcnow())["bars"]
        assert bar["clock_state"] == "FUTURE_TIMESTAMP"
        assert bar["clock_skew_seconds"] > 0
    finally:
        await runtime.engine.stop()


# ==========================================================================
# 6/7/9. TRADEPLAN LIFECYCLE + INVALIDATION + DYNAMIC PARAMETERS
# ==========================================================================
def test_trade_plan_created_active_with_audit_fields():
    plan = _active_plan()
    assert plan.status == TradePlanStatus.ACTIVE
    assert plan.previous_status is None
    assert plan.invalidation_reason is None
    assert plan.timestamp.tzinfo is not None, "timestamps must be timezone-aware"


def test_trade_plan_lifecycle_transition_records_full_audit():
    b = _builder()
    result = b.invalidate(_active_plan(b), reason="REGIME_CHANGED")
    assert result.plan.previous_status == TradePlanStatus.ACTIVE
    assert result.plan.status == TradePlanStatus.INVALIDATED
    assert result.plan.invalidation_reason == "REGIME_CHANGED"
    entry = result.plan.lifecycle_history[-1]
    for key in ("previous_status", "new_status", "timestamp", "reason", "trigger", "source_event"):
        assert key in entry, f"lifecycle entry missing {key}"


def test_trade_plan_dynamic_holding_depends_on_regime():
    b = _builder()
    trending = _active_plan(b)
    range_bound = b.build_from_signal(
        make_signal(), regime=make_regime(Regime.RANGE_BOUND), features=make_features(),
    ).plan
    assert trending.expected_holding_minutes != range_bound.expected_holding_minutes
    assert range_bound.expected_holding_minutes < trending.maximum_holding_minutes


def test_trade_plan_stop_target_scale_with_atr():
    b = _builder()
    tight = b.build_from_signal(
        make_signal(), regime=make_regime(), features=make_features({"atr": 10.0}),
    ).plan
    wide = b.build_from_signal(
        make_signal(), regime=make_regime(), features=make_features({"atr": 100.0}),
    ).plan
    assert wide.stop_price < tight.stop_price, "higher ATR must widen the stop"


@pytest.mark.asyncio
async def test_trade_plan_invalidated_on_regime_change():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        engine._trade_plan = _active_plan()
        engine._last_bar_at = utcnow()          # fresh bars so staleness is not the trigger
        engine.state.regime = make_regime(Regime.RANGE_BOUND)
        assert engine.trade_plan_invalidity(engine._trade_plan, utcnow()) == "REGIME_CHANGED"
        await engine._invalidate_trade_plan_if_needed(utcnow())
        assert engine._trade_plan.status == TradePlanStatus.INVALIDATED
        assert engine._trade_plan.invalidation_reason == "REGIME_CHANGED"
        assert engine._trade_plan_history, "invalidated plan must be kept in history"
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_trade_plan_invalidated_on_stale_market_data():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        engine._trade_plan = _active_plan()
        engine.state.regime = make_regime(Regime.TRENDING_BULLISH)
        engine._last_bar_at = utcnow() - timedelta(seconds=10_000)
        assert engine.trade_plan_invalidity(engine._trade_plan, utcnow()) == "STALE_MARKET_DATA"
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_trade_plan_invalidated_on_data_integrity_failure():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        engine._trade_plan = _active_plan()
        engine.state.regime = make_regime(Regime.TRENDING_BULLISH)
        engine._last_bar_at = utcnow()
        engine._data_gap_ok = False
        assert engine.trade_plan_invalidity(engine._trade_plan, utcnow()) == "DATA_INTEGRITY_FAILURE"
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_trade_plan_invalidated_on_maximum_holding():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        plan = _active_plan()
        engine._trade_plan = plan.model_copy(update={
            "timestamp": utcnow() - timedelta(minutes=plan.maximum_holding_minutes + 5)
        })
        engine.state.regime = make_regime(Regime.TRENDING_BULLISH)
        engine._last_bar_at = utcnow()
        assert engine.trade_plan_invalidity(
            engine._trade_plan, utcnow()
        ) == "MAXIMUM_HOLDING_EXCEEDED"
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_trade_plan_invalidated_on_spread_widening():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        # pump first to obtain a real snapshot, THEN install the plan so the tick
        # loop cannot invalidate it before the assertion
        await _pump(engine, ticks=60)
        engine._trade_plan = _active_plan()
        engine._last_bar_at = utcnow()
        engine.state.regime = make_regime(Regime.TRENDING_BULLISH)
        # isolate the spread condition: the mock feed also trips the timeframe
        # gate, and invalidity is evaluated in a fixed priority order
        engine._timeframe_selection = None
        snap = engine.state.latest_snapshot
        # spread_percent is derived from bid/ask, so widen the quote itself
        engine.state.latest_snapshot = snap.model_copy(
            update={"bid": 100.0, "ask": 500.0}
        )
        assert engine.trade_plan_invalidity(engine._trade_plan, utcnow()) == "SPREAD_UNACCEPTABLE"
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_valid_plan_reports_no_invalidity():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        engine._trade_plan = _active_plan()
        engine.state.regime = make_regime(Regime.TRENDING_BULLISH)
        engine._last_bar_at = utcnow()
        engine.session.entries_allowed = True
        engine._timeframe_selection = None
        engine._need_reconciliation = False
        engine._data_gap_ok = True
        assert engine.trade_plan_invalidity(engine._trade_plan, utcnow()) is None
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_invalidated_plan_emits_event_and_blocks():
    runtime = await _build_engine()
    engine = runtime.engine
    events = []
    engine.bus.subscribe_all(events.append)
    try:
        engine._trade_plan = _active_plan()
        engine.state.regime = make_regime(Regime.RANGE_BOUND)
        await engine._invalidate_trade_plan_if_needed(utcnow())
        types = [e.type.value for e in events]
        assert "TradePlanInvalidated" in types
        evt = next(e for e in events if e.type.value == "TradePlanInvalidated")
        assert evt.payload["action"] == "BLOCK_FURTHER_EXECUTION"
        assert evt.payload["previous_status"] == "active"
        assert evt.payload["new_status"] == "invalidated"
    finally:
        await runtime.engine.stop()


# ==========================================================================
# 5/15. PERSISTENCE + RESTART
# ==========================================================================
@pytest.mark.asyncio
async def test_trade_plan_persists_and_reloads_after_restart(tmp_path):
    """START -> CREATE -> PERSIST -> STOP -> RESTART -> LOAD -> VERIFY."""
    config, env = _storage_config(tmp_path)

    # --- session 1: create + persist
    runtime1 = build_runtime(config, env)
    engine1 = runtime1.engine
    await engine1.start()
    plan = _active_plan()
    saved = await engine1.repository.save_trade_plan(
        plan.model_dump(mode="json"), session_id="ses_d1"
    )
    assert saved is True
    await engine1.stop()

    # --- session 2: restart, reload, verify
    runtime2 = build_runtime(config, env)
    engine2 = runtime2.engine
    await engine2.start()
    try:
        loaded = await engine2.repository.load_trade_plan(plan.plan_id)
        assert loaded is not None, "TradePlan must survive a restart"
        assert loaded["plan_id"] == plan.plan_id
        assert loaded["status"] == TradePlanStatus.ACTIVE.value
        assert loaded["direction"] == "long"
        latest = await engine2.repository.load_latest_trade_plan()
        assert latest is not None
        assert latest["plan_id"] == plan.plan_id
    finally:
        await engine2.stop()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_trade_plan_columns_map_to_real_values(tmp_path):
    """Regression: stop/target/quantity columns must NOT silently persist NULL."""
    config, env = _storage_config(tmp_path)
    runtime = build_runtime(config, env)
    engine = runtime.engine
    await engine.start()
    try:
        plan = _active_plan()
        assert plan.stop_price is not None and plan.target_price is not None
        assert plan.position_quantity is not None
        await engine.repository.save_trade_plan(plan.model_dump(mode="json"), session_id="s")
        async with engine.repository._session_factory() as session:
            from app.storage.models import TradePlanRow
            row = await session.get(TradePlanRow, plan.plan_id)
            assert row is not None
            assert row.stop_reference == plan.stop_price
            assert row.target_reference == plan.target_price
            assert row.quantity == plan.position_quantity
            assert row.risk_amount == plan.risk_amount
            assert row.regime == plan.regime.value
            assert row.strategy_name == plan.strategy
            assert row.symbol == plan.symbol
            assert row.direction == plan.direction.value
            assert row.invalidation_reason is None
            assert row.reason_codes["entry_conditions"], "reason_codes must carry conditions"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_trade_plan_invalidation_reason_persists_to_column(tmp_path):
    config, env = _storage_config(tmp_path)
    runtime = build_runtime(config, env)
    engine = runtime.engine
    await engine.start()
    try:
        plan = _active_plan()
        await engine.repository.save_trade_plan(plan.model_dump(mode="json"), session_id="s")
        engine._trade_plan = plan
        engine._last_bar_at = utcnow()
        engine.state.regime = make_regime(Regime.RANGE_BOUND, timestamp=utcnow())
        engine._timeframe_selection = None
        await engine._invalidate_trade_plan_if_needed(utcnow())
        async with engine.repository._session_factory() as session:
            from app.storage.models import TradePlanRow
            row = await session.get(TradePlanRow, plan.plan_id)
            assert row.status == "invalidated"
            assert row.invalidation_reason == "REGIME_CHANGED"
    finally:
        await engine.stop()


# ==========================================================================
# 11. DECISION TRACE
# ==========================================================================
async def test_trade_plan_update_overwrites_existing_row(tmp_path):
    config, env = _storage_config(tmp_path)
    runtime = build_runtime(config, env)
    await runtime.engine.start()
    try:
        b = _builder()
        plan = _active_plan(b)
        await runtime.engine.repository.save_trade_plan(plan.model_dump(mode="json"), session_id="s")
        invalidated = b.invalidate(plan, reason="STALE_MARKET_DATA").plan
        await runtime.engine.repository.save_trade_plan(
            invalidated.model_dump(mode="json"), session_id="s"
        )
        loaded = await runtime.engine.repository.load_trade_plan(plan.plan_id)
        assert loaded["status"] == TradePlanStatus.INVALIDATED.value
        assert loaded["invalidation_reason"] == "STALE_MARKET_DATA"
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_trade_plan_invalidation_is_persisted(tmp_path):
    config, env = _storage_config(tmp_path)
    runtime = build_runtime(config, env)
    engine = runtime.engine
    await engine.start()
    try:
        plan = _active_plan()
        await engine.repository.save_trade_plan(plan.model_dump(mode="json"), session_id="s")
        # drive a real invalidation through the engine so persistence + event
        # emission + history are all exercised together
        engine._trade_plan = plan
        engine._last_bar_at = utcnow()
        engine.state.regime = make_regime(Regime.RANGE_BOUND)
        await engine._invalidate_trade_plan_if_needed(utcnow())
        assert engine._trade_plan.status == TradePlanStatus.INVALIDATED
        loaded = await engine.repository.load_trade_plan(plan.plan_id)
        assert loaded is not None
        assert loaded["status"] == "invalidated"
        assert loaded["invalidation_reason"] == "REGIME_CHANGED"
    finally:
        await engine.stop()


# ==========================================================================
# 11. DECISION TRACE
# ==========================================================================
@pytest.mark.asyncio
async def test_decision_trace_has_all_pipeline_stages():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        await _pump(engine)
        stages = {e["stage"] for e in engine.decision_trace()}
        for required in ("FRESHNESS", "TIMEFRAME_SELECTION", "FEATURES", "REGIME",
                         "EXECUTION_GATE"):
            assert required in stages, f"missing trace stage {required}"
        for entry in engine.decision_trace():
            assert set(entry) >= {"stage", "status", "decision", "reason",
                                  "inputs", "output", "timestamp"}
            assert entry["status"] in TRACE_STATUSES
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_decision_trace_records_execution_gate_disabled():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        await _pump(engine)
        gate = [e for e in engine.decision_trace() if e["stage"] == "EXECUTION_GATE"]
        assert gate, "execution gate must always appear in the trace"
        assert gate[-1]["output"].get("status") == "DISABLED"
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_decision_trace_freshness_stage_reports_thresholds():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        await _pump(engine)
        fresh = [e for e in engine.decision_trace() if e["stage"] == "FRESHNESS"]
        assert fresh
        out = fresh[-1]["output"]
        for key in ("observed_age_seconds", "measurement_threshold", "risk_threshold",
                    "risk_state", "clock_state"):
            assert key in out, f"freshness trace missing {key}"
        assert out["clock_state"] in CLOCK_STATES
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_decision_trace_is_bounded():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        limit = engine.config.trade_plan.decision_trace_max_stages
        for _ in range(limit + 20):
            engine._trace("X", "PASS", decision="D", reason="R")
        assert len(engine.decision_trace()) <= limit
    finally:
        await runtime.engine.stop()


# ==========================================================================
# 10. POSITION SIZING GUARD RAILS
# ==========================================================================
@pytest.mark.asyncio
async def test_sizing_rejects_malformed_stop_distance():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        # zero stop distance (stop == entry)
        sig = make_signal(stop_reference=30000.0)
        sig.entry_reference = 30000.0
        assert engine.sizer.size(sig, 100_000.0).rejected_reason == "invalid_stop_distance"
        # missing stop reference
        sig2 = make_signal(stop_reference=30000.0)
        sig2.stop_reference = None
        assert engine.sizer.size(sig2, 100_000.0).rejected_reason == "invalid_stop_distance"
        # non-finite entry makes the derived distance NaN/inf
        for bad in (float("nan"), float("inf")):
            sig3 = make_signal(stop_reference=30000.0)
            sig3.entry_reference = bad
            result = engine.sizer.size(sig3, 100_000.0)
            assert result.ok is False, f"entry_reference={bad} must be rejected"
            assert result.rejected_reason
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_sizing_rejects_non_positive_equity():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        for bad in (0.0, -1000.0, float("nan"), float("inf")):
            result = engine.sizer.size(make_signal(), bad)
            assert result.ok is False
            assert result.rejected_reason
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_sizing_respects_precision_and_maximum():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        cfg = engine.config.position_sizing
        result = engine.sizer.size(make_signal(), 100_000.0)
        assert result.ok is True
        assert result.final_quantity > 0
        assert result.final_quantity <= cfg.maximum_quantity
        decimals = len(str(result.final_quantity).split(".")[-1])
        assert decimals <= cfg.quantity_precision
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_sizing_caps_notional():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        result = engine.sizer.size(make_signal(), 100_000.0, reference_price=30_000.0,
                                   max_notional=1_000.0)
        assert result.ok is True
        assert result.final_quantity * 30_000.0 <= 1_000.0 + 1e-6
    finally:
        await runtime.engine.stop()


# ==========================================================================
# 22. FULL PIPELINE — execution MUST remain disabled
# ==========================================================================
@pytest.mark.asyncio
async def test_full_pipeline_reaches_gate_and_stops():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        await _pump(engine)
        assert engine.execution_enabled is False
        assert engine.oms.all_orders() == []
        assert engine.position_manager.is_flat
        assert engine.readiness()["execution"] == "DISABLED"
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_pipeline_emits_block_and_creates_no_orders():
    runtime = await _build_engine()
    engine = runtime.engine
    events = []
    engine.bus.subscribe_all(events.append)
    try:
        await _pump(engine)
        types = [e.type.value for e in events]
        assert "EntryBlocked" in types or "ExecutionBlocked" in types
        assert engine.oms.all_orders() == []
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_payload_exposes_trace_and_history():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        await _pump(engine)
        payload = engine.payload()
        assert "decision_trace" in payload
        assert "trade_plan_history" in payload
        assert "freshness_policy" in payload
        assert "timeframe_selection" in payload
        assert "trade_plan" in payload
    finally:
        await runtime.engine.stop()
@pytest.mark.asyncio
async def test_stream_state_reports_no_data():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        engine._last_bar_at = None
        bar = engine.stream_state(utcnow())["bars"]
        assert bar["clock_state"] == "NO_DATA"
        assert bar["fresh"] is False
    finally:
        await runtime.engine.stop()


@pytest.mark.asyncio
async def test_update_data_health_uses_cadence_aware_threshold():
    """Regression: the old static 30 s threshold must not trigger stale."""
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        engine._stale_flag = False
        engine._last_bar_at = utcnow() - timedelta(seconds=45)  # > 30 s, < 90 s
        await engine._update_data_health(utcnow())
        assert engine._stale_flag is False
        engine._last_bar_at = utcnow() - timedelta(seconds=500)
        await engine._update_data_health(utcnow())
        assert engine._stale_flag is True
    finally:
        await runtime.engine.stop()


def test_trade_plan_wait_when_regime_unknown():
    b = _builder()
    plan = b.build_from_signal(
        make_signal(), regime=make_regime(Regime.UNKNOWN), features=make_features(),
    ).plan
    assert plan.status == TradePlanStatus.WAIT
    assert plan.direction is None


def test_trade_plan_reactivation_from_wait():
    b = _builder()
    waiting = b.build_from_signal(
        make_signal(), regime=make_regime(Regime.UNKNOWN), features=make_features(),
    ).plan
    reactivated = b.reactivate(waiting, reason="regime_became_known").plan
    assert reactivated.status == TradePlanStatus.ACTIVE
    assert reactivated.previous_status == TradePlanStatus.WAIT


@pytest.mark.asyncio
async def test_trade_plan_invalidated_on_session_cutoff():
    runtime = await _build_engine()
    engine = runtime.engine
    try:
        engine._trade_plan = _active_plan()
        engine._last_bar_at = utcnow()
        engine.state.regime = make_regime(Regime.TRENDING_BULLISH)
        engine.session.entries_allowed = False
        assert engine.trade_plan_invalidity(
            engine._trade_plan, utcnow()
        ) == "SESSION_ENTRIES_CLOSED"
    finally:
        await runtime.engine.stop()
