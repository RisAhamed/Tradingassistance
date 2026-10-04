"""Load and validate the central configuration, merging env + YAML."""
from __future__ import annotations

import copy
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config.models import AppConfig
from app.config.settings import EnvSettings
from app.core.errors import ConfigError, PaperOnlyViolation

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_path(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate


def load_config(
    path: str | os.PathLike[str] | None = None,
    env: EnvSettings | None = None,
) -> AppConfig:
    """Load ``config.yaml``, apply environment overrides, and validate.

    Precedence: env override > YAML > safe default (never for critical params).
    Raises :class:`ConfigError` on any problem so startup can fail fast, and
    :class:`PaperOnlyViolation` if a live endpoint is detected.
    """
    env = env or EnvSettings()
    config_path = _resolve_path(path or env.config_path)

    if not config_path.exists():
        raise ConfigError(f"configuration file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")

    data: dict[str, Any] = copy.deepcopy(raw)
    _apply_env_overrides(data, env)

    try:
        config = AppConfig.model_validate(data)
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"invalid configuration: {exc}") from exc

    _enforce_paper_only(config, env)
    return config


def _apply_env_overrides(data: dict[str, Any], env: EnvSettings) -> None:
    """Apply the small set of intentionally-supported env overrides."""
    if env.trading_mode:
        data.setdefault("trading", {})["mode"] = env.trading_mode

    if env.alpaca_data_feed:
        data.setdefault("market_data", {})["feed"] = env.alpaca_data_feed

    if env.database_url:
        data.setdefault("storage", {})["url"] = env.database_url

    ai = data.setdefault("ai", {})
    if env.ollama_base_url:
        ai.setdefault("endpoint", {})["base_url"] = env.ollama_base_url
    if env.ollama_model:
        ai.setdefault("model", {})["name"] = env.ollama_model

    # The application environment can be pinned by APP_ENV.
    if env.app_env:
        data.setdefault("application", {})["environment"] = env.app_env


def _enforce_paper_only(config: AppConfig, env: EnvSettings) -> None:
    """Fail safe if anything smells like live trading."""
    if config.trading.mode != "paper":
        raise PaperOnlyViolation("PAPER_TRADING_ONLY: trading.mode must be 'paper'")
    if env.alpaca_paper is False:
        raise PaperOnlyViolation("PAPER_TRADING_ONLY: ALPACA_PAPER must be true")
    if env.is_live_alpaca_endpoint():
        raise PaperOnlyViolation(
            "PAPER_TRADING_ONLY: a live Alpaca endpoint was detected; refusing to start"
        )


def sanitized_config(config: AppConfig, env: EnvSettings) -> dict[str, Any]:
    """Return a secret-free, JSON-safe view of the effective configuration."""
    payload = config.model_dump(mode="json")
    payload["env"] = env.masked()
    return payload


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Cached configuration accessor used by the FastAPI app factory."""
    return load_config()


def get_env() -> EnvSettings:
    """Environment/secrets accessor (not cached so tests can override)."""
    return EnvSettings()
