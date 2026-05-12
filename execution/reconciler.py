"""
Order Reconciler — startup and periodic synchronization between local state
and the actual exchange state.

Runs at startup (after connect) and can be invoked periodically to detect:
  - Orphan orders (orders on exchange not tracked in local state)
  - Stale pendings (tracked locally but already filled/cancelled on exchange)
  - Orders from previous sessions (restart recovery)

This handles the Bybit-specific problem that unlike MT5 (which has persistent
terminal state), a Python bot restart loses all local tracking.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from core.constants import ORDER_LINK_ID_PREFIX
from exchange.bybit_client import BybitClient
from models.order import OrderStatus, PendingOrder, Side
from state.bot_state import BotState
from storage.event_journal import EventJournal

log = logging.getLogger(__name__)


class OrderReconciler:
    """
    Exchange ↔ local state reconciliation.

    Call `await reconciler.reconcile_on_startup()` once before the main loop.
    Call `await reconciler.reconcile_cycle()` each loop iteration to keep
    local state consistent with the exchange (catches fills, cancellations).
    """

    def __init__(
        self,
        client: BybitClient,
        state: BotState,
        magic: int,
        symbol: str,
        journal: Optional[EventJournal] = None,
    ) -> None:
        self._client = client
        self._state = state
        self._magic = magic
        self._symbol = symbol
        self._journal = journal

    # ── Startup recovery ──────────────────────────────────────────────────────

    async def reconcile_on_startup(self) -> None:
        """
        Run on bot startup to discover any pre-existing orders from a prior session.

        Behavior:
        1. Check for open positions — if found, log and DON'T touch (let them run).
        2. Check for orphan stop orders belonging to our magic prefix.
        3. If found: adopt the most recent one into local state.
        4. Cancel any stale duplicates (more than one pending is a race condition).
        """
        log.info("[%s] Running startup reconciliation...", self._symbol)

        # ── Check open positions ──────────────────────────────────────────────
        positions = await self._client.get_positions(self._symbol)
        if positions:
            log.info(
                "[%s] Found %d open position(s) at startup. Will not place pending orders "
                "until positions close.",
                self._symbol,
                len(positions),
            )
            for p in positions:
                log.info(
                    "  Position: side=%s size=%s entry=%.5f",
                    p.side.value, p.size, p.entry_price,
                )

        # ── Check orphan stop orders ──────────────────────────────────────────
        raw_orders = await self._client.get_open_stop_orders(self._symbol)
        our_orders = [
            o for o in raw_orders
            if o.get("orderLinkId", "").startswith(f"{ORDER_LINK_ID_PREFIX}-")
        ]

        if not our_orders:
            log.info("[%s] No orphan orders found at startup.", self._symbol)
            self._state.mark_recovery_done()
            return

        log.info("[%s] Found %d orphan order(s) from previous session.", self._symbol, len(our_orders))
        for o in our_orders:
            log.info(
                "  Orphan: link=%s side=%s triggerPrice=%s status=%s",
                o.get("orderLinkId"),
                o.get("side"),
                o.get("triggerPrice"),
                o.get("orderStatus"),
            )

        # Keep the most recent one (highest unix time in link ID suffix)
        def _link_sort_key(o: dict) -> int:
            link = o.get("orderLinkId", "")
            parts = link.rsplit("-", 1)
            try:
                return int(parts[-1])
            except ValueError:
                return 0

        our_orders.sort(key=_link_sort_key, reverse=True)
        latest = our_orders[0]
        stale = our_orders[1:]

        # Cancel duplicates
        for orphan in stale:
            link = orphan.get("orderLinkId", "")
            log.info("Cancelling stale duplicate orphan: %s", link)
            await self._client.cancel_order(self._symbol, link)

        # Adopt latest into state
        adopted = _raw_order_to_pending(latest, self._symbol)
        if adopted is not None:
            self._state.set_pending(adopted)
            log.info("[%s] Adopted orphan into state: %s", self._symbol, adopted)
            if self._journal:
                self._journal.log("orphan_adopted",
                                  link_id=adopted.order_link_id,
                                  side=adopted.side.value,
                                  entry=adopted.entry)

        self._state.mark_recovery_done()

    # ── Per-cycle check ───────────────────────────────────────────────────────

    async def reconcile_cycle(self) -> None:
        """
        Lightweight per-cycle check: verify tracked pending still exists on exchange.

        If our tracked pending is no longer visible on Bybit (filled, cancelled,
        or triggered), clear it from local state so next cycle can create a fresh one.
        """
        if not self._state.has_pending():
            return

        link_id = self._state.pending.order_link_id
        order = await self._client.get_order_by_link_id(self._symbol, link_id)

        if order is None:
            log.info(
                "Tracked pending %s no longer on exchange (filled/cancelled/triggered). "
                "Clearing local state.",
                link_id,
            )
            if self._journal:
                self._journal.log("order_cleared", link_id=link_id,
                                  reason="not_on_exchange")
            self._state.clear_pending(reason="not_on_exchange")
            return

        status = order.get("orderStatus", "").lower()
        if status in ("cancelled", "deactivated", "filled"):
            log.info("Tracked pending %s status=%s — clearing.", link_id, status)
            if self._journal:
                self._journal.log("order_cleared", link_id=link_id,
                                  reason=f"exchange_status_{status}",
                                  exchange_status=status)
            self._state.clear_pending(reason=f"exchange_status_{status}")

    # ── Orphan cleanup ────────────────────────────────────────────────────────

    async def cleanup_orphan_orders(self) -> int:
        """
        Cancel all stop orders that belong to our magic prefix but are NOT the
        currently tracked pending. Returns number of orders cancelled.
        """
        raw_orders = await self._client.get_open_stop_orders(self._symbol)
        our_prefix = f"{ORDER_LINK_ID_PREFIX}-"
        tracked_link = self._state.pending.order_link_id if self._state.pending else None

        cancelled = 0
        for o in raw_orders:
            link = o.get("orderLinkId", "")
            if not link.startswith(our_prefix):
                continue
            if link == tracked_link:
                continue
            log.info("Cancelling orphan order: %s", link)
            await self._client.cancel_order(self._symbol, link)
            cancelled += 1

        if cancelled:
            log.info("Cleaned up %d orphan order(s).", cancelled)
        return cancelled


# ── Helper ────────────────────────────────────────────────────────────────────

def _raw_order_to_pending(raw: dict, symbol: str) -> Optional[PendingOrder]:
    """Convert a Bybit raw order dict to a PendingOrder for state adoption."""
    try:
        link = raw.get("orderLinkId", "")
        side_str = raw.get("side", "").lower()
        side = Side.BUY if side_str == "buy" else Side.SELL
        entry = float(raw.get("triggerPrice", raw.get("price", 0)) or 0)
        sl = float(raw.get("stopLoss", 0) or 0)
        tp = float(raw.get("takeProfit", 0) or 0)
        qty = float(raw.get("qty", raw.get("leaves_qty", 0)) or 0)

        # Try to parse signal bar time from the link ID suffix
        parts = link.rsplit("-", 1)
        try:
            bar_ts = pd.Timestamp(int(parts[-1]), unit="s", tz="UTC").to_pydatetime()
        except (ValueError, IndexError):
            bar_ts = datetime.now(timezone.utc)

        return PendingOrder(
            order_link_id=link,
            side=side,
            entry=entry,
            sl=sl,
            tp=tp,
            qty=qty,
            created_at=datetime.now(timezone.utc),
            signal_bar_time=bar_ts,
            symbol=symbol,
            bybit_order_id=raw.get("orderId"),
            status=OrderStatus.PENDING,
        )
    except Exception as exc:
        log.warning("Could not parse raw order into PendingOrder: %s — %s", raw, exc)
        return None
