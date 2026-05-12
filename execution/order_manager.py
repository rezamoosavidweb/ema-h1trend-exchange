"""
Order Manager — all create / modify / cancel operations on Bybit.

This module is the DIRECT replacement for the MT5 order functions in main.py:
  - create_pending_order()  → place_conditional_order()
  - modify_pending_order()  → amend_conditional_order()
  - remove_pending_order()  → cancel_order()

MT5 → Bybit mapping:
  ORDER_TYPE_BUY_STOP  → side="Buy",  triggerDirection=1 (rises to)
  ORDER_TYPE_SELL_STOP → side="Sell", triggerDirection=2 (falls to)
  TRADE_ACTION_PENDING → place_order(triggerPrice=..., orderType="Limit")
  TRADE_ACTION_MODIFY  → amend_order(orderLinkId=...)
  TRADE_ACTION_REMOVE  → cancel_order(orderLinkId=...)
  ORDER_TIME_GTC       → timeInForce="GTC"
  ORDER_TIME_SPECIFIED + expiry → manual expiry tracking in BotState

SL and TP are attached directly on the conditional order (Bybit supports this).
They become active once the trigger fires and a position is opened.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from core.exceptions import InvalidPriceError, InvalidQtyError, ExchangeError, InsufficientMarginError
from exchange.bybit_client import BybitClient
from exchange.precision import (
    normalize_qty,
    price_to_str,
    qty_to_str,
    snap_signal_to_ticks,
    validate_order_geometry,
    validate_qty,
)
from models.order import InstrumentInfo, PendingOrder, Side, TriggerDirection, WalletBalance
from risk.sizing import check_margin_available, compute_qty, risk_summary
from state.bot_state import BotState, make_order_link_id
from storage.event_journal import EventJournal

log = logging.getLogger(__name__)


class OrderManager:
    """
    Handles all order lifecycle operations for one symbol.

    Mirrors the MT5 sync_pending_orders() logic but uses Bybit conditional orders.
    All methods are async.
    """

    def __init__(
        self,
        client: BybitClient,
        info: InstrumentInfo,
        state: BotState,
        magic: int,
        position_idx: int = 0,
        leverage: int = 1,
        dry_run: bool = False,
        journal: Optional[EventJournal] = None,
    ) -> None:
        self._client = client
        self._info = info
        self._state = state
        self._magic = magic
        self._position_idx = position_idx
        self._leverage = leverage
        self._dry_run = dry_run
        self._journal = journal
        self._last_wallet: Optional[WalletBalance] = None  # set externally before sync

    # ── Create ────────────────────────────────────────────────────────────────

    async def create_pending_order(
        self,
        side: str,
        entry: float,
        sl: float,
        tp: float,
        qty: float,
        signal_bar_time: pd.Timestamp,
    ) -> Optional[PendingOrder]:
        """
        Place a new conditional stop-entry order.

        Equivalent to MT5 TRADE_ACTION_PENDING with BUY_STOP/SELL_STOP.
        SL and TP are attached at creation time (Bybit inline SL/TP).
        """
        # Snap to tick grid
        adj_entry, adj_sl, adj_tp = snap_signal_to_ticks(side, entry, sl, tp, self._info)

        # Validate geometry
        try:
            validate_order_geometry(side, adj_entry, adj_sl, adj_tp, self._info)
        except InvalidPriceError as exc:
            log.error("Price geometry invalid after snap — skipping: %s", exc)
            return None

        # Normalize quantity
        norm_qty = normalize_qty(qty, self._info)
        try:
            validate_qty(norm_qty, self._info)
        except InvalidQtyError as exc:
            log.error("Invalid qty — skipping: %s", exc)
            return None

        order_link_id = make_order_link_id(
            self._info.symbol, self._magic, side, signal_bar_time
        )

        log.info(
            "Creating %s_STOP | link=%s entry=%s sl=%s tp=%s qty=%s signal_bar=%s",
            side.upper(),
            order_link_id,
            price_to_str(adj_entry, self._info.tick_size),
            price_to_str(adj_sl, self._info.tick_size),
            price_to_str(adj_tp, self._info.tick_size),
            qty_to_str(norm_qty, self._info),
            signal_bar_time,
        )

        if self._dry_run:
            log.info("[DRY RUN] Would place order — no actual API call.")
            pending = PendingOrder(
                order_link_id=order_link_id,
                side=Side.BUY if side == "buy" else Side.SELL,
                entry=adj_entry,
                sl=adj_sl,
                tp=adj_tp,
                qty=norm_qty,
                created_at=datetime.now(timezone.utc),
                signal_bar_time=signal_bar_time.to_pydatetime(),
                symbol=self._info.symbol,
            )
            self._state.set_pending(pending)
            if self._journal:
                self._journal.log("order_created", dry_run=True,
                                  link_id=order_link_id, side=side,
                                  entry=adj_entry, sl=adj_sl, tp=adj_tp, qty=norm_qty,
                                  signal_bar=str(signal_bar_time))
            return pending

        bybit_side = "Buy" if side == "buy" else "Sell"
        trigger_dir = TriggerDirection.for_side(
            Side.BUY if side == "buy" else Side.SELL
        ).value

        try:
            result = await self._client.place_conditional_order(
                symbol=self._info.symbol,
                side=bybit_side,
                qty=qty_to_str(norm_qty, self._info),
                trigger_price=price_to_str(adj_entry, self._info.tick_size),
                limit_price=price_to_str(adj_entry, self._info.tick_size),
                sl=price_to_str(adj_sl, self._info.tick_size),
                tp=price_to_str(adj_tp, self._info.tick_size),
                order_link_id=order_link_id,
                trigger_direction=trigger_dir,
                position_idx=self._position_idx,
            )
        except ExchangeError as exc:
            log.error("create_pending_order FAILED: %s", exc)
            if self._journal:
                self._journal.log("order_create_failed", link_id=order_link_id,
                                  side=side, entry=adj_entry, error=str(exc))
            return None

        pending = PendingOrder(
            order_link_id=order_link_id,
            side=Side.BUY if side == "buy" else Side.SELL,
            entry=adj_entry,
            sl=adj_sl,
            tp=adj_tp,
            qty=norm_qty,
            created_at=datetime.now(timezone.utc),
            signal_bar_time=signal_bar_time.to_pydatetime(),
            symbol=self._info.symbol,
            bybit_order_id=result.get("orderId"),
        )
        self._state.set_pending(pending)
        log.info("Pending order CREATED | %s", pending)
        if self._journal:
            self._journal.log("order_created", dry_run=False,
                              link_id=order_link_id, side=side,
                              entry=adj_entry, sl=adj_sl, tp=adj_tp, qty=norm_qty,
                              signal_bar=str(signal_bar_time),
                              bybit_order_id=result.get("orderId"))
        return pending

    # ── Modify ────────────────────────────────────────────────────────────────

    async def modify_pending_order(
        self,
        side: str,
        entry: float,
        sl: float,
        tp: float,
        qty: float,
    ) -> bool:
        """
        Amend prices on the currently tracked pending order.

        Equivalent to MT5 TRADE_ACTION_MODIFY.
        Returns True on success.
        """
        if self._state.pending is None:
            log.error("modify_pending_order called but no pending order tracked.")
            return False

        order_link_id = self._state.pending.order_link_id

        adj_entry, adj_sl, adj_tp = snap_signal_to_ticks(side, entry, sl, tp, self._info)

        try:
            validate_order_geometry(side, adj_entry, adj_sl, adj_tp, self._info)
        except InvalidPriceError as exc:
            log.error("Modify geometry invalid — aborting: %s", exc)
            return False

        norm_qty = normalize_qty(qty, self._info)

        log.info(
            "Modifying pending | link=%s entry=%s sl=%s tp=%s qty=%s",
            order_link_id,
            price_to_str(adj_entry, self._info.tick_size),
            price_to_str(adj_sl, self._info.tick_size),
            price_to_str(adj_tp, self._info.tick_size),
            qty_to_str(norm_qty, self._info),
        )

        if self._dry_run:
            log.info("[DRY RUN] Would amend order.")
            self._state.pending.entry = adj_entry
            self._state.pending.sl = adj_sl
            self._state.pending.tp = adj_tp
            self._state.pending.qty = norm_qty
            if self._journal:
                self._journal.log("order_modified", dry_run=True, link_id=order_link_id,
                                  entry=adj_entry, sl=adj_sl, tp=adj_tp, qty=norm_qty)
            return True

        try:
            await self._client.amend_conditional_order(
                symbol=self._info.symbol,
                order_link_id=order_link_id,
                qty=qty_to_str(norm_qty, self._info),
                trigger_price=price_to_str(adj_entry, self._info.tick_size),
                limit_price=price_to_str(adj_entry, self._info.tick_size),
                sl=price_to_str(adj_sl, self._info.tick_size),
                tp=price_to_str(adj_tp, self._info.tick_size),
            )
        except ExchangeError as exc:
            log.error("modify_pending_order FAILED: %s", exc)
            if self._journal:
                self._journal.log("order_modify_failed", link_id=order_link_id, error=str(exc))
            return False

        self._state.pending.entry = adj_entry
        self._state.pending.sl = adj_sl
        self._state.pending.tp = adj_tp
        self._state.pending.qty = norm_qty
        log.info("Pending order MODIFIED | link=%s", order_link_id)
        if self._journal:
            self._journal.log("order_modified", dry_run=False, link_id=order_link_id,
                              entry=adj_entry, sl=adj_sl, tp=adj_tp, qty=norm_qty)
        return True

    # ── Cancel ────────────────────────────────────────────────────────────────

    async def cancel_pending_order(self, reason: str = "") -> None:
        """
        Cancel the currently tracked pending order.

        Equivalent to MT5 TRADE_ACTION_REMOVE.
        """
        if self._state.pending is None:
            log.debug("cancel_pending_order: no pending order to cancel.")
            return

        order_link_id = self._state.pending.order_link_id
        log.info("Cancelling pending order | link=%s reason=%s", order_link_id, reason or "unspecified")

        if self._journal:
            self._journal.log("order_cancelled", link_id=order_link_id,
                              reason=reason or "unspecified", dry_run=self._dry_run)

        if not self._dry_run:
            await self._client.cancel_order(self._info.symbol, order_link_id)

        self._state.clear_pending(reason)

    async def cancel_all_pending(self, reason: str = "") -> None:
        """Cancel ALL stop orders on this symbol and clear local state."""
        log.info("Cancelling ALL pending orders | symbol=%s reason=%s", self._info.symbol, reason)
        if not self._dry_run:
            await self._client.cancel_all_stop_orders(self._info.symbol)
        self._state.clear_pending(reason)

    # ── Full sync logic (mirrors MT5 sync_pending_orders) ────────────────────

    async def sync_pending_orders(
        self,
        has_position: bool,
        signals_df,
        m5_ctx,
        balance: float,
        risk_per_trade: float,
        pip_size: float,
        pending_expiry_min: int,
        entry_tf_minutes: int = 5,
        wallet: Optional[WalletBalance] = None,
    ) -> None:
        """
        Synchronize exchange pending orders with the latest strategy signal.

        This is the DIRECT replacement of MT5 sync_pending_orders() from main.py.
        Logic is identical:

        1. If position open → return (already in trade)
        2. If no signals → remove pending (if any)
        3. Take last signal, compute bars_passed
        4. If expired (bars_passed >= expiry_bars) → cancel pending
        5. If valid:
           a. Margin check (available_balance >= required_margin)
           b. No pending → create
           c. Pending matches → no action
           d. Pending different side → cancel + recreate
           e. Pending different prices → modify
        """
        expiry_bars = max(1, pending_expiry_min // entry_tf_minutes)

        # ── Step 1: position guard ────────────────────────────────────────────
        if has_position:
            log.info("Open position detected — skipping pending order sync.")
            if self._journal:
                self._journal.log("position_open_skip")
            return

        # ── Step 2: no signals ────────────────────────────────────────────────
        if signals_df is None or (hasattr(signals_df, "empty") and signals_df.empty):
            log.info("No signals — no action on pending orders.")
            if self._journal:
                self._journal.log("no_signal")
            return

        # ── Step 3: last signal ───────────────────────────────────────────────
        last_signal = signals_df.iloc[-1]
        signal_time = pd.to_datetime(last_signal["signal_bar_time"])
        current_time = m5_ctx.index[-1]

        bars_passed = int(
            (current_time - signal_time).total_seconds() / 60 / entry_tf_minutes
        )

        side = last_signal["side"]
        raw_entry = float(last_signal["entry"])
        raw_sl = float(last_signal["sl"])
        raw_tp = float(last_signal["tp"])
        raw_qty = float(last_signal["qty"])

        log.info(
            "Latest signal | side=%s entry=%.5f signal_time=%s bars_passed=%d expiry_bars=%d",
            side, raw_entry, signal_time, bars_passed, expiry_bars,
        )

        # ── Step 4: expired ───────────────────────────────────────────────────
        if bars_passed >= expiry_bars:
            log.info("Signal EXPIRED (bars_passed=%d >= expiry_bars=%d).", bars_passed, expiry_bars)
            if self._journal:
                self._journal.log("signal_expired", bars_passed=bars_passed,
                                  expiry_bars=expiry_bars, side=side, entry=raw_entry)
            if self._state.has_pending():
                await self.cancel_pending_order(reason="signal_expired")
            return

        # ── Step 5: valid signal ──────────────────────────────────────────────
        log.info("Signal VALID.")

        # ── Available balance guard ───────────────────────────────────────────
        if wallet is not None:
            avail = wallet.available_balance
            if avail <= 0:
                log.warning(
                    "Available balance is %.2f USDT — no free margin. Skipping order.", avail
                )
                if self._journal:
                    self._journal.log("margin_blocked", available=avail, reason="zero_balance")
                return

        # Compute properly sized qty using live balance
        try:
            norm_qty = compute_qty(balance, risk_per_trade, raw_entry, raw_sl, side, self._info)
        except (InvalidQtyError, Exception) as exc:
            log.error("Qty computation failed — skipping: %s", exc)
            if self._journal:
                self._journal.log("qty_error", error=str(exc), side=side, entry=raw_entry)
            return

        # Risk summary for logging
        summary = risk_summary(balance, risk_per_trade, raw_entry, raw_sl, raw_tp, side, norm_qty)
        log.info("Risk | %s", " ".join(f"{k}={v}" for k, v in summary.items()))
        if self._journal:
            self._journal.log("risk_sizing", **summary)

        # Validate stale signal: check TP distance didn't collapse after snap
        adj_entry, adj_sl, adj_tp = snap_signal_to_ticks(side, raw_entry, raw_sl, raw_tp, self._info)

        original_sl_dist = abs(raw_entry - raw_sl)
        if side == "buy":
            tp_dist = adj_tp - adj_entry
        else:
            tp_dist = adj_entry - adj_tp

        if tp_dist < original_sl_dist * 0.5:
            log.warning(
                "Stale signal — entry snapped past market (tp_dist=%.5f < min=%.5f). Skipping.",
                tp_dist, original_sl_dist * 0.5,
            )
            if self._journal:
                self._journal.log("signal_stale", tp_dist=tp_dist,
                                  min_dist=original_sl_dist * 0.5, entry=raw_entry)
            return

        # ── Margin check ──────────────────────────────────────────────────────
        if wallet is not None and not self._dry_run:
            try:
                check_margin_available(wallet, norm_qty, adj_entry, self._leverage)
            except InsufficientMarginError as exc:
                log.error("MARGIN CHECK FAILED — skipping order: %s", exc)
                if self._journal:
                    self._journal.log("margin_failed", error=str(exc),
                                      qty=norm_qty, entry=adj_entry, leverage=self._leverage)
                return

        # ── 5b: no existing pending → create ─────────────────────────────────
        if not self._state.has_pending():
            log.info("No existing pending — creating new.")
            await self.create_pending_order(
                side, raw_entry, raw_sl, raw_tp, norm_qty, signal_time
            )
            return

        # ── 5b / 5c / 5d: existing pending ───────────────────────────────────
        log.info("Checking existing pending for changes (link=%s).", self._state.pending.order_link_id)

        if self._state.pending_matches_signal(side, adj_entry, adj_sl, adj_tp, pip_size):
            log.info("Pending order already up-to-date — no action.")
            return

        if not self._state.pending_side_matches(side):
            # Different side — cannot amend; must cancel and recreate
            log.info(
                "Order side changed (%s → %s) — cancelling and recreating.",
                self._state.pending.side.value, side,
            )
            await self.cancel_pending_order(reason="side_changed")
            await self.create_pending_order(
                side, raw_entry, raw_sl, raw_tp, norm_qty, signal_time
            )
        else:
            # Same side, different prices → modify
            log.info("Pending prices changed — modifying.")
            success = await self.modify_pending_order(side, raw_entry, raw_sl, raw_tp, norm_qty)
            if not success:
                # Amend failed (possibly already filled/cancelled) — recreate
                log.warning("Amend failed — attempting cancel + recreate.")
                await self.cancel_pending_order(reason="amend_failed")
                await self.create_pending_order(
                    side, raw_entry, raw_sl, raw_tp, norm_qty, signal_time
                )
