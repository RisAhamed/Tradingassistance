"""Builds the read-only + controlled tool set bound to the live engine."""
from __future__ import annotations

from typing import Any

from app.ai_supervisor.gateway import ToolGateway, ToolSpec
from app.core.clock import utcnow
from app.domain.enums import Permission


def build_gateway(engine) -> ToolGateway:
    gateway = ToolGateway(engine.config.ai, requester="ai")

    # ---- READ_ONLY tools ---------------------------------------------------
    async def get_system_status() -> dict[str, Any]:
        return engine.payload()["system"]

    async def get_market_state() -> dict[str, Any]:
        return engine.payload().get("market")

    async def get_features() -> dict[str, Any]:
        return engine.payload().get("features")

    async def get_regime() -> dict[str, Any]:
        data = engine.payload()
        return {"current": data.get("regime"), "history": data.get("regime_history")}

    async def get_strategy_status() -> dict[str, Any]:
        return engine.payload().get("strategy")

    async def get_positions() -> dict[str, Any]:
        return engine.payload().get("position")

    async def get_orders() -> dict[str, Any]:
        return engine.payload().get("orders")

    async def get_account() -> dict[str, Any]:
        risk = engine.payload().get("risk", {})
        return {
            "equity": risk.get("account_equity"),
            "exposure": risk.get("exposure"),
            "open_positions": risk.get("open_positions"),
        }

    async def get_daily_pnl() -> dict[str, Any]:
        return engine.payload().get("pnl")

    async def get_recent_trades() -> dict[str, Any]:
        return {"trades": engine.payload().get("trades", [])}

    async def get_recent_events() -> dict[str, Any]:
        return {"events": engine.bus.recent_public(50)}

    async def get_risk_status() -> dict[str, Any]:
        return engine.payload().get("risk")

    async def get_execution_metrics() -> dict[str, Any]:
        risk = engine.payload().get("risk", {})
        orders = engine.payload().get("orders", [])
        filled = sum(1 for o in orders if o.get("status") == "filled")
        rejected = sum(1 for o in orders if o.get("status") == "rejected")
        return {
            "orders_total": len(orders),
            "orders_filled": filled,
            "orders_rejected": rejected,
            "orders_this_session": risk.get("orders_this_session"),
            "session_state": (engine.payload().get("session") or {}).get("state"),
            "persistence_failures": engine.repository.failures if engine.repository else 0,
        }

    read_only = [
        ("get_system_status", "System status, components and uptime.", get_system_status),
        ("get_market_state", "Latest normalized market snapshot.", get_market_state),
        ("get_features", "Latest feature snapshot.", get_features),
        ("get_regime", "Current regime and recent transitions.", get_regime),
        ("get_strategy_status", "Strategy name, last signal and recent signals.", get_strategy_status),
        ("get_positions", "Current position state.", get_positions),
        ("get_orders", "Recent orders.", get_orders),
        ("get_account", "Account equity and exposure.", get_account),
        ("get_daily_pnl", "Realized/unrealized P&L.", get_daily_pnl),
        ("get_recent_trades", "Recently closed trades.", get_recent_trades),
        ("get_recent_events", "Recent system events.", get_recent_events),
        ("get_risk_status", "Risk status and last decision.", get_risk_status),
        ("get_execution_metrics", "Execution and order metrics.", get_execution_metrics),
    ]
    for name, description, handler in read_only:
        gateway.register(
            ToolSpec(name=name, permission=Permission.READ_ONLY, description=description, handler=handler)
        )

    # ---- controlled tools --------------------------------------------------
    async def pause_strategy() -> dict[str, Any]:
        await engine.pause(source="ai", reason="ai_pause")
        return {"paused": True, "state": engine.session.state.value}

    async def resume_strategy() -> dict[str, Any]:
        await engine.resume(source="ai")
        return {"resumed": True, "state": engine.session.state.value}

    async def request_reconciliation() -> dict[str, Any]:
        ok = await engine.request_reconciliation(source="ai")
        return {"reconciled": ok, "reconciliation": engine.state.reconciliation}

    async def request_flatten() -> dict[str, Any]:
        success = await engine.flatten(force=True, session_closeout=False)
        return {"flattened": success, "position": engine.position_manager.is_flat}

    gateway.register(
        ToolSpec(
            name="pause_strategy",
            permission=Permission.LOW_RISK_CONTROL,
            description="Pause new entries (session PAUSED).",
            handler=pause_strategy,
            config_key="allow_pause",
        )
    )
    gateway.register(
        ToolSpec(
            name="resume_strategy",
            permission=Permission.LOW_RISK_CONTROL,
            description="Resume the trading session.",
            handler=resume_strategy,
            config_key="allow_resume",
        )
    )
    gateway.register(
        ToolSpec(
            name="request_reconciliation",
            permission=Permission.LOW_RISK_CONTROL,
            description="Reconcile internal state against the broker.",
            handler=request_reconciliation,
            config_key="allow_reconciliation",
        )
    )
    gateway.register(
        ToolSpec(
            name="request_flatten",
            permission=Permission.HIGH_RISK_CONTROL,
            description="Close all open positions and verify flatness.",
            handler=request_flatten,
            config_key="allow_flatten",
        )
    )
    return gateway
