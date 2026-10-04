"""Phase A #10: AI safety.

Invariant: the AI can only act through the permission-checked tool gateway. It
cannot bypass risk, touch the broker directly, call unknown/unauthorized tools,
or enable flatten (which stays disabled by configuration).
"""
import pytest

from app.ai_supervisor.gateway import ToolGateway, ToolSpec
from app.config.loader import get_env, load_config
from app.config.models import AiConfig
from app.core.errors import ToolPermissionError, UnknownToolError
from app.domain.enums import Permission
from app.runtime import build_runtime


def _runtime():
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False  # UnavailableProvider; no network in tests
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    return build_runtime(config, env)


def test_gateway_exposes_no_broker_or_order_tools():
    runtime = _runtime()
    names = set(runtime.gateway.names())
    for forbidden in ("submit_order", "place_order", "cancel_order", "close_position", "get_broker"):
        assert forbidden not in names
    assert "get_orders" in names  # read-only visibility only


async def test_unknown_tool_is_rejected():
    gateway = ToolGateway(AiConfig())
    with pytest.raises(UnknownToolError):
        await gateway.call("definitely_not_a_tool", {}, requester="ai")


async def test_ai_flatten_is_disabled_by_configuration():
    runtime = _runtime()
    assert runtime.config.ai.permissions.allow_flatten is False
    with pytest.raises(ToolPermissionError):
        await runtime.gateway.call("request_flatten", {}, requester="ai")


async def test_read_only_tool_is_permitted():
    runtime = _runtime()
    result = await runtime.gateway.call("get_system_status", {}, requester="ai")
    assert isinstance(result, dict)


async def test_disallowed_control_tool_is_rejected():
    config = AiConfig()
    config.permissions.allow_pause = False
    gateway = ToolGateway(config)
    gateway.register(
        ToolSpec(
            name="pause_strategy",
            permission=Permission.LOW_RISK_CONTROL,
            description="pause",
            handler=lambda: None,
            config_key="allow_pause",
        )
    )
    with pytest.raises(ToolPermissionError):
        await gateway.call("pause_strategy", {}, requester="ai")


async def test_unknown_arguments_are_rejected():
    config = AiConfig()
    gateway = ToolGateway(config)

    async def _handler(**kwargs):
        return kwargs

    gateway.register(
        ToolSpec(
            name="read_thing",
            permission=Permission.READ_ONLY,
            description="read",
            handler=_handler,
            parameters={"type": "object", "properties": {"symbol": {"type": "string"}}},
        )
    )
    with pytest.raises(ToolPermissionError):
        await gateway.call("read_thing", {"symbol": "BTC/USD", "extra": 1}, requester="ai")


async def test_supervisor_rejects_unknown_and_malformed_tool_calls():
    runtime = _runtime()
    out = await runtime.supervisor._execute_tool({"name": "ghost_tool", "arguments": "{}"})
    assert "error" in out
    out = await runtime.supervisor._execute_tool({"name": "get_positions", "arguments": "{not json"})
    assert "error" not in out  # malformed JSON degrades to empty args, not a crash


async def test_ai_unavailable_still_allows_deterministic_control():
    runtime = _runtime()
    await runtime.supervisor.start()
    assert runtime.engine.state.ai["status"] in ("unavailable", "disabled")
    # The deterministic gateway still enforces permissions regardless of AI health.
    with pytest.raises(ToolPermissionError):
        await runtime.gateway.call("request_flatten", {}, requester="ai")
