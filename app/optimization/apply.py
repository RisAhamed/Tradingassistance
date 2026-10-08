"""Shared proposal apply/reject/list routines (Phase I).

Single code path used by BOTH the human API endpoints and the AI
``apply_proposal`` tool — no duplicate logic. The base
``configs/config.yaml`` is never written; applies land in the code-owned
``configs/applied_overrides.yaml`` overlay (atomic write), which
``load_config()`` merges at next startup (restart required).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.config.loader import APPLIED_OVERRIDES_FILENAME, PROJECT_ROOT
from app.events.types import EventType
from app.optimization.proposals import validate_proposal

logger = logging.getLogger("app.optimization.apply")


def _dirs(engine) -> dict[str, Path]:
    cfg = engine.config.optimization

    def _resolve(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else PROJECT_ROOT / path

    return {
        "output": _resolve(cfg.output_dir),
        "pending": _resolve(cfg.pending_dir),
        "applied": _resolve(cfg.applied_dir),
        "rejected": _resolve(cfg.rejected_dir),
    }


def _overlay_path() -> Path:
    return PROJECT_ROOT / "configs" / APPLIED_OVERRIDES_FILENAME


def find_proposal(engine, proposal_id: str) -> tuple[dict[str, Any], Path] | None:
    """Locate a proposal JSON by id across pending/applied/rejected/output."""
    for directory in _dirs(engine).values():
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.stem == proposal_id and not path.stem.endswith(".applied"):
                try:
                    return json.loads(path.read_text(encoding="utf-8")), path
                except (json.JSONDecodeError, OSError):
                    continue
    return None


def list_proposals(engine) -> list[dict[str, Any]]:
    """Small summaries across pending + applied + rejected."""
    out: list[dict[str, Any]] = []
    for state, directory in (("pending", _dirs(engine)["pending"]),
                             ("applied", _dirs(engine)["applied"]),
                             ("rejected", _dirs(engine)["rejected"])):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.stem.endswith(".applied"):
                continue
            try:
                proposal = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            out.append({
                "proposal_id": path.stem,
                "state": state,
                "origin": proposal.get("origin"),
                "summary": proposal.get("summary") or proposal.get("recommended", {}).get("reason"),
                "score": (proposal.get("recommended") or {}).get("score"),
            })
    return out


def overlay_params(proposal: dict[str, Any]) -> dict[str, Any]:
    """Params the overlay carries: recommended.params, else the AI proposal's own params.

    Tuner proposals rank candidates, so the overlay takes the winner
    (``recommended.params``). AI-authored proposals carry their params
    directly (``params``).
    """
    recommended = proposal.get("recommended") or {}
    if isinstance(recommended, dict) and recommended.get("params"):
        return dict(recommended["params"])
    if isinstance(proposal.get("params"), dict) and proposal["params"]:
        return dict(proposal["params"])
    raise ValueError("proposal carries no appliable params (need recommended.params or params)")


def _nest(dotted_params: dict[str, Any]) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for dotted, value in dotted_params.items():
        node = nested
        parts = str(dotted).split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return nested


async def _record_ai_action(engine, *, action: str, tool: str, payload: dict) -> None:
    if engine.repository is None:
        return
    try:
        await engine.repository.save_ai_action(
            session_id=getattr(engine.session, "session_id", None),
            action=action, tool=tool, permitted=True, payload=payload)
    except Exception:  # noqa: BLE001 - observability must not break applies
        logger.warning("ai_actions persistence failed",
                       extra={"structured": {"event": "AI_UNAVAILABLE", "component": "optimization.apply"}})


async def apply_proposal(engine, proposal_id: str, *, actor: str) -> dict[str, Any]:
    """Apply a pending proposal through the single shared routine."""
    dirs = _dirs(engine)
    if (dirs["applied"] / f"{proposal_id}.json").exists():
        return {"applied": False, "reason": "already_applied", "proposal_id": proposal_id}
    found = find_proposal(engine, proposal_id)
    if found is None:
        return {"applied": False, "reason": "proposal_not_found", "proposal_id": proposal_id}
    proposal, path = found
    if path.parent != dirs["pending"]:
        return {"applied": False, "reason": f"proposal is not pending (in {path.parent.name})",
                "proposal_id": proposal_id}
    try:
        params = overlay_params(proposal)
    except ValueError as exc:
        return {"applied": False, "reason": str(exc), "proposal_id": proposal_id}

    # Safety: halt first while execution is still armed, before any write.
    halted = False
    if bool(getattr(engine.config.execution, "enabled", False)):
        await engine.halt_experiment("proposal_apply", operator=actor)
        halted = True

    overlay_file = _overlay_path()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    if overlay_file.exists():
        backup = dirs["applied"] / f"{stamp}_{proposal_id}_backup.yaml"
        backup.write_text(overlay_file.read_text(encoding="utf-8"), encoding="utf-8")

    nested = _nest(params)
    tmp = overlay_file.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        yaml.safe_dump(nested, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, overlay_file)

    moved = dirs["applied"] / f"{proposal_id}.json"
    dirs["applied"].mkdir(parents=True, exist_ok=True)
    path.rename(moved)
    sidecar = dirs["applied"] / f"{proposal_id}.applied.json"
    sidecar.write_text(json.dumps({
        "proposal_id": proposal_id,
        "applied_by": actor,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "diff": params,
        "halted_first": halted,
        "restart_required": True,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    logger.warning(
        "PROPOSAL APPLIED",
        extra={"structured": {"event": "PROPOSAL_APPLIED", "component": "optimization.apply",
                              "proposal_id": proposal_id, "actor": actor,
                              "halted_first": halted, "restart_required": True}},
    )
    await engine.bus.emit(EventType.PROPOSAL_APPLIED,
                          payload={"proposal_id": proposal_id, "actor": actor,
                                   "halted_first": halted, "restart_required": True})
    await _record_ai_action(engine, action="proposal_applied", tool="apply_proposal",
                            payload={"proposal_id": proposal_id, "actor": actor})
    engine._last_applied_proposal = {"proposal_id": proposal_id,
                                     "applied_at": datetime.now(timezone.utc).isoformat()}
    return {"applied": True, "restart_required": True, "proposal_id": proposal_id,
            "halted_first": halted, "overlay": str(overlay_file)}


async def reject_proposal(engine, proposal_id: str, *, actor: str) -> dict[str, Any]:
    """Move a pending proposal to rejected_dir. No overlay change, ever."""
    dirs = _dirs(engine)
    found = find_proposal(engine, proposal_id)
    if found is None:
        return {"rejected": False, "reason": "proposal_not_found", "proposal_id": proposal_id}
    _, path = found
    if path.parent != dirs["pending"]:
        return {"rejected": False, "reason": f"proposal is not pending (in {path.parent.name})",
                "proposal_id": proposal_id}
    dirs["rejected"].mkdir(parents=True, exist_ok=True)
    moved = dirs["rejected"] / f"{proposal_id}.json"
    path.rename(moved)
    logger.warning(
        "PROPOSAL REJECTED",
        extra={"structured": {"event": "PROPOSAL_REJECTED", "component": "optimization.apply",
                              "proposal_id": proposal_id, "actor": actor}},
    )
    await engine.bus.emit(EventType.PROPOSAL_REJECTED,
                          payload={"proposal_id": proposal_id, "actor": actor})
    await _record_ai_action(engine, action="proposal_rejected", tool="reject_proposal",
                            payload={"proposal_id": proposal_id, "actor": actor})
    return {"rejected": True, "proposal_id": proposal_id}
