"""Environment-backed settings — the SECRETS and machine-specific layer.

Secrets and connection strings are read from the process environment and/or the
project ``.env`` file (never committed). Non-secret behaviour lives in
``configs/config.yaml`` (see :mod:`app.config.loader`).
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Endpoints that indicate a LIVE Alpaca account; their presence is a fail-safe
# trigger. Paper endpoints are the only ones allowed.
_LIVE_ALPACA_HOSTS = ("api.alpaca.markets",)


class EnvSettings(BaseSettings):
    """Secrets + a few operational switches sourced from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    config_path: str = "configs/config.yaml"
    trading_mode: str | None = None

    # --- Alpaca ---
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_paper: bool = True
    alpaca_data_feed: str = "crypto"
    apca_api_base_url: str = ""

    # --- Ollama ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_api_key: str = ""
    ollama_model: str = ""

    # --- Persistence ---
    database_url: str = ""

    # --- Optional enrichment ---
    quiver_api_key: str | None = None

    # ----- helpers ---------------------------------------------------------
    def secret_values(self) -> list[str]:
        """Return non-empty secrets so logging can redact them."""
        candidates = [
            self.alpaca_api_key,
            self.alpaca_api_secret,
            self.ollama_api_key,
            self.quiver_api_key or "",
        ]
        return [value for value in candidates if value]

    def has_alpaca_credentials(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_api_secret)

    def is_live_alpaca_endpoint(self) -> bool:
        """True when a live (non-paper) Alpaca endpoint is configured."""
        base = (self.apca_api_base_url or "").strip().lower()
        if not base:
            return False
        # paper endpoint contains "paper-api"; anything with the live host and
        # without "paper" is treated as live.
        if "paper" in base:
            return False
        return any(host in base for host in _LIVE_ALPACA_HOSTS)

    def masked(self) -> dict[str, str]:
        """A sanitised view safe to expose via API/dashboard/logs."""
        return {
            "app_env": self.app_env,
            "trading_mode": self.trading_mode or "paper",
            "alpaca_api_key": _mask(self.alpaca_api_key),
            "alpaca_api_secret": _mask(self.alpaca_api_secret),
            "alpaca_paper": self.alpaca_paper,
            "alpaca_data_feed": self.alpaca_data_feed,
            "ollama_base_url": self.ollama_base_url,
            "ollama_api_key": _mask(self.ollama_api_key),
            "ollama_model": self.ollama_model,
            "database_url": _mask(self.database_url),
            "config_path": self.config_path,
        }


def _mask(value: str | None, keep: int = 4) -> str:
    """Mask a secret, revealing only a short suffix for debugging."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]
