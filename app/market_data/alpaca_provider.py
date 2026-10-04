"""Alpaca market-data provider (PHASE B) with reconnect/backoff.

The live websocket is wrapped so the rest of the app only ever sees normalized
:class:`Quote` / :class:`Trade` objects — no Alpaca type ever leaks past this
module. Everything is validated on the way in (symbol, timestamp, price, size,
bid/ask, crossed quotes); malformed messages are rejected and logged, never
repaired or fabricated.

Connection lifecycle emits explicit structured events so the terminal and the
dashboard can show exactly what the feed is doing:

    ALPACA_CONNECTING -> ALPACA_CONNECTED -> ALPACA_SUBSCRIPTION_STARTED
      on loss:  ALPACA_DISCONNECTED -> ALPACA_RECONNECTING
      on retry: ALPACA_RECONNECTED | ALPACA_RECONNECT_FAILED
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
from datetime import datetime

from app.config.models import ReconnectConfig
from app.domain.market import Quote, Trade
from app.events.types import EventType
from app.market_data.base import MarketDataProvider, ProviderHealth

logger = logging.getLogger("app.market_data.alpaca")


class AlpacaMarketDataProvider(MarketDataProvider):
    name = "alpaca"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        feed: str = "crypto",
        reconnect: ReconnectConfig | None = None,
        bus=None,
    ) -> None:
        super().__init__()
        if not api_key or not api_secret:
            raise ValueError("Alpaca market-data credentials are required")
        # Secrets are held privately and NEVER logged or placed in payloads.
        self._api_key = api_key
        self._api_secret = api_secret
        self.feed = feed
        self.reconnect = reconnect or ReconnectConfig()
        self._bus = bus
        self._symbols: list[str] = []
        self._stream = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._connected = False
        self._last_at: datetime | None = None
        self._rejected = 0

    # -- structured events --------------------------------------------------
    async def _emit(self, event_type: EventType, message: str, level: int = logging.INFO, **fields) -> None:
        """Log a structured event and (optionally) publish it on the event bus.

        ``fields`` never contains credentials — only symbol/count/reason.
        """
        logger.log(
            level,
            message,
            extra={"structured": {"event": event_type.value, "component": "market_data.alpaca", **fields}},
        )
        if self._bus is not None:
            await self._bus.emit(event_type, payload=fields)

    def _reject(self, kind: str, reason: str, **fields) -> None:
        self._rejected += 1
        logger.debug(
            f"rejected malformed {kind}",
            extra={
                "structured": {
                    "event": "INVALID_MARKET_DATA",
                    "component": "market_data.alpaca",
                    "kind": kind,
                    "reason": reason,
                    **fields,
                }
            },
        )

    # -- lifecycle ----------------------------------------------------------
    async def connect(self, symbols: list[str]) -> None:
        self._symbols = list(symbols)
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever())

    async def disconnect(self) -> None:
        self._stop.set()
        if self._stream is not None:
            try:
                await self._stream.stop_ws()
            except Exception:  # noqa: BLE001 - best-effort shutdown
                logger.warning("error while stopping alpaca stream", exc_info=True)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._connected = False

    def health(self) -> ProviderHealth:
        return ProviderHealth(connected=self._connected, detail=f"feed={self.feed}", last_message_at=self._last_at)

    @property
    def rejected_count(self) -> int:
        return self._rejected

# -- normalization (Phase B #4) ----------------------------------------
    async def _on_quote(self, data) -> None:
        try:
            symbol = str(data.symbol)
            if symbol not in self._symbols:
                return self._reject("quote", "unexpected_symbol", symbol=symbol)
            bid = float(data.bid_price)
            ask = float(data.ask_price)
            if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 or ask <= 0:
                return self._reject("quote", "non_positive_or_non_finite", symbol=symbol)
            if ask < bid:
                return self._reject("quote", "crossed_quote", symbol=symbol)
            quote = Quote(
                timestamp=_ts(data.timestamp),
                symbol=symbol,
                bid=bid,
                ask=ask,
                bid_size=float(getattr(data, "bid_size", 0.0) or 0.0),
                ask_size=float(getattr(data, "ask_size", 0.0) or 0.0),
            )
        except (TypeError, ValueError) as exc:
            return self._reject("quote", f"malformed:{exc}")
        self._last_at = quote.timestamp
        await self._dispatch(quote)

    async def _on_trade(self, data) -> None:
        try:
            symbol = str(data.symbol)
            if symbol not in self._symbols:
                return self._reject("trade", "unexpected_symbol", symbol=symbol)
            price = float(data.price)
            size = float(getattr(data, "size", 0.0) or 0.0)
            if not math.isfinite(price) or price <= 0:
                return self._reject("trade", "non_positive_or_non_finite", symbol=symbol)
            if not math.isfinite(size) or size < 0:
                return self._reject("trade", "invalid_size", symbol=symbol)
            trade = Trade(timestamp=_ts(data.timestamp), symbol=symbol, price=price, size=size)
        except (TypeError, ValueError) as exc:
            return self._reject("trade", f"malformed:{exc}")
        self._last_at = trade.timestamp
        await self._dispatch(trade)

    async def _run_stream(self) -> None:
        """Run the Alpaca stream inside our event loop.

        alpaca-py's ``DataStream.run()`` is a *blocking* helper that calls
        ``asyncio.run()`` internally, so invoking it from a running loop raises.
        When the SDK exposes an async ``run`` we await it; otherwise we hand it to
        a worker thread where it can own its event loop (its supported usage).
        """
        run = self._stream.run
        if inspect.iscoroutinefunction(run):
            await run()
        else:
            await asyncio.to_thread(run)

    # -- connection loop ----------------------------------------------------
    def _build_stream(self):
        if self.feed == "crypto":
            from alpaca.data.live import CryptoDataStream

            stream = CryptoDataStream(self._api_key, self._api_secret)
        else:
            from alpaca.data.live import StockDataStream

            stream = StockDataStream(self._api_key, self._api_secret, feed=self.feed)
        stream.subscribe_quotes(self._on_quote, *self._symbols)
        stream.subscribe_trades(self._on_trade, *self._symbols)
        return stream

    async def _run_forever(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._emit(
                    EventType.ALPACA_CONNECTING,
                    "alpaca market data connecting",
                    symbols=",".join(self._symbols),
                    feed=self.feed,
                    attempt=attempt,
                )
                self._stream = self._build_stream()
                await self._emit(
                    EventType.ALPACA_CONNECTED,
                    "alpaca connected",
                    feed=self.feed,
                    reconnecting=attempt > 0,
                )
                await self._emit(
                    EventType.ALPACA_SUBSCRIPTION_STARTED,
                    f"SUBSCRIBED {','.join(self._symbols)}",
                    symbols=",".join(self._symbols),
                )
                self._connected = True
                if attempt > 0:
                    await self._emit(EventType.ALPACA_RECONNECTED, "alpaca reconnected", attempt=attempt)
                started_at = _monotonic()
                await self._run_stream()
                stable = _monotonic() - started_at
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect path is explicit
                self._connected = False
                attempt += 1
                if attempt == 1:
                    await self._emit(
                        EventType.ALPACA_RECONNECTING, "alpaca reconnecting", attempt=attempt
                    )
                await self._emit(
                    EventType.ALPACA_DISCONNECTED,
                    "alpaca disconnected",
                    logging.ERROR,
                    attempt=attempt,
                    error=str(exc),
                )
                if not self.reconnect.enabled or attempt > self.reconnect.max_attempts:
                    await self._emit(
                        EventType.ALPACA_RECONNECT_FAILED,
                        "alpaca reconnect exhausted",
                        logging.CRITICAL,
                        attempts=attempt,
                        error=str(exc),
                    )
                    return
                delay = min(
                    self.reconnect.initial_delay_seconds * (2 ** (attempt - 1)),
                    self.reconnect.max_delay_seconds,
                )
                if attempt > 1:
                    await self._emit(
                        EventType.ALPACA_RECONNECTING,
                        "alpaca reconnecting",
                        attempt=attempt,
                        delay=delay,
                    )
                await asyncio.sleep(delay)
            else:
                # Stream returned cleanly (e.g. the server closed it) -> treat as a
                # loss, but only forgive the backoff if it had been healthy for a
                # while (otherwise a flapping stream would reset the counter and
                # reconnect forever).
                self._connected = False
                if self._stop.is_set():
                    return
                if stable >= _stable_seconds(self.reconnect):
                    attempt = 0
                attempt += 1
                await self._emit(
                    EventType.ALPACA_DISCONNECTED,
                    "alpaca stream ended",
                    logging.WARNING,
                    attempt=attempt,
                )
                if not self.reconnect.enabled or attempt > self.reconnect.max_attempts:
                    await self._emit(
                        EventType.ALPACA_RECONNECT_FAILED,
                        "alpaca reconnect exhausted",
                        logging.CRITICAL,
                        attempts=attempt,
                    )
                    return
                await asyncio.sleep(self.reconnect.initial_delay_seconds)


def _monotonic() -> float:
    return asyncio.get_event_loop().time()


def _stable_seconds(reconnect: ReconnectConfig) -> float:
    """How long a stream must survive before the backoff counter is forgiven."""
    return max(5.0, reconnect.initial_delay_seconds * 5)


def _ts(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"unsupported timestamp: {value!r}")