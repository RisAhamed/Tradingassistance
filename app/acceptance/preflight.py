"""Preflight safety checks (Phase J).

Every check records {name, ok, detail, timestamp}. ANY failure blocks the
run (exit 2) before an order can exist. No check submits anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str = ""
    timestamp: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "timestamp": self.timestamp}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check(name: str, ok: bool, detail: str = "") -> PreflightCheck:
    return PreflightCheck(name=name, ok=bool(ok), detail=str(detail), timestamp=_now())


async def run_preflight(config, env, engine=None) -> list[PreflightCheck]:
    """Run all 11 safety checks. Order-free by construction.

    ``engine`` is the runtime engine built AFTER the repository checks but
    BEFORE the in-process gate opens, so checks (j)/(k) assert real local
    state rather than an assumption.
    """
    from app.brokers.alpaca import AlpacaPaperBroker

    checks: list[PreflightCheck] = []
    checks.append(_check("trading_mode_paper", config.trading.mode == "paper",
                         f"mode={config.trading.mode}"))
    checks.append(_check("env_alpaca_paper", env.alpaca_paper is True,
                         f"alpaca_paper={env.alpaca_paper}"))
    live = env.is_live_alpaca_endpoint()
    checks.append(_check("not_live_endpoint", live is False,
                         "live endpoint detected" if live else "paper endpoint"))
    checks.append(_check("has_credentials", env.has_alpaca_credentials(),
                         "credentials present" if env.has_alpaca_credentials() else "missing"))
    checks.append(_check("repository_execution_disabled", config.execution.enabled is False,
                         f"execution.enabled={config.execution.enabled}"))
    checks.append(_check("repository_paper_safety_disabled",
                         config.paper_safety.enabled is False,
                         f"paper_safety.enabled={config.paper_safety.enabled}"))
    broker = AlpacaPaperBroker(env.alpaca_api_key, env.alpaca_api_secret,
                               feed=config.market_data.feed)
    checks.append(_check("broker_is_paper", broker.paper is True and broker.name == "alpaca",
                         f"broker={broker.name} paper={broker.paper}"))
    try:
        await broker.connect()
        account = await broker.get_account()
        checks.append(_check("broker_reachable", True,
                             f"equity={round(account.equity, 2)}"))
    except Exception as exc:  # noqa: BLE001 - preflight must report, not raise
        checks.append(_check("broker_reachable", False, f"{type(exc).__name__}: {exc}"))
        await broker.disconnect()
        return checks
    try:
        positions = await broker.get_positions()
        mine = [p for p in positions if p.symbol.replace("/", "") == config.trading.symbol.replace("/", "")]
        checks.append(_check("broker_position_flat", not mine,
                             f"positions={[p.symbol for p in positions]}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("broker_position_flat", False, f"{type(exc).__name__}: {exc}"))
    finally:
        await broker.disconnect()

    # Local state is asserted against the real engine instance built for
    # this run (gate still closed), never assumed.
    if engine is None:
        checks.append(_check("local_position_flat", False, "no engine built"))
        checks.append(_check("local_oms_empty", False, "no engine built"))
    else:
        checks.append(_check("local_position_flat", engine.position_manager.is_flat,
                             f"flat={engine.position_manager.is_flat}"))
        checks.append(_check("local_oms_empty", not engine.oms.all_orders(),
                             f"orders={len(engine.oms.all_orders())}"))
    return checks
