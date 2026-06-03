"""
WebSocket Order Notifier — subscribes to Bybit private WebSocket and sends
Telegram notifications for every order/position event in real time.

One instance is shared across all symbols (Bybit private WS streams
all symbols for the account simultaneously).

Usage:
    notifier = WsOrderNotifier(settings, send_fn=tg_bot.send)
    await notifier.run()          # blocks until stop() is called
    await notifier.stop()
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union, TYPE_CHECKING

from config.settings import Settings
from exchange.bybit_ws import BybitPrivateWebSocket
from storage.ws_event_log import WsEventJournal
from telegram_bot.formatters import format_order_event, format_position_event

if TYPE_CHECKING:
    from services.account_pnl_tracker import AccountPnLTracker

log = logging.getLogger(__name__)


class WsOrderNotifier:
    """
    Connects to Bybit private WebSocket and forwards order/position events
    to Telegram via an async send function.

    Only sends notifications when an order changes state meaningfully —
    ping/auth frames and empty messages are silently dropped.
    """

    # Statuses worth notifying about (suppress noisy intermediate states)
    _NOTIFY_STATUSES = frozenset({
        "untriggered",    # new conditional order placed
        "triggered",      # stop order triggered → entering market
        "filled",         # fully filled
        "partiallyfilled",
        "cancelled",
        "deactivated",
        "rejected",
    })

    def __init__(
        self,
        settings: Settings,
        send_fn: Callable[[str], Awaitable[None]],
        pnl_tracker: Optional["AccountPnLTracker"] = None,
        log_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self._cfg     = settings
        self._send    = send_fn
        self._tracker = pnl_tracker
        self._journal = WsEventJournal(log_dir)
        self._stop    = asyncio.Event()
        self._ws      = BybitPrivateWebSocket(
            api_key    = settings.bybit_api_key,
            api_secret = settings.bybit_api_secret,
            testnet    = settings.bybit_testnet,
            demo       = settings.bybit_demo,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect to WebSocket and stream events until stop() is called."""
        log.info("WsOrderNotifier starting…")
        await self._ws.connect()

        if not self._ws.is_connected:
            log.warning(
                "WsOrderNotifier: WebSocket did not connect — "
                "Telegram order notifications are inactive."
            )
            await self._stop.wait()
            return

        log.info("WsOrderNotifier active — listening for order/position events.")
        try:
            async for event in self._ws.events(stop_event=self._stop):
                await self._dispatch(event)
        finally:
            await self._ws.disconnect()
            log.info("WsOrderNotifier stopped.")

    async def stop(self) -> None:
        """Signal graceful shutdown."""
        self._stop.set()

    # ── Event dispatch ────────────────────────────────────────────────────────

    async def _dispatch(self, event: dict) -> None:
        topic = event.get("topic", "")

        if "order" in topic:
            # Persist a faithful copy *before* the Telegram-notify filter so the
            # journal captures every order event, not just the notable ones.
            self._journal.log_event(event)
            msg = self._make_order_msg(event)
        elif "position" in topic:
            self._journal.log_event(event)
            account_pnl = self._update_pnl_tracker(event)
            msg = format_position_event(event, account_pnl=account_pnl)
        else:
            return

        if not msg:
            return

        try:
            await self._send(msg)
        except Exception as exc:
            log.warning("WsOrderNotifier: Telegram send failed: %s", exc)

    def _update_pnl_tracker(self, event: dict) -> Optional[float]:
        """Add each closed position's realisedPnl to the tracker; return total."""
        if self._tracker is None:
            return None
        any_closed = False
        for pos in event.get("data", []) or []:
            try:
                size = float(pos.get("size") or 0)
            except (TypeError, ValueError):
                size = 0.0 if pos.get("size") in ("0", "") else float("nan")
            if size == 0:
                any_closed = True
                try:
                    rpnl = float(pos.get("realisedPnl") or 0)
                except (TypeError, ValueError):
                    rpnl = 0.0
                self._tracker.add(rpnl)
        return self._tracker.total if any_closed else None

    def _make_order_msg(self, event: dict) -> str:
        """Only notify for meaningful status changes; suppress noisy updates."""
        data = event.get("data", [])

        # Filter to orders with a notifiable status
        relevant = [
            o for o in data
            if (o.get("orderStatus") or "").lower() in self._NOTIFY_STATUSES
        ]
        if not relevant:
            return ""

        # Rebuild event with only relevant orders
        filtered = dict(event)
        filtered["data"] = relevant
        return format_order_event(filtered)
