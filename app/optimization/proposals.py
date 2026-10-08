"""Proposal schema + writer (Phase H).

One JSON per tuner run, written ONLY under the configured output directory
(``logs/proposals/``). Never writes into ``configs/``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_KEYS = (
    "run_id", "generated_at", "git_commit", "data_hash", "reference_start",
    "space_file", "window_config", "scoring_config", "baseline", "candidates",
    "recommended", "reproducibility",
)

ORIGINS = ("tuner", "ai")


def build_proposal(
    *,
    data_hash: str,
    reference_start: str | None,
    space_file: str,
    space_content: str,
    window_config: dict[str, Any],
    scoring_config: dict[str, Any],
    baseline: dict[str, Any],
    candidates: list[dict[str, Any]],
    recommended: dict[str, Any],
    git_commit: str,
    config_hash: str,
    command: str,
    origin: str = "tuner",
    author: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the proposal dict. ``run_id`` covers only deterministic inputs."""
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}, got '{origin}'")
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "data_hash": data_hash,
                "reference_start": reference_start,
                "space_file": space_file,
                "space_content": space_content,
                "window_config": window_config,
                "scoring_config": scoring_config,
                "baseline": baseline,
                "candidates": candidates,
                "recommended": recommended,
                "config_hash": config_hash,
                "git_commit": git_commit,
                "command": command,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "run_id": fingerprint,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "origin": origin,
        "author": author,
        "git_commit": git_commit,
        "data_hash": data_hash,
        "reference_start": reference_start,
        "space_file": space_file,
        "window_config": window_config,
        "scoring_config": scoring_config,
        "baseline": baseline,
        "candidates": candidates,
        "recommended": recommended,
        "reproducibility": {
            "config_hash": config_hash,
            "git_commit": git_commit,
            "command": command,
        },
    }


def validate_proposal(proposal: dict[str, Any]) -> list[str]:
    """Return a list of missing/invalid keys (empty when valid)."""
    problems: list[str] = []
    for key in REQUIRED_KEYS:
        if key not in proposal:
            problems.append(f"missing key: {key}")
    return problems


def validate_override_params(config: Any, params: dict[str, Any]) -> Any:
    """Validate dotted override params against the AppConfig schema in memory.

    Unknown dotted paths raise ValueError; out-of-range values fail full
    model revalidation. Returns the validated override mapping. Never writes.
    """
    from app.config.models import AppConfig

    if not isinstance(params, dict) or not params:
        raise ValueError("params must be a non-empty mapping of dotted.path: value")
    probe = config.model_copy(deep=True)
    for dotted, value in params.items():
        node: Any = probe
        parts = str(dotted).split(".")
        for part in parts[:-1]:
            if not hasattr(node, part):
                raise ValueError(f"unknown parameter '{dotted}'")
            node = getattr(node, part)
        if not hasattr(node, parts[-1]):
            raise ValueError(f"unknown parameter '{dotted}'")
        setattr(node, parts[-1], value)
    AppConfig.model_validate(probe.model_dump(mode="json"))  # range/type check
    return dict(params)


def build_ai_proposal(*, summary: str, params: dict[str, Any], rationale: str,
                      model: str) -> dict[str, Any]:
    """Assemble an AI-authored pending proposal (origin='ai')."""
    if not summary or not rationale:
        raise ValueError("summary and rationale are both required")
    stamp = datetime.now(timezone.utc).isoformat()
    proposal_id = hashlib.sha256(
        f"ai|{model}|{stamp}|{json.dumps(params, sort_keys=True, default=str)}".encode()
    ).hexdigest()[:16]
    return {
        "proposal_id": proposal_id,
        "origin": "ai",
        "author": {"model": model, "created_at": stamp},
        "summary": summary,
        "params": dict(params),
        "rationale": rationale,
        "created_at": stamp,
    }


def write_pending_proposal(proposal: dict[str, Any], pending_dir: str | Path) -> tuple[str, Path]:
    """Write an AI proposal into pending_dir. Returns (proposal_id, path)."""
    directory = Path(pending_dir)
    directory.mkdir(parents=True, exist_ok=True)
    proposal_id = str(proposal.get("proposal_id") or proposal.get("run_id"))
    path = directory / f"{proposal_id}.json"
    path.write_text(json.dumps(proposal, indent=2, sort_keys=True, default=str) + "\n",
                     encoding="utf-8")
    return proposal_id, path


def write_proposal(proposal: dict[str, Any], output_dir: str | Path) -> Path:
    """Persist one proposal JSON under output_dir. Returns the path."""
    problems = validate_proposal(proposal)
    if problems:
        raise ValueError(f"invalid proposal: {'; '.join(problems)}")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    path = directory / f"{stamp}.json"
    path.write_text(json.dumps(proposal, indent=2, sort_keys=True, default=str) + "\n",
                     encoding="utf-8")
    return path
