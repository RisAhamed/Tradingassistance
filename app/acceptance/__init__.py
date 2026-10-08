"""Bounded live acceptance harness (Phase J).

Proves a real Alpaca PAPER order can be submitted, filled, read back,
flattened, and reconciled end-to-end. Repository configuration is NEVER
modified: execution opens in-process only, bounded to a single order, and
is closed before exit on every path.
"""
