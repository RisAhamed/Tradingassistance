"""Async database engine setup. PostgreSQL-ready, SQLite for dev/tests."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.models import StorageConfig
from app.storage.models import Base

logger = logging.getLogger("app.storage.db")


class Database:
    def __init__(self, config: StorageConfig, *, project_root: Path | None = None) -> None:
        self.config = config
        self.url = self._resolve_url(config, project_root)
        self._engine = create_async_engine(self.url, echo=config.echo, future=True)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def session_factory(self) -> async_sessionmaker:
        return self._sessionmaker

    @staticmethod
    def _resolve_url(config: StorageConfig, project_root: Path | None) -> str:
        if config.url:
            return config.url
        path = Path(config.sqlite_path)
        if project_root and not path.is_absolute():
            path = project_root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path.as_posix()}"

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self):
        async with self._sessionmaker() as session:  # type: AsyncSession
            yield session

    async def ping(self) -> bool:
        from sqlalchemy import text

        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 - reported via health
            logger.error(
                "database ping failed",
                extra={"structured": {"event": "DB_PING_FAILED", "component": "storage.db", "error": str(exc)}},
            )
            return False

    async def dispose(self) -> None:
        await self._engine.dispose()
