"""CLI: ``python -m app.acceptance --mode direct|pipeline`` (Phase J).

Bounded live acceptance. Repository yaml is never modified; execution opens
in-process only and is restored on every exit path.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.acceptance import direct as direct_mod
from app.acceptance import pipeline as pipeline_mod
from app.acceptance.reporting import (
    banner_bottom,
    banner_top,
    print_summary,
    write_report,
)
from app.config.loader import PROJECT_ROOT, get_env, load_config


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Bounded live acceptance harness")
    parser.add_argument("--mode", choices=["direct", "pipeline"], default=None)
    parser.add_argument("--quantity", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=str, default=None)
    parser.add_argument("--timeout", type=float, default=None)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    env = get_env()
    config = load_config(env=env)
    from app.core.logging import configure_logging

    configure_logging(config, env, project_root=PROJECT_ROOT)
    mode = args.mode or config.live_acceptance.default_mode
    quantity = args.quantity if args.quantity is not None else config.live_acceptance.max_order_quantity
    timeout = args.timeout if args.timeout is not None else (
        config.live_acceptance.pipeline_timeout_seconds if mode == "pipeline"
        else config.live_acceptance.max_runtime_seconds)

    print(banner_top(config))
    print(f"mode={mode} quantity(requested)={quantity} timeout={timeout}s")
    if args.dry_run:
        from app.acceptance.preflight import run_preflight
        from app.runtime import build_runtime

        async def _dry() -> int:
            probe = build_runtime(config, env).engine
            checks = await run_preflight(config, env, probe)
            for check in checks:
                print(f"preflight {check.name}: {'PASS' if check.ok else 'FAIL'} — {check.detail}")
            print("dry-run plan: preflight only, no order submitted")
            print(f"repository execution.enabled={config.execution.enabled} (unchanged)")
            return 0 if all(c.ok for c in checks) else 2

        return asyncio.run(_dry())

    if mode == "direct":
        code, report = asyncio.run(direct_mod.run_direct(
            config, env, quantity=quantity, timeout=timeout))
    else:
        code, report = asyncio.run(pipeline_mod.run_pipeline(
            config, env, quantity=quantity, timeout=timeout))
    path = write_report(report, args.report)
    print(banner_bottom(report))
    print_summary(report, path)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
