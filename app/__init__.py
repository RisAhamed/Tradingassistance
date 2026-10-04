"""Trading Agent — a systematic, configuration-driven PAPER-TRADING system.

This package intentionally implements PAPER TRADING ONLY. There is no live
trading switch, and the engine refuses to start against live endpoints.
"""
from __future__ import annotations

__all__ = ["__version__", "TRADING_DISCLAIMER"]

__version__ = "0.1.0"

TRADING_DISCLAIMER = "PAPER_TRADING_ONLY"
