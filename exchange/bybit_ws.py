"""
WebSocket client scaffold for Bybit — websocket-ready architecture.

Currently this is a SKELETON. The bot runs correctly via REST polling only.
This module exists to define the interface so the trading service can be
extended to use real-time feeds without changing the strategy layer.

WebSocket endpoints (Bybit v5):
  Public:  wss://stream.bybit.com/v5/public/linear
  Private: wss://stream.bybit.com/v5/private

Topics of interest:
  publicTrade.*        — real-time trades (for last-price triggers)
  kline.<interval>.*   — real-time OHLCV (replaces REST polling)
  orderbook.*          — bid/ask
  order                — private: order updates (fill/cancel/trigger)
  position             — private: position changes

Usage (future):
    ws = BybitWebSocket(api_key=..., api_secret=...)
    await ws.connect()
    async for event in ws.order_events():
        ...
    await ws.disconnect()
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

log = logging.getLogger(__name__)


class BybitWebSocket:
    """
    Bybit WebSocket client scaffold.

    Provides an async context manager and async iterators for market events.
    Replace the pass/NotImplementedError bodies with real pybit WebSocket
    calls once real-time feeds are required.
    """

    PUBLIC_WS_URL = "wss://stream.bybit.com/v5/public/linear"
    PUBLIC_WS_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"
    PRIVATE_WS_URL = "wss://stream.bybit.com/v5/private"
    PRIVATE_WS_TESTNET = "wss://stream-testnet.bybit.com/v5/private"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        testnet: bool = False,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = testnet
        self._connected = False
        self._order_queue: asyncio.Queue = asyncio.Queue()
        self._kline_queue: asyncio.Queue = asyncio.Queue()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to Bybit WebSocket endpoints."""
        # TODO: implement using websockets library or pybit WebSocket client
        log.warning(
            "BybitWebSocket.connect() is a stub — REST polling is used instead. "
            "Implement with 'websockets' library for real-time feeds."
        )
        self._connected = False

    async def disconnect(self) -> None:
        self._connected = False
        log.info("WebSocket disconnected.")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Subscriptions ─────────────────────────────────────────────────────────

    async def subscribe_kline(self, symbol: str, interval: str) -> None:
        """Subscribe to kline (OHLCV) updates for one symbol."""
        # topic: kline.5.BTCUSDT
        topic = f"kline.{interval}.{symbol}"
        log.debug("WS subscribe (stub): %s", topic)

    async def subscribe_orders(self) -> None:
        """Subscribe to private order updates (fills, cancels, triggers)."""
        log.debug("WS subscribe (stub): order")

    async def subscribe_positions(self) -> None:
        """Subscribe to private position updates."""
        log.debug("WS subscribe (stub): position")

    # ── Async iterators ───────────────────────────────────────────────────────

    async def kline_events(self, symbol: str, interval: str) -> AsyncIterator[dict]:
        """
        Async generator yielding OHLCV updates.
        Stub: yields nothing — REST polling in DataFetcher is used instead.
        """
        await self.subscribe_kline(symbol, interval)
        # In production: read from self._kline_queue filled by the WS receive loop
        return
        yield  # makes this a generator

    async def order_events(self) -> AsyncIterator[dict]:
        """
        Async generator yielding private order events (fills, triggers, cancels).
        Stub: yields nothing — polling via get_open_orders is used instead.

        In production, these events would:
          - Update BotState when a pending order fills → clear pending
          - Trigger position updates
          - Alert on unexpected cancellations
        """
        await self.subscribe_orders()
        return
        yield

    async def position_events(self) -> AsyncIterator[dict]:
        """Async generator yielding position change events."""
        await self.subscribe_positions()
        return
        yield
