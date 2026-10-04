"""Phase A #4 (risk authority), #8 (session closeout), #9 (reconciliation)."""
from datetime import datetime, timezone

from app.config.loader import get_env, load_config
from app.config.models import SessionCloseoutConfig, SessionConfig
from app.domain.enums import ReasonCode, SessionState
from app.events.types import EventType
from app.risk.engine import RiskEngine
from app.runtime import build_runtime
from app.sessions.manager import SessionManager
from tests.support import make_risk_context, make_signal

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc)


def _engine():
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    # These tests exercise broker-interaction paths (flatten via a real exit
    # order, broker reconciliation), which only exist when the Phase B
    # execution gate is open. The gate itself is tested in
    # tests/test_phase_b_execution_gate.py.
    config.execution.enabled = True
    return build_runtime(config, env).engine


def _collect(engine) -> list:
    seen: list = []
    engine.bus.subscribe_all(lambda event: seen.append(event))
    return seen


# --- #4 risk authority ------------------------------------------------------
def test_risk_rejects_stale_market_data():
    decision = RiskEngine(config=_risk_config()).evaluate(
        make_signal(), make_risk_context(market_data_fresh=False)
    )
    assert not decision.approved
    assert decision.reason is ReasonCode.STALE_DATA


def test_risk_rejects_when_reconciliation_failed():
    decision = RiskEngine(config=_risk_config()).evaluate(
        make_signal(), make_risk_context(reconciliation_ok=False)
    )
    assert not decision.approved
    assert decision.reason is ReasonCode.RECONCILIATION_FAILED


def test_risk_disabled_rejects_everything():
    from app.config.models import RiskConfig

    decision = RiskEngine(config=RiskConfig(enabled=False)).evaluate(make_signal(), make_risk_context())
    assert not decision.approved
    assert decision.reason is ReasonCode.RISK_DISABLED


def _risk_config():
    from app.config.models import RiskConfig

    return RiskConfig()


async def test_engine_entry_is_gated_by_risk():
    """A signal reaching _handle_signal must be adjudicated by risk; with an
    unstarted (non-TRADING) session it is rejected and no order is created."""
    engine = _engine()
    await engine._handle_signal(make_signal())
    assert engine.oms.all_orders() == []
    assert engine.state.rejections


# --- #8 session closeout ----------------------------------------------------
def test_entry_cutoff_blocks_new_entries():
    manager = SessionManager(SessionConfig(), SessionCloseoutConfig())
    manager.start(_at(0, 1))
    manager.update(_at(23, 40))  # after 23:30 cutoff / 30-min closeout stop
    assert manager.entries_allowed is False


def test_entries_allowed_before_cutoff():
    manager = SessionManager(SessionConfig(), SessionCloseoutConfig())
    manager.start(_at(0, 1))
    manager.update(_at(12, 0))
    assert manager.entries_allowed is True


def test_flatten_deadline_forces_closeout():
    manager = SessionManager(SessionConfig(), SessionCloseoutConfig())
    manager.start(_at(0, 1))
    state = manager.update(_at(23, 56))  # past 23:55 deadline
    assert state is SessionState.CLOSEOUT
    assert manager.entries_allowed is False


def test_mark_flat_failure_halts_session():
    manager = SessionManager(SessionConfig(), SessionCloseoutConfig())
    manager.start(_at(0, 1))
    manager.mark_flat(success=False, note="flatten_failed")
    assert manager.is_flat is False
    assert manager.state is SessionState.HALTED

    manager2 = SessionManager(SessionConfig(), SessionCloseoutConfig())
    manager2.start(_at(0, 1))
    manager2.mark_flat(success=True)
    assert manager2.is_flat is True
    assert manager2.state is SessionState.COMPLETED


# --- engine flatten + reconciliation ----------------------------------------
from app.brokers.mock import MockBroker  # noqa: E402
from app.core.clock import utcnow  # noqa: E402
from app.domain.enums import Side  # noqa: E402
from tests.support import SYMBOL, make_fill  # noqa: E402


async def test_successful_flatten_marks_session_flat():
    engine = _engine()
    broker = engine.broker
    broker.set_price(SYMBOL, 100.0)
    await broker.connect()
    engine.session.start(utcnow())
    events = _collect(engine)

    flat = await engine.flatten(session_closeout=True)

    assert flat is True
    assert engine.position_manager.is_flat
    assert engine.session.is_flat is True
    assert engine.session.state is SessionState.COMPLETED
    types = {event.type for event in events}
    assert EventType.FLATTEN_COMPLETED in types
    assert EventType.FLATTEN_FAILED not in types


async def test_submitted_exit_order_is_not_a_successful_flatten():
    """An exit order may be submitted and even filled internally, but unless the
    authoritative broker position is zero the flatten is a FAILURE."""
    engine = _engine()

    class _StaleBroker(MockBroker):
        def __init__(self) -> None:
            super().__init__()
            self.submit_calls = 0

        async def submit_order(self, order):  # noqa: ANN001 - test double
            self.submit_calls += 1
            return await super().submit_order(order)

        async def get_positions(self):  # broker still reports a position
            return [object()]

    broker = _StaleBroker()
    broker.set_price(SYMBOL, 100.0)
    await broker.connect()
    engine.broker = broker
    engine.oms.broker = broker
    engine.position_manager.apply_fill(make_fill(1.0, 100.0, Side.BUY, fill_id="seed"))
    engine.session.start(utcnow())
    events = _collect(engine)

    flat = await engine.flatten(session_closeout=True)

    assert flat is False
    assert broker.submit_calls >= 1  # an exit order WAS submitted
    assert engine.session.is_flat is False
    assert engine.session.state is SessionState.HALTED
    types = {event.type for event in events}
    assert EventType.FLATTEN_FAILED in types
    assert EventType.FLATTEN_COMPLETED not in types


async def test_reconcile_blocks_entries_on_mismatch():
    engine = _engine()

    class _MismatchBroker(MockBroker):
        async def reconcile(self, internal):  # noqa: ANN001 - test double
            return ["quantity mismatch BTC/USD"]

    broker = _MismatchBroker()
    await broker.connect()
    engine.broker = broker

    ok = await engine.reconcile()

    assert ok is False
    assert engine._need_reconciliation is True
    assert engine.state.reconciliation["ok"] is False
    assert engine._build_risk_context(utcnow()).reconciliation_ok is False


async def test_reconcile_treats_unknown_broker_state_as_not_flat():
    engine = _engine()

    class _DownBroker(MockBroker):
        async def get_account(self):  # noqa: ANN001 - test double
            raise RuntimeError("exchange down")

    broker = _DownBroker()
    await broker.connect()
    engine.broker = broker

    ok = await engine.reconcile()

    assert ok is False
    assert engine._need_reconciliation is True
    assert any("reconciliation_query_failed" in d for d in engine.state.reconciliation["discrepancies"])

