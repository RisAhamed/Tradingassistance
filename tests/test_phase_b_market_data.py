"""PHASE B #2-#12: Alpaca market data, normalization, candles, features, reconnect.

These tests never touch the network: the Alpaca adapter is driven with fake
wire-format objects so normalization and reconnect behaviour are deterministic.
"""
import asyncio
import logging
from types import SimpleNamespace

from app.config.models import FeaturesConfig, ReconnectConfig
from app.core.clock import floor_to_timeframe
from app.events.bus import EventBus
from app.events.types import EventType
from app.features.engine import FeatureEngine
from app.market_data.aggregator import CandleAggregator
from app.market_data.alpaca_provider import AlpacaMarketDataProvider
from app.domain.market import Trade
from tests.support import NOW, SYMBOL


def _provider(bus=None, reconnect: ReconnectConfig | None = None) -> AlpacaMarketDataProvider:
    provider = AlpacaMarketDataProvider(
        "KEY-not-a-real-secret",
        "SECRET-not-a-real-secret",
        reconnect=reconnect or ReconnectConfig(),
        bus=bus,
    )
    provider._symbols = [SYMBOL]
    return provider


def _quote(**overrides):
    data = dict(
        symbol=SYMBOL,
        bid_price=100.0,
        ask_price=100.5,
        bid_size=1.0,
        ask_size=1.0,
        timestamp=NOW,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _trade(**overrides):
    data = dict(symbol=SYMBOL, price=100.25, size=0.5, timestamp=NOW)
    data.update(overrides)
    return SimpleNamespace(**data)


def _capture(provider) -> list:
    seen: list = []
    provider.set_handler(lambda update: seen.append(update))
    return seen


# --- #2 valid Alpaca normalization ------------------------------------------
async def test_valid_alpaca_quote_and_trade_are_normalized():
    provider = _provider()
    seen = _capture(provider)
    await provider._on_quote(_quote())
    await provider._on_trade(_trade())
    assert len(seen) == 2
    quote, trade = seen
    assert quote.bid == 100.0 and quote.ask == 100.5
    assert trade.price == 100.25 and trade.size == 0.5
    assert quote.symbol == SYMBOL and trade.symbol == SYMBOL


async def test_alpaca_timestamp_string_is_parsed():
    provider = _provider()
    seen = _capture(provider)
    await provider._on_trade(_trade(timestamp="2026-01-01T12:00:00Z"))
    assert seen[0].timestamp.tzinfo is not None


# --- #3 malformed market data is rejected -----------------------------------
async def test_malformed_alpaca_messages_are_rejected():
    cases = [
        _quote(bid_price=0.0),                 # non-positive
        _quote(ask_price=-1.0),                # negative
        _quote(bid_price=101.0, ask_price=99.0),  # crossed
        _quote(bid_price=float("nan")),        # non-finite
        _quote(symbol="ETH/USD"),              # wrong symbol
        _trade(price=0.0),                     # non-positive price
        _trade(price=float("inf")),            # non-finite price
        _trade(size=-1.0),                     # invalid size
        _trade(symbol="ETH/USD"),              # wrong symbol
        _trade(timestamp=None),                # malformed timestamp
        _quote(bid_price=None),                # missing field
    ]
    for message in cases:
        provider = _provider()
        seen = _capture(provider)
        await provider._on_quote(message) if hasattr(message, "bid_price") else await provider._on_trade(message)
        assert seen == [], f"should have been rejected: {message}"
        assert provider.rejected_count == 1


async def test_malformed_data_never_fabricates_a_price():
    provider = _provider()
    seen = _capture(provider)
    await provider._on_trade(_trade(price=0.0))
    assert seen == []
    assert provider.health().last_message_at is None


# --- #12 no secret leakage --------------------------------------------------
async def test_alpaca_secrets_never_appear_in_logs(caplog):
    secret_key = "AKIA-SUPER-SECRET-KEY"
    secret_value = "SUPER-SECRET-VALUE-123"
    provider = AlpacaMarketDataProvider(
        secret_key, secret_value, bus=EventBus(), reconnect=ReconnectConfig()
    )
    provider._symbols = [SYMBOL]
    with caplog.at_level(logging.DEBUG, logger="app.market_data.alpaca"):
        await provider._on_quote(_quote(symbol="ETH/USD"))   # rejected -> logged
        await provider._on_trade(_trade(price=0.0))          # rejected -> logged
        await provider._emit(EventType.ALPACA_CONNECTED, "connected", symbol=SYMBOL)
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_key not in text
    assert secret_value not in text


# --- #10 connection lifecycle / reconnect -----------------------------------
class _FakeStream:
    """Stands in for an Alpaca websocket stream (no network)."""

    def __init__(self, behaviours):
        self._behaviours = list(behaviours)

    def subscribe_quotes(self, *args, **kwargs):
        return None

    def subscribe_trades(self, *args, **kwargs):
        return None

    async def run(self):
        behaviour = self._behaviours.pop(0) if self._behaviours else "hang"
        if isinstance(behaviour, Exception):
            raise behaviour
        if behaviour == "end":
            return
        while True:
            await asyncio.sleep(0.01)

    async def stop_ws(self):
        return None


async def _wait_for(bus: EventBus, event_type: EventType, timeout: float = 3.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if any(event.type is event_type for event in bus.recent(2000)):
            return True
        await asyncio.sleep(0.01)
    return False


def _fast_reconnect(max_attempts: int = 3) -> ReconnectConfig:
    return ReconnectConfig(
        enabled=True, max_attempts=max_attempts, initial_delay_seconds=0.01, max_delay_seconds=0.02
    )


async def test_initial_connection_failure_reports_reconnect_failure():
    bus = EventBus()
    provider = _provider(bus=bus, reconnect=_fast_reconnect(max_attempts=2))
    provider._build_stream = lambda: _FakeStream([RuntimeError("boom")])
    await provider.connect([SYMBOL])
    try:
        assert await _wait_for(bus, EventType.ALPACA_RECONNECT_FAILED)
        types = {event.type for event in bus.recent(2000)}
        assert EventType.ALPACA_CONNECTING in types
        assert EventType.ALPACA_DISCONNECTED in types
        assert EventType.ALPACA_RECONNECTING in types
    finally:
        await provider.disconnect()


async def test_reconnect_recovers_after_transient_failure():
    bus = EventBus()
    provider = _provider(bus=bus, reconnect=_fast_reconnect(max_attempts=5))
    calls = {"n": 0}

    def _build():
        calls["n"] += 1
        return _FakeStream([RuntimeError("boom")]) if calls["n"] == 1 else _FakeStream(["hang"])

    provider._build_stream = _build
    await provider.connect([SYMBOL])
    try:
        assert await _wait_for(bus, EventType.ALPACA_RECONNECTED)
        assert provider.health().connected is True
    finally:
        await provider.disconnect()


async def test_connection_loss_is_reported_and_never_leaves_fake_data():
    bus = EventBus()
    provider = _provider(bus=bus, reconnect=_fast_reconnect(max_attempts=1))
    provider._build_stream = lambda: _FakeStream(["end"])
    seen = _capture(provider)
    await provider.connect([SYMBOL])
    try:
        assert await _wait_for(bus, EventType.ALPACA_DISCONNECTED)
        assert seen == []  # a lost stream invents nothing
    finally:
        await provider.disconnect()


# --- #7 candle aggregation ---------------------------------------------------
def _trade_at(ts, price: float, size: float = 1.0) -> Trade:
    return Trade(timestamp=ts, symbol=SYMBOL, price=price, size=size)


def test_candle_first_bucket_is_partial_and_not_yet_closed():
    agg = CandleAggregator(SYMBOL, ["5m"])
    t0 = floor_to_timeframe(NOW, "5m")
    assert agg.add_trade(_trade_at(t0, 100.0)) == []  # only *completed* candles close
    current = agg.current("5m")
    assert current.open == 100.0 and current.close == 100.0


def test_candle_updates_within_bucket_and_closes_on_boundary():
    agg = CandleAggregator(SYMBOL, ["5m"])
    t0 = floor_to_timeframe(NOW, "5m")
    agg.add_trade(_trade_at(t0, 100.0))
    agg.add_trade(_trade_at(t0.replace(second=30), 105.0))
    current = agg.current("5m")
    assert (current.high, current.low, current.close) == (105.0, 100.0, 105.0)
    closed = agg.add_trade(_trade_at(t0 + _minutes(5), 110.0))
    assert len(closed) == 1 and closed[0].close == 105.0


def test_out_of_order_trade_never_rewrites_a_closed_candle():
    agg = CandleAggregator(SYMBOL, ["5m"])
    t0 = floor_to_timeframe(NOW, "5m")
    agg.add_trade(_trade_at(t0, 100.0))
    agg.add_trade(_trade_at(t0 + _minutes(10), 120.0))
    current = agg.current("5m")
    closed = agg.add_trade(_trade_at(t0 + _minutes(1), 1.0, size=9.0))
    assert closed == []
    assert agg.current("5m") is current
    assert current.close == 120.0


def test_reconnect_gap_does_not_invent_candles():
    agg = CandleAggregator(SYMBOL, ["5m"])
    t0 = floor_to_timeframe(NOW, "5m")
    agg.add_trade(_trade_at(t0, 100.0))
    closed = agg.add_trade(_trade_at(t0 + _minutes(180), 120.0))
    assert len(closed) == 1
    assert closed[0].timestamp == t0


def test_candle_started_callback_fires_per_bucket():
    started: list = []
    agg = CandleAggregator(SYMBOL, ["5m"], on_candle_started=started.append)
    t0 = floor_to_timeframe(NOW, "5m")
    agg.add_trade(_trade_at(t0, 100.0))
    agg.add_trade(_trade_at(t0 + _minutes(5), 101.0))
    assert len(started) == 2
    assert {c.timeframe for c in started} == {"5m"}


def _minutes(n: int):
    from datetime import timedelta

    return timedelta(minutes=n)


# --- #8 feature warm-up ------------------------------------------------------
def _candles(count: int):
    from datetime import timedelta

    from app.domain.market import Candle

    out = []
    for i in range(count):
        base = 100.0 + i
        out.append(
            Candle(
                timestamp=NOW + timedelta(minutes=5 * i),
                symbol=SYMBOL,
                open=base,
                high=base + 1.5,
                low=base - 1.5,
                close=base + 0.5,
                volume=1.0,
                timeframe="5m",
            )
        )
    return out


def test_features_are_not_ready_until_the_series_is_long_enough():
    engine = FeatureEngine(FeaturesConfig())
    assert not engine.compute(_candles(10), timeframe="5m").ready
    assert not engine.compute(_candles(20), timeframe="5m").ready
    assert engine.compute(_candles(60), timeframe="5m").ready


def test_features_never_invent_missing_values():
    engine = FeatureEngine(FeaturesConfig())
    snapshot = engine.compute(_candles(10), timeframe="5m")
    numeric = snapshot.numeric_values()
    # ema_50 cannot exist with 10 candles and must be absent, not fabricated.
    assert "ema_50" not in numeric or snapshot.candle_count >= 50
    assert "ema_50" in snapshot.missing or snapshot.candle_count >= 50


# --- #9 UNKNOWN regime during warm-up (engine level) -------------------------
async def test_unknown_regime_warmup_produces_no_signals_and_no_orders():
    from app.config.loader import get_env, load_config
    from app.runtime import build_runtime

    config = load_config(env=get_env())
    config.market_data.provider = "mock"
    config.trading.broker = "mock"
    config.ai.enabled = False
    config.storage.enabled = False
    config.logging.console.enabled = False
    config.logging.file.enabled = False
    config.execution.enabled = False
    config.market_data.max_future_skew_seconds = 86400 * 30
    engine = build_runtime(config, get_env()).engine

    await engine.start()
    try:
        provider = engine.provider
        ticks = 0
        # Deliberately short: not enough context candles to classify a regime.
        while not provider.exhausted and ticks < 100:
            await provider.pump_one()
            ticks += 1
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        assert engine.state.regime is not None
        assert engine.state.regime.is_unknown
        assert not engine.state.recent_signals
        assert engine.oms.all_orders() == []
    finally:
        await engine.stop()