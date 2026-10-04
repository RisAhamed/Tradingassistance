"""FastAPI application factory and entry point.

Run with:  python -m app   (or)   uvicorn app.main:app --reload
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.routes import create_router
from app.config.loader import PROJECT_ROOT, get_env, load_config
from app.config.models import AppConfig
from app.config.settings import EnvSettings
from app.core.logging import configure_logging

logger = logging.getLogger("app.main")


def create_app(
    config: AppConfig | None = None,
    env: EnvSettings | None = None,
    *,
    autostart: bool | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    ``autostart`` (defaults to ``config.trading.autostart``) controls whether the
    trading engine starts with the web server.
    """
    env = env or get_env()
    config = config or load_config(env=env)
    configure_logging(config, env, project_root=PROJECT_ROOT)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from app.runtime import build_runtime

        app.state.config = config
        app.state.env = env
        app.state.startup_error = None
        app.state.runtime = None

        should_start = config.trading.autostart if autostart is None else autostart
        try:
            runtime = build_runtime(config, env)
        except Exception as exc:  # noqa: BLE001 - surfaced via /health
            app.state.startup_error = str(exc)
            logger.critical(
                "runtime build failed (trading engine NOT started)",
                extra={"structured": {"event": "SYSTEM_ERROR", "component": "main", "error": str(exc)}},
            )
            yield
            return

        app.state.runtime = runtime
        if should_start:
            try:
                await runtime.startup()
            except Exception as exc:  # noqa: BLE001 - engine must not start, API stays up
                app.state.startup_error = str(exc)
                logger.critical(
                    "trading engine startup failed (API still serving)",
                    extra={"structured": {"event": "SYSTEM_ERROR", "component": "main", "error": str(exc)}},
                )
        else:
            logger.info(
                "trading engine autostart disabled",
                extra={"structured": {"event": "CONFIGURATION_LOADED", "component": "main", "note": "autostart=false"}},
            )
        try:
            yield
        finally:
            await runtime.shutdown()

    app = FastAPI(
        title="Trading Agent",
        version=__version__,
        description="Systematic PAPER-TRADING agent (Alpaca paper broker + Ollama supervision).",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.env = env
    app.include_router(create_router())
    return app


app = create_app()


def main() -> None:
    """Console-script entry point (``trading-agent``)."""
    import uvicorn

    config = app.state.config
    uvicorn.run(
        "app.main:app",
        host=config.dashboard.host,
        port=config.dashboard.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
