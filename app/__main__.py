"""``python -m app`` — start the API server, dashboard, and trading engine."""
from __future__ import annotations


def main() -> None:
    import uvicorn

    from app.main import app

    config = app.state.config
    uvicorn.run(
        app,
        host=config.dashboard.host,
        port=config.dashboard.port,
        reload=False,
        log_level=config.logging.level.lower(),
    )


if __name__ == "__main__":
    main()
