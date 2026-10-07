"""Canonical symbol normalization (Phase D.5.5-R2).

Alpaca may report a symbol as ``BTCUSD`` while the application and its
configuration use ``BTC/USD``. A single authoritative helper keeps every
layer (adapter, reconciliation, ledger, OMS, position manager, harness,
reporting) comparing symbols consistently. Unknown representations are
normalized to a best-effort canonical form rather than silently mapped to a
valid symbol.
"""
from __future__ import annotations

# Quote suffixes we know how to expand. Longest first so ``USDT`` is tried
# before ``USD``. Phase D.5.6: configurable via market_data.symbol.quote_suffixes.
_DEFAULT_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "BTC", "ETH")
_QUOTE_SUFFIXES: tuple[str, ...] = _DEFAULT_QUOTE_SUFFIXES


def init_quote_suffixes(suffixes) -> None:
    """Called at engine startup with the configured suffix list.
    Module stays importable with sane default (no config dependency)."""
    global _QUOTE_SUFFIXES
    _QUOTE_SUFFIXES = tuple(str(s).upper() for s in suffixes)


def canonical_symbol(raw: str) -> str:
    """Return the canonical (app-configured) form, e.g. ``BTCUSD`` -> ``BTC/USD``."""
    symbol = (raw or "").strip().upper()
    if not symbol:
        return ""
    if "/" in symbol:
        base, _, quote = symbol.partition("/")
        return f"{base.upper()}/{quote.upper()}"
    for quote in _QUOTE_SUFFIXES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[: -len(quote)]}/{quote}"
    return symbol


def symbols_equal(a: str, b: str) -> bool:
    return canonical_symbol(a) == canonical_symbol(b)


def to_broker_symbol(raw: str) -> str:
    """Adapter-facing symbol, e.g. ``BTC/USD`` -> ``BTCUSD`` (Alpaca positions)."""
    return canonical_symbol(raw).replace("/", "")


__all__ = ["canonical_symbol", "symbols_equal", "to_broker_symbol", "init_quote_suffixes"]
