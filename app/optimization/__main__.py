"""CLI: ``python -m app.optimization.tuner --synthetic 5000 --space breakout_baseline ...``.

Thin wrapper — the real entry point lives in :mod:`app.optimization.tuner`
so both ``python -m app.optimization`` and
``python -m app.optimization.tuner`` work identically.
"""
from __future__ import annotations

from app.optimization.tuner import main

if __name__ == "__main__":
    raise SystemExit(main())
