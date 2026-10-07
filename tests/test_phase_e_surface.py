"""Phase E tests part 3: API, dashboard, AI tool, package hygiene."""
from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from app.ai_supervisor.tools import build_gateway
from app.config.loader import get_env, load_config
from app.main import create_app
from app.runtime import build_runtime


def _engine_config():
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.market_data.max_future_skew_seconds = 86400 * 30
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.sentiment.enabled = True
    config.sentiment.provider = "mock"
    return config, env


def test_api_sentiment_returns_sanitized_payload():
    config, env = _engine_config()
    env.sentiment_api_token = "tok-secret-12345"
    with TestClient(create_app(config=config, env=env, autostart=False)) as client:
        body = client.get("/api/sentiment").json()
        assert body is None or "tok-secret-12345" not in str(body)
        text = str(client.get("/api/config").json())
        assert "tok-secret-12345" not in text


def test_dashboard_contains_sentiment_card():
    client = TestClient(create_app(autostart=False))
    html = client.get("/dashboard").text
    assert 'id="sentiment"' in html
    assert "Sentiment" in html


async def test_ai_tool_gateway_exposes_read_only_sentiment():
    config, env = _engine_config()
    runtime = build_runtime(config, env)
    gateway = build_gateway(runtime.engine)
    assert "get_sentiment" in gateway.names()
    result = await gateway.call("get_sentiment", {}, requester="test")
    assert result is None or isinstance(result, dict)


def test_sentiment_package_has_no_symbol_or_vendor_references():
    root = pathlib.Path("app/sentiment")
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in ("BTC", "BTC/USD", "USD", "Alpaca", "alpaca"):
            assert token not in text, f"{path.as_posix()} references {token}"
