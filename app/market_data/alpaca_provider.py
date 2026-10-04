"""Alpaca market-data provider (crypto by default) with reconnect/backoff.

The live websocket is wrapped so the rest of the app only ever sees normalized
:class:`Quote` / :class:`Trade` objects. If the stream drops, the provider marks
itself unhealthy, reconnects with exponential backoff, and restores
subscriptions before data flows again.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.config.models import ReconnectConfig
from app.domain.market import Quote, Trade
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
    ) -> None:
        super().__init__()
        if not api_key or not api_secret:
            raise ValueError("Alpaca market-data credentials are required")
        self._api_key = api_key
        self._api_secret = api_secret
        self.feed = feed
        self.reconnect = reconnect or ReconnectConfig()
        self._symbols: list[str] = []
        self._stream = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._connected = False
        self._last_at: datetime | None = None

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

    # -- data callbacks -----------------------------------------------------
    async def _on_quote(self, data) -> None:
        try:
            quote = Quote(
                timestamp=_ts(data.timestamp),
                symbol=str(data.symbol),
                bid=float(data.bid_price),
                ask=float(data.ask_price),
                bid_size=float(getattr(data, "bid_size", 0.0) or 0.0),
                ask_size=float(getattr(data, "ask_size", 0.0) or 0.0),
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "rejected malformed quote",
                extra={"structured": {"event": "INVALID_MARKET_DATA", "component": "market_data.alpaca", "error": str(exc)}},
            )
            return
        self._last_at = quote.timestamp
        await self._dispatch(quote)

    async def _on_trade(self, data) -> None:
        try:
            trade = Trade(
                timestamp=_ts(data.timestamp),
                symbol=str(data.symbol),
                price=float(data.price),
                size=float(getattr(data, "size", 0.0) or 0.0),
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "rejected malformed trade",
                extra={"structured": {"event": "INVALID_MARKET_DATA", "component": "market_data.alpaca", "error": str(exc)}},
            )
            return
        self._last_at = trade.timestamp
        await self._dispatch(trade)

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
                self._stream = self._build_stream()
                self._connected = True
                logger.info(
                    "market data stream connecting",
                    extra={"structured": {"event": "MARKET_DATA_STREAM_CONNECTING", "component": "market_data.alpaca", "symbols": ",".join(self._symbols)}},
                )
                await self._stream.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect path is explicit
                self._connected = False
                attempt += 1
                logger.error(
                    "market data stream error",
                    extra={"structured": {"event": "MARKET_DATA_DISCONNECTED", "component": "market_data.alpaca", "attempt": attempt, "error": str(exc)}},
                )
                if not self.reconnect.enabled or attempt > self.reconnect.max_attempts:
                    logger.critical(
                        "market data reconnect exhausted",
                        extra={"structured": {"event": "MARKET_DATA_RECONNECT_EXHAUSTED", "component": "market_data.alpaca"}},
                    )
                    return
                delay = min(
                    self.reconnect.initial_delay_seconds * (2 ** (attempt - 1)),
                    self.reconnect.max_delay_seconds,
                )
                await asyncio.sleep(delay)
            else:
                attempt = 0
                self._connected = False
                if not self._stop.is_set():
                    await asyncio.sleep(self.reconnect.initial_delay_seconds)


def _ts(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"unsupported timestamp: {value!r}")

