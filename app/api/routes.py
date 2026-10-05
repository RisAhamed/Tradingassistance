"""REST API + SSE endpoints (structured JSON responses)."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.config.loader import PROJECT_ROOT, sanitized_config

logger = logging.getLogger("app.api")

DASHBOARD_DIR = PROJECT_ROOT / "dashboard"


def _runtime(request: Request):
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="runtime not initialised")
    return runtime


def create_router() -> APIRouter:
    router = APIRouter()

    # -- health -------------------------------------------------------------
    @router.get("/health")
    async def health(request: Request):
        runtime = getattr(request.app.state, "runtime", None)
        error = getattr(request.app.state, "startup_error", None)
        if runtime is None:
            return {"status": "error", "ready": False, "paper_trading_only": True, "error": error or "not built"}
        report = runtime.engine.health.report()
        return {
            "status": report["status"] if runtime.ready else "error",
            "ready": runtime.ready and report["ready"],
            "paper_trading_only": True,
            "error": error,
            "checks": report["checks"],
        }

    @router.get("/health/ready")
    async def health_ready(request: Request):
        runtime = getattr(request.app.state, "runtime", None)
        if runtime is None:
            raise HTTPException(status_code=503, detail=getattr(request.app.state, "startup_error", "not built"))
        report = runtime.engine.health.report()
        ready = runtime.ready and report["ready"]
        if not ready:
            raise HTTPException(status_code=503, detail=report)
        return {"status": "ready", "checks": report["checks"]}

    @router.get("/health/live")
    async def health_live():
        return {"status": "alive"}

    # -- system / market ----------------------------------------------------
    @router.get("/api/system/status")
    async def system_status(request: Request):
        return _runtime(request).engine.payload()["system"]

    @router.get("/api/market/latest")
    async def market_latest(request: Request):
        return _runtime(request).engine.payload().get("market")

    @router.get("/api/features")
    async def features(request: Request):
        return _runtime(request).engine.payload().get("features")

    @router.get("/api/regime")
    async def regime(request: Request):
        payload = _runtime(request).engine.payload()
        return {"current": payload.get("regime"), "history": payload.get("regime_history")}

    @router.get("/api/strategy/status")
    async def strategy_status(request: Request):
        return _runtime(request).engine.payload().get("strategy")

    @router.get("/api/risk/status")
    async def risk_status(request: Request):
        payload = _runtime(request).engine.payload()
        return {"risk": payload.get("risk"), "rejections": payload.get("rejections")}

    @router.get("/api/orders")
    async def orders(request: Request):
        return {"orders": _runtime(request).engine.payload().get("orders", [])}

    @router.get("/api/positions")
    async def positions(request: Request):
        return {"position": _runtime(request).engine.payload().get("position")}

    @router.get("/api/pnl")
    async def pnl(request: Request):
        payload = _runtime(request).engine.payload()
        return {"pnl": payload.get("pnl"), "trades": payload.get("trades", [])}

    @router.get("/api/events")
    async def events(request: Request, limit: int = 100):
        return {"events": _runtime(request).bus.recent_public(limit)}

    @router.get("/api/logs")
    async def logs(request: Request, limit: int = 100):
        runtime = _runtime(request)
        path = PROJECT_ROOT / runtime.config.logging.file.path
        lines: list[dict] = []
        if path.exists():
            def _read() -> list[str]:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    return handle.readlines()[-limit:]
            raw = await asyncio.to_thread(_read)
            for line in raw:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    lines.append({"message": line, "level": "INFO"})
        return {"logs": lines, "errors": list(runtime.engine.state.errors)}

    @router.get("/api/session")
    async def session(request: Request):
        runtime = _runtime(request)
        payload = runtime.engine.payload()
        return {
            "session": payload.get("session"),
            "summary": runtime.engine.session.build_summary(
                _now(), realized=runtime.engine.pnl.realized, fees=runtime.engine.pnl.fees
            ).model_dump(mode="json"),
        }

    @router.get("/api/ai/status")
    async def ai_status(request: Request):
        runtime = _runtime(request)
        return {
            "enabled": runtime.config.ai.enabled,
            "provider": runtime.config.ai.provider,
            "model": runtime.ai_provider.model if hasattr(runtime.ai_provider, "model") else "",
            "tools": runtime.gateway.names(),
            "permissions": runtime.config.ai.permissions.model_dump(mode="json"),
            **runtime.engine.payload().get("ai", {}),
        }

    @router.get("/api/execution")
    async def execution_status(request: Request):
        runtime = _runtime(request)
        payload = runtime.engine.payload()
        return {"execution": payload.get("execution"), "paper_trading_only": True}

    @router.get("/api/market/status")
    async def market_status(request: Request):
        runtime = _runtime(request)
        engine = runtime.engine
        snapshot = engine.state.latest_snapshot
        provider_health = engine.provider.health()
        age = snapshot.age_seconds(_now()) if snapshot else None
        limit = runtime.config.market_data.freshness.threshold_seconds
        candles: dict[str, object] = {}
        for timeframe in engine.timeframes:
            current = engine.aggregator.current(timeframe)
            candles[timeframe] = current.model_dump(mode="json") if current else None
        return {
            "provider": engine.provider.name,
            "connected": provider_health.connected,
            "detail": provider_health.detail,
            "symbol": engine.symbol,
            "bid": snapshot.bid if snapshot else None,
            "ask": snapshot.ask if snapshot else None,
            "last": snapshot.last if snapshot else None,
            "price": snapshot.price if snapshot else None,
            "spread_percent": snapshot.spread_percent if snapshot else None,
            "data_age_seconds": age,
            "max_age_seconds": limit,
            "data_status": "STALE" if age is None or age > limit else "LIVE",
            "candles": candles,
            "streams": engine.stream_state(_now()),
            "bars": {
                "enabled": runtime.config.market_data.bars.enabled,
                "canonical": runtime.config.market_data.bars.canonical,
                "timeframe": runtime.config.market_data.bars.timeframe,
                "max_gap_candles": runtime.config.market_data.bars.max_gap_candles,
            },
        }

    @router.get("/api/warmup")
    async def warmup_status(request: Request):
        runtime = _runtime(request)
        engine = runtime.engine
        return {
            "warmup": engine.warmup_status(),
            "freshness": runtime.config.market_data.freshness.model_dump(mode="json"),
            "risk_stale_threshold_seconds": runtime.config.risk.stale_market_data.maximum_age_seconds,
            "history": runtime.config.market_data.history.model_dump(mode="json"),
        }

    @router.get("/api/readiness")
    async def readiness(request: Request):
        runtime = _runtime(request)
        engine = runtime.engine
        return {
            "readiness": engine.readiness(),
            "streams": engine.stream_state(),
            "coverage": engine.coverage,
            "recovery": engine.recovery,
            "execution": engine.execution_status(),
            "bars": runtime.config.market_data.bars.model_dump(mode="json"),
            "history": runtime.config.market_data.history.model_dump(mode="json"),
            "freshness": runtime.config.market_data.freshness.model_dump(mode="json"),
        }

    @router.get("/api/config")
    async def config_endpoint(request: Request):
        runtime = _runtime(request)
        return sanitized_config(runtime.config, runtime.env)

    # -- Phase D: decision observability -----------------------------------
    @router.get("/api/decision")
    async def decision(request: Request):
        runtime = _runtime(request)
        payload = runtime.engine.payload()
        return {
            "freshness_policy": payload.get("freshness_policy"),
            "timeframe_selection": payload.get("timeframe_selection"),
            "trade_plan": payload.get("trade_plan"),
        }

    @router.get("/api/trade-plan")
    async def trade_plan(request: Request):
        runtime = _runtime(request)
        return runtime.engine.payload().get("trade_plan")

    @router.get("/api/timeframes")
    async def timeframes(request: Request):
        runtime = _runtime(request)
        return runtime.engine.payload().get("timeframe_selection")

    @router.get("/api/stream")
    async def stream(request: Request):
        runtime = _runtime(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)

        def handler(event) -> None:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        runtime.bus.subscribe_all(handler)

        async def generator():
            try:
                yield "event: hello\ndata: {}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(event.to_public())}\n\n"
            finally:
                runtime.bus.remove_all(handler)

        return StreamingResponse(generator(), media_type="text/event-stream")

    # -- human control (same command layer as AI) ---------------------------
    @router.post("/api/control/pause")
    async def control_pause(request: Request, reason: str = "human_pause"):
        runtime = _runtime(request)
        await runtime.engine.pause(source="human", reason=reason)
        logger.info(
            "human pause",
            extra={"structured": {"event": "AI_ACTION_REQUESTED", "component": "api", "action": "pause", "requester": "human"}},
        )
        return {"state": runtime.engine.session.state.value, "entries_allowed": runtime.engine.session.entries_allowed}

    @router.post("/api/control/resume")
    async def control_resume(request: Request):
        runtime = _runtime(request)
        await runtime.engine.resume(source="human")
        return {"state": runtime.engine.session.state.value, "entries_allowed": runtime.engine.session.entries_allowed}

    @router.post("/api/control/flatten")
    async def control_flatten(request: Request):
        runtime = _runtime(request)
        logger.warning(
            "human flatten requested",
            extra={"structured": {"event": "AI_ACTION_REQUESTED", "component": "api", "action": "flatten", "requester": "human"}},
        )
        success = await runtime.engine.flatten(force=True, session_closeout=False)
        return {"flattened": success, "position_flat": runtime.engine.position_manager.is_flat}

    @router.post("/api/control/reconcile")
    async def control_reconcile(request: Request):
        runtime = _runtime(request)
        ok = await runtime.engine.request_reconciliation(source="human")
        return {"reconciled": ok, "reconciliation": runtime.engine.state.reconciliation}

    # -- dashboard ----------------------------------------------------------
    @router.get("/dashboard")
    async def dashboard():
        path = DASHBOARD_DIR / "index.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="dashboard not built")
        return FileResponse(path)

    return router


def _now():
    from app.core.clock import utcnow

    return utcnow()
