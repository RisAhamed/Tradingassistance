"""Builds the read-only + controlled tool set bound to the live engine."""
from __future__ import annotations

from typing import Any

from app.ai_supervisor.gateway import ToolGateway, ToolSpec
from app.core.clock import utcnow
from app.core.errors import ToolPermissionError
from app.domain.enums import Permission
from app.events.types import EventType


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

    async def get_sentiment() -> dict[str, Any] | None:
        """Observation-only sentiment snapshot (read-only, never trades)."""
        return engine.payload().get("sentiment")

    async def get_strategy_selection() -> dict[str, Any] | None:
        """Read-only strategy selection snapshot; never affects trading."""
        return engine.payload().get("strategy_selection")

    async def get_signal_confidence() -> dict[str, Any] | None:
        """Read-only last-signal conviction; never affects trading."""
        signal = (engine.payload().get("strategy") or {}).get("last_signal")
        if signal is None:
            return None
        return {
            "signal_id": signal.get("signal_id"),
            "strategy": signal.get("strategy"),
            "confidence": signal.get("confidence"),
            "confidence_components": signal.get("confidence_components"),
        }

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
        ("get_sentiment", "Observation-only sentiment snapshot (never trades).", get_sentiment),
        ("get_strategy_selection", "Read-only strategy selection snapshot (never trades).", get_strategy_selection),
        ("get_signal_confidence", "Read-only last-signal conviction score and components (never trades).", get_signal_confidence),
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

    # ---- Phase I: proposal lifecycle --------------------------------------
    async def _record_proposal_action(action: str, tool: str, payload: dict[str, Any]) -> None:
        if engine.repository is None:
            return
        try:
            await engine.repository.save_ai_action(
                session_id=getattr(engine.session, "session_id", None),
                action=action, tool=tool, permitted=True, payload=payload)
        except Exception:  # noqa: BLE001 - observability must not break tools
            pass

    async def list_proposals() -> list[dict[str, Any]]:
        """List pending + applied + rejected proposals (read-only)."""
        from app.optimization.apply import list_proposals as _list

        return _list(engine)

    async def get_proposal(proposal_id: str) -> dict[str, Any] | None:
        """Full detail for one proposal (read-only)."""
        from app.optimization.apply import find_proposal

        found = find_proposal(engine, proposal_id)
        if found is None:
            return None
        proposal, path = found
        return {"proposal_id": proposal_id, "state": path.parent.name, "proposal": proposal}

    async def propose_strategy_change(summary: str, params: dict, rationale: str) -> dict[str, Any]:
        """Write an AI-authored candidate proposal into pending_dir.

        LOW_RISK_CONTROL gated by allow_propose_strategy_changes. Writes
        pending JSON only — never the overlay, never configs/, no restart.
        """
        from app.optimization.apply import _dirs
        from app.optimization.proposals import (
            build_ai_proposal,
            validate_override_params,
            write_pending_proposal,
        )

        try:
            validated = validate_override_params(engine.config, params)
        except ValueError as exc:
            raise ToolPermissionError(f"proposal refused: {exc}") from exc
        model = engine.config.ai.model.name or "unknown"
        proposal = build_ai_proposal(summary=summary, params=validated,
                                     rationale=rationale, model=model)
        proposal_id, path = write_pending_proposal(proposal, _dirs(engine)["pending"])
        await engine.bus.emit(EventType.AI_PROPOSAL_CREATED,
                              payload={"proposal_id": proposal_id, "model": model})
        await _record_proposal_action("ai_proposal_created", "propose_strategy_change",
                                      {"proposal_id": proposal_id})
        return {"proposal_id": proposal_id, "path": str(path), "origin": "ai"}

    async def apply_proposal(proposal_id: str) -> dict[str, Any]:
        """Apply an approved proposal via the shared human routine.

        HIGH_RISK_CONTROL gated by allow_apply_proposal (default False).
        """
        from app.optimization import apply as _apply_mod

        if not engine.config.ai.permissions.allow_apply_proposal:
            await engine.bus.emit(
                EventType.AI_PROPOSAL_APPLICATION_REFUSED,
                payload={"proposal_id": proposal_id, "reason": "allow_apply_proposal=false"})
            raise ToolPermissionError("apply_proposal not permitted (allow_apply_proposal=false)")
        return await _apply_mod.apply_proposal(engine, proposal_id, actor="ai:supervisor")

    gateway.register(
        ToolSpec(name="list_proposals", permission=Permission.READ_ONLY,
                 description="List pending/applied/rejected optimizer proposals.",
                 handler=list_proposals)
    )
    gateway.register(
        ToolSpec(name="get_proposal", permission=Permission.READ_ONLY,
                 description="Full detail for one optimizer proposal.",
                 handler=get_proposal)
    )
    gateway.register(
        ToolSpec(
            name="propose_strategy_change",
            permission=Permission.LOW_RISK_CONTROL,
            description="Write a candidate strategy proposal to pending/ for human review.",
            handler=propose_strategy_change,
            config_key="allow_propose_strategy_changes",
            parameters={"type": "object", "properties": {
                "summary": {"type": "string"},
                "params": {"type": "object"},
                "rationale": {"type": "string"}},
                "required": ["summary", "params", "rationale"]},
        )
    )
    gateway.register(
        ToolSpec(
            name="apply_proposal",
            permission=Permission.HIGH_RISK_CONTROL,
            description="Apply an approved proposal through the shared routine (restart required).",
            handler=apply_proposal,
            config_key="allow_apply_proposal",
            parameters={"type": "object", "properties": {"proposal_id": {"type": "string"}},
                        "required": ["proposal_id"]},
        )
    )
    return gateway
