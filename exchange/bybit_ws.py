"""
Bybit private WebSocket client — real-time order and position notifications.

Connects to the Bybit v5 private WebSocket and subscribes to:
  - order    : all order lifecycle events (created, triggered, filled, cancelled)
  - position : position open/close/change events

Architecture: pybit's WebSocket runs in a background thread with callbacks.
              Events are bridged into asyncio via a thread-safe Queue.

Usage:
    ws = BybitPrivateWebSocket(api_key, api_secret, demo=True)
    await ws.connect()
    async for event in ws.events(stop_event=shutdown):
        handle(event)
    await ws.disconnect()
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

log = logging.getLogger(__name__)


class BybitPrivateWebSocket:
    """
    Real-time private WebSocket for Bybit order/position notifications.

    Wraps pybit's thread-based WebSocket and exposes an async iterator
    so the caller stays fully in asyncio.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        testnet: bool = False,
        demo: bool = False,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = testnet
        self._demo = demo

        # Events arrive from pybit's thread → put into this queue
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1024)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        self._connected = False

    # ── Thread callback ───────────────────────────────────────────────────────

    def _on_message(self, message: dict) -> None:
        """Called from pybit's internal thread — bridge to asyncio."""
        if self._loop is None or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, message)
        except asyncio.QueueFull:
            log.warning("WS event queue full — dropping message: topic=%s", message.get("topic"))
        except Exception as exc:
            log.debug("WS bridge error: %s", exc)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Start pybit WebSocket in background thread and subscribe to private topics."""
        self._loop = asyncio.get_event_loop()

        try:
            from pybit.unified_trading import WebSocket as _PybitWS
        except ImportError:
            log.error("pybit not installed — install with: pip install pybit>=5.8.0")
            return

        kwargs: dict = dict(
            testnet=self._testnet,
            channel_type="private",
            api_key=self._api_key,
            api_secret=self._api_secret,
        )
        if self._demo:
            kwargs["demo"] = True

        try:
            self._ws = _PybitWS(**kwargs)
            self._ws.order_stream(callback=self._on_message)
            self._ws.position_stream(callback=self._on_message)
            self._connected = True
            mode = "DEMO" if self._demo else ("TESTNET" if self._testnet else "LIVE")
            log.info("Bybit private WebSocket connected | mode=%s", mode)
        except Exception as exc:
            log.error("Bybit WebSocket connection failed: %s", exc)
            self._connected = False

    async def disconnect(self) -> None:
        """Stop the pybit WebSocket thread."""
        if self._ws is not None:
            try:
                self._ws.exit()
            except Exception:
                pass
        self._connected = False
        log.info("Bybit private WebSocket disconnected.")

    # ── Async iterator ────────────────────────────────────────────────────────

    async def events(
        self,
        stop_event: Optional[asyncio.Event] = None,
    ) -> AsyncIterator[dict]:
        """
        Async generator yielding order/position dicts from the WebSocket.
        Exits cleanly when stop_event is set or connection drops.
        """
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                # Skip auth/ping/subscription confirmation frames
                if event.get("op") in ("auth", "subscribe", "ping", "pong"):
                    continue
                if "topic" not in event:
                    continue
                yield event
            except asyncio.TimeoutError:
                continue

    @property
    def is_connected(self) -> bool:
        return self._connected
