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
) -> dict[str, Any]:
    """Assemble the proposal dict. ``run_id`` covers only deterministic inputs."""
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
