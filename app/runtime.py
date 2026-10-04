"""Application runtime: wires everything together and runs the startup checklist.

Startup order follows the spec: configuration -> environment -> paper mode ->
credentials -> connectivity -> session/risk config -> SYSTEM READY.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.ai_supervisor.gateway import ToolGateway
from app.ai_supervisor.provider import AIProvider, OllamaProvider, UnavailableProvider
from app.ai_supervisor.supervisor import AISupervisor
from app.ai_supervisor.tools import build_gateway
from app.config.models import AppConfig
from app.config.settings import EnvSettings
from app.core.errors import ConfigError, PaperOnlyViolation
from app.events.bus import EventBus
from app.runner.engine import TradingEngine
from app.runner.factories import create_broker, create_provider, create_storage
from app.storage.db import Database
from app.storage.repository import Repository

logger = logging.getLogger("app.runtime")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    mandatory: bool = True


@dataclass
class Runtime:
    config: AppConfig
    env: EnvSettings
    bus: EventBus
    repository: Repository
    engine: TradingEngine
    ai_provider: AIProvider
    gateway: ToolGateway
    supervisor: AISupervisor
    database: Database | None = None
    ready: bool = False
    checklist: list[Check] = field(default_factory=list)

    async def startup(self) -> None:
        self.checklist = self._static_checks()
        self._log_checks()

        if not all(c.ok for c in self.checklist if c.mandatory):
            raise PaperOnlyViolation("startup checklist failed (static)")

        try:
            await self.engine.start()
        except (ConfigError, PaperOnlyViolation):
            raise
        except Exception as exc:  # noqa: BLE001 - connectivity failure blocks trading
            logger.critical(
                "start-up connectivity failure",
                extra={"structured": {"event": "SYSTEM_ERROR", "component": "runtime", "error": str(exc)}},
            )
            self.checklist.append(Check("market_data", False, str(exc)))
            self._log_checks()
            raise

        self.checklist.extend(await self._connectivity_checks())
        await self.supervisor.start()
        self.checklist.append(Check("ollama", await self._ai_ok(), "optional", mandatory=False))

        self.ready = all(c.ok for c in self.checklist if c.mandatory)
        self._log_checks()
        if not self.ready:
            logger.critical("SYSTEM NOT READY", extra={"structured": {"event": "SYSTEM_ERROR", "component": "runtime"}})
            raise RuntimeError("startup checklist failed (connectivity)")
        logger.info("SYSTEM READY", extra={"structured": {"event": "SYSTEM_READY", "component": "runtime"}})

    async def shutdown(self) -> None:
        try:
            await self.engine.stop()
        except Exception:  # noqa: BLE001
            logger.exception("engine shutdown error")
        if self.database is not None:
            try:
                await self.database.dispose()
            except Exception:  # noqa: BLE001
                logger.exception("database shutdown error")
        logger.info("RUNTIME STOPPED", extra={"structured": {"event": "RUNTIME_STOPPED", "component": "runtime"}})

    # -- checks -------------------------------------------------------------
    def _static_checks(self) -> list[Check]:
        needs_alpaca = self.config.trading.broker == "alpaca" or self.config.market_data.provider == "alpaca"
        session = self.config.session
        risk = self.config.risk
        return [
            Check("configuration", True, self.config.application.name),
            Check("environment", bool(self.env.app_env), self.env.app_env),
            Check("paper_mode", self.config.trading.mode == "paper" and self.env.alpaca_paper, "PAPER_TRADING_ONLY"),
            Check(
                "alpaca_credentials",
                (self.env.has_alpaca_credentials() if needs_alpaca else True),
                "present" if (not needs_alpaca or self.env.has_alpaca_credentials()) else "missing",
            ),
            Check(
                "session_configuration",
                session.enabled and session.start_time <= session.entry_cutoff_time <= session.flatten_deadline_time <= session.end_time,
                f"{session.start}-{session.end} cutoff={session.entry_cutoff} flatten={session.flatten_deadline}",
            ),
            Check(
                "risk_configuration",
                risk.enabled
                and risk.risk_per_trade_percent > 0
                and risk.maximum_daily_loss_percent > 0
                and risk.maximum_open_positions >= 1,
                f"risk_per_trade={risk.risk_per_trade_percent}% max_daily_loss={risk.maximum_daily_loss_percent}%",
            ),
        ]

    async def _connectivity_checks(self) -> list[Check]:
        provider_health = self.engine.provider.health()
        broker_health = self.engine.broker.health()
        checks = [
            Check("market_data", provider_health.connected, provider_health.detail),
            Check("broker", broker_health.connected, broker_health.detail),
        ]
        if self.database is not None and self.config.storage.enabled:
            checks.append(Check("database", await self.database.ping(), "SELECT 1"))
        else:
            checks.append(Check("database", True, "disabled", mandatory=False))
        return checks

    async def _ai_ok(self) -> bool:
        if not self.config.ai.enabled:
            return True
        return await self.ai_provider.health()

    # -- reporting ----------------------------------------------------------
    def _log_checks(self) -> None:
        for index, check in enumerate(self.checklist, start=1):
            mark = "OK" if check.ok else "FAIL"
            level = logging.INFO if check.ok else logging.ERROR
            suffix = "" if check.mandatory else " (optional)"
            logger.log(
                level,
                f"[{index}] {check.name} = {mark}{suffix}  {check.detail}",
                extra={
                    "structured": {
                        "event": "STARTUP_CHECK",
                        "component": "runtime",
                        "index": index,
                        "check": check.name,
                        "status": mark,
                        "detail": check.detail,
                        "mandatory": check.mandatory,
                    }
                },
            )


def build_ai_provider(config: AppConfig, env: EnvSettings) -> AIProvider:
    if not config.ai.enabled:
        return UnavailableProvider()
    return OllamaProvider(
        config.ai.endpoint.base_url,
        api_key=env.ollama_api_key,
        model=config.ai.model.name or env.ollama_model,
        temperature=config.ai.model.temperature,
        max_tokens=config.ai.model.max_tokens,
        timeout=float(config.ai.limits.timeout_seconds),
    )


def build_runtime(config: AppConfig, env: EnvSettings) -> Runtime:
    """Construct the fully wired runtime (no I/O yet)."""
    bus = EventBus()
    provider = create_provider(config, env)
    broker = create_broker(config, env)
    database, repository = create_storage(config, env)
    engine = TradingEngine(
        config,
        env,
        provider=provider,
        broker=broker,
        bus=bus,
        repository=repository,
    )
    gateway = build_gateway(engine)
    ai_provider = build_ai_provider(config, env)
    supervisor = AISupervisor(config, ai_provider, gateway, engine, repository=repository)
    return Runtime(
        config=config,
        env=env,
        bus=bus,
        repository=repository,
        engine=engine,
        ai_provider=ai_provider,
        gateway=gateway,
        supervisor=supervisor,
        database=database,
    )


__all__ = ["Runtime", "Check", "build_runtime", "build_ai_provider"]
