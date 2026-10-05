"""Phase A #11 (config source of truth), #12 (event logging), #13 (dashboard)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.loader import PROJECT_ROOT, get_env, load_config, sanitized_config
from app.config.models import AppConfig
from app.events.types import EventType
from app.main import create_app
from app.runtime import build_runtime


def _config():
    return load_config(env=get_env())


def _runtime():
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.market_data.max_future_skew_seconds = 86400 * 30
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    return build_runtime(config, env)


# --- #11 configuration source of truth --------------------------------------
def test_config_yaml_contains_no_secrets():
    text = (PROJECT_ROOT / "configs" / "config.yaml").read_text(encoding="utf-8")
    for token in ("API_KEY", "API_SECRET", "SECRET_KEY", "PASSWORD", "TOKEN"):
        assert token not in text, f"secret-like token {token} found in config.yaml"
    for secret in get_env().secret_values():
        assert secret not in text


def test_tunables_come_from_config():
    config = _config()
    assert config.trading.symbol  # not hardcoded in code
    assert config.timeframes.signal and config.timeframes.context
    assert config.risk.risk_per_trade_percent > 0
    assert config.risk.maximum_position_value_percent > 0
    assert config.position_sizing.quantity_precision >= 0
    assert config.dashboard.port > 0
    assert config.ai.provider  # provider is configurable, not hardcoded
    # Ambiguous submissions are reconciled, never blindly retried (Phase A).
    assert config.execution.retry.action == "reconcile"
    assert config.position_sizing.risk_per_trade_percent is None


def test_conflicting_legacy_position_risk_is_rejected():
    config = load_config(env=get_env()).model_dump()
    config["position_sizing"]["risk_per_trade_percent"] = 1.0
    with pytest.raises(ValueError, match="must match"):
        AppConfig.model_validate(config)


def test_sanitized_config_excludes_secret_values():
    env = get_env()
    payload = str(sanitized_config(_config(), env))
    for secret in env.secret_values():
        assert secret not in payload


# --- #13 dashboard ----------------------------------------------------------
def test_dashboard_shows_all_required_panels():
    client = TestClient(create_app(autostart=False))
    html = client.get("/dashboard").text
    for panel in ("System", "Market", "Features", "Regime", "Strategy", "Risk", "Position", "Orders", "Session", "AI"):
        assert panel in html, f"dashboard missing {panel} panel"
    assert "P&amp;L" in html
    for element_id in ("health", "session", "system", "market", "features", "regime", "strategy", "risk", "position", "pnl", "orders", "ai", "stream"):
        assert f'id="{element_id}"' in html, f"dashboard missing {element_id} element"
    for element_id in ("handoff", "candles", "recovery", "logging", "freshness-state", "decision-trace"):
        assert f'id="{element_id}"' in html, f"dashboard missing {element_id} element"


def test_handoff_endpoint_is_read_only_and_exposes_live_watermark():
    with TestClient(create_app(config=_config(), autostart=False)) as client:
        response = client.get("/api/handoff")
        assert response.status_code == 200
        payload = response.json()
        assert payload["live_bar_count"] == 0
        assert "warmup" in payload
        assert "candles" in payload
        logging_payload = client.get("/api/logging").json()
        assert logging_payload["path"].endswith("logs\\trading-agent.log") or logging_payload["path"].endswith("logs/trading-agent.log")


# --- #12 critical event logging --------------------------------------------
async def test_startup_emits_critical_events():
    runtime = _runtime()
    engine = runtime.engine
    seen: list = []
    engine.bus.subscribe_all(lambda event: seen.append(event))
    await engine.start()
    try:
        types = {event.type for event in seen}
        assert EventType.SYSTEM_READY in types
        assert EventType.SESSION_STARTED in types
        assert EventType.RECONCILIATION_STARTED in types
    finally:
        await engine.stop()


async def test_market_data_event_is_emitted_per_tick():
    from datetime import datetime, timezone

    from app.domain.market import Trade
    from tests.support import SYMBOL

    runtime = _runtime()
    engine = runtime.engine
    seen: list = []
    engine.bus.subscribe_all(lambda event: seen.append(event))
    await engine.on_market_update(
        Trade(timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), symbol=SYMBOL, price=100.0, size=1.0)
    )
    assert EventType.MARKET_DATA_RECEIVED in {event.type for event in seen}
