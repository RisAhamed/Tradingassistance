"""Phase A #3: market-data safety.

Invariant: missing / invalid / out-of-order / duplicate market data can never
create a new entry, and the engine never invents a replacement price.
"""
from datetime import timedelta

from app.config.loader import get_env, load_config
from app.domain.market import Quote, Trade
from app.runtime import build_runtime
from tests.support import NOW, SYMBOL


def _engine():
    env = get_env()
    config = load_config(env=env)
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    return build_runtime(config, env).engine


def _spy_on_trades(engine) -> list:
    seen: list = []
    original = engine.aggregator.add_trade

    def spy(trade):
        seen.append(trade)
        return original(trade)

    engine.aggregator.add_trade = spy  # type: ignore[method-assign]
    return seen


async def test_unexpected_symbol_is_rejected():
    engine = _engine()
    await engine.on_market_update(Quote(timestamp=NOW, symbol="ETH/USD", bid=99.0, ask=101.0))
    assert engine.store.snapshot("ETH/USD") is None
    assert engine.store.snapshot(SYMBOL) is None


async def test_invalid_crossed_quote_is_rejected():
    engine = _engine()
    # ask < bid is invalid; the domain permits construction, the engine must not.
    await engine.on_market_update(Quote(timestamp=NOW, symbol=SYMBOL, bid=101.0, ask=99.0))
    assert engine.store.snapshot(SYMBOL) is None


async def test_duplicate_trade_is_dropped():
    engine = _engine()
    trades = _spy_on_trades(engine)
    trade = Trade(timestamp=NOW, symbol=SYMBOL, price=100.0, size=1.0)
    await engine.on_market_update(trade)
    await engine.on_market_update(trade)  # exact replay
    assert len(trades) == 1


async def test_out_of_order_trade_is_dropped():
    engine = _engine()
    trades = _spy_on_trades(engine)
    await engine.on_market_update(Trade(timestamp=NOW, symbol=SYMBOL, price=100.0, size=1.0))
    await engine.on_market_update(
        Trade(timestamp=NOW - timedelta(seconds=30), symbol=SYMBOL, price=90.0, size=1.0)
    )
    assert len(trades) == 1


async def test_quote_and_trade_same_timestamp_are_both_accepted():
    """A paired quote+trade share one market moment; both must be processed."""
    engine = _engine()
    trades = _spy_on_trades(engine)
    await engine.on_market_update(Quote(timestamp=NOW, symbol=SYMBOL, bid=99.0, ask=101.0))
    await engine.on_market_update(Trade(timestamp=NOW, symbol=SYMBOL, price=100.0, size=1.0))
    assert engine.store.snapshot(SYMBOL) is not None
    assert len(trades) == 1


async def test_advancing_ticks_are_accepted():
    engine = _engine()
    trades = _spy_on_trades(engine)
    for i in range(3):
        await engine.on_market_update(
            Trade(timestamp=NOW + timedelta(seconds=i), symbol=SYMBOL, price=100.0 + i, size=1.0)
        )
    assert len(trades) == 3
