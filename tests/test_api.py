"""API surface test: health + secret-free config handling and dashboard."""
from fastapi.testclient import TestClient

from app.main import create_app


def test_health_live_and_dashboard_served():
    client = TestClient(create_app(autostart=False))
    assert client.get("/health/live").json() == {"status": "alive"}
    assert client.get("/dashboard").status_code == 200
    body = client.get("/health").json()
    assert body["paper_trading_only"] is True


def test_config_endpoint_never_exposes_secrets():
    client = TestClient(create_app(autostart=False))
    config = client.app.state.config
    env = client.app.state.env
    from app.config.loader import sanitized_config

    payload = sanitized_config(config, env)
    text = str(payload)
    for secret in env.secret_values():
        assert secret not in text
