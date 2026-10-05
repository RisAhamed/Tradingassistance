"""Structured logging with secret redaction.

The console shows a readable internal flow; the file log keeps a
JSON-compatible structured record of every important event. All output is
scrubbed of configured secrets before it is emitted.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

_PAPER_BANNER = "PAPER_TRADING_ONLY"

# LogRecord attributes that must not be treated as structured extras.
_RESERVED_ATTRS = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "structured"}


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def _structured(record: logging.LogRecord) -> dict[str, Any]:
    payload = getattr(record, "structured", None)
    if isinstance(payload, dict):
        return dict(payload)
    extras: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key not in _RESERVED_ATTRS and not key.startswith("_"):
            extras[key] = value
    return extras


class _ScrubbingFormatter(logging.Formatter):
    """Base formatter that redacts configured secret values from output."""

    secrets: list[str] = []

    @classmethod
    def set_secrets(cls, secrets: list[str]) -> None:
        cls.secrets = [s for s in secrets if s]

    def scrub(self, text: str) -> str:
        for secret in self.secrets:
            if secret and secret in text:
                text = text.replace(secret, mask(secret))
        return text


class JsonFormatter(_ScrubbingFormatter):
    """Emit one JSON object per record (file log)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_structured(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return self.scrub(json.dumps(payload, default=str))

class ConsoleFormatter(_ScrubbingFormatter):
    """Human-readable console output: ``[time] LEVEL component event k=v``."""

    _SCALAR = (str, int, float, bool)

    def format(self, record: logging.LogRecord) -> str:
        structured = _structured(record)
        component = structured.pop("component", record.name)
        event = structured.pop("event", record.getMessage())
        stamp = self.formatTime(record, "%H:%M:%S")
        line = f"[{stamp}] {record.levelname:<8} {component:<14} {event}"
        trailing = " ".join(
            f"{key}={value}"
            for key, value in structured.items()
            if isinstance(value, self._SCALAR)
        )
        if trailing:
            line = f"{line}  {trailing}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return self.scrub(line)


_CONFIGURED = False


def configure_logging(
    config: Any,
    env: Any,
    project_root: Path | None = None,
) -> None:
    """Configure root logging from the ``logging`` config section.

    Idempotent: repeated calls are ignored so importing the app multiple times
    (e.g. under uvicorn reload) does not duplicate handlers.
    """
    global _CONFIGURED
    logging_config = config.logging
    secrets = env.secret_values()
    JsonFormatter.set_secrets(secrets)
    ConsoleFormatter.set_secrets(secrets)

    log_path = Path(logging_config.file.path)
    if project_root and not log_path.is_absolute():
        log_path = project_root / log_path
    print(f"LOG_DIRECTORY = {log_path.parent.resolve()}")

    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(logging_config.level).upper(), logging.INFO))
    root.handlers.clear()

    if logging_config.console.enabled:
        console = logging.StreamHandler(stream=sys.stdout)
        if logging_config.console.format == "structured":
            console.setFormatter(ConsoleFormatter())
        else:
            console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(console)

    if logging_config.file.enabled:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if logging_config.rotation.enabled:
            file_handler: logging.Handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=logging_config.rotation.maximum_size_mb * 1024 * 1024,
                backupCount=logging_config.rotation.backup_count,
                encoding="utf-8",
            )
        else:
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    _CONFIGURED = True


def reset_logging_state() -> None:
    """Testing helper: allow ``configure_logging`` to run again."""
    global _CONFIGURED
    _CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    component: str,
    **fields: Any,
) -> None:
    """Log a named structured event with optional contextual fields."""
    structured = {"event": event, "component": component}
    structured.update(fields)
    logger.log(level, event, extra={"structured": structured})

