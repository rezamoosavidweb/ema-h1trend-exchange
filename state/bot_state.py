"""
Bot runtime state — holds all mutable in-process tracking data.

Responsibilities:
  - Duplicate-candle guard (identical to MT5 restart behavior)
  - Pending order lifecycle tracking (creation time, expiry)
  - Startup recovery detection

State is intentionally NOT persisted to disk (restarts re-sync from exchange).
Exchange is always the source of truth; state is the local cache.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from models.order import PendingOrder, Side

log = logging.getLogger(__name__)


@dataclass
class BotState:
    """
    Thread-safe (asyncio lock) runtime state for one symbol's trading loop.

    Invariants:
      - At most ONE pending order tracked at any time.
      - last_processed_candle is updated only after a successful full cycle.
      - pending is cleared on cancellation, fill, and expiry.
    """

    symbol: str

    # ── Duplicate candle guard ─────────────────────────────────────────────
    last_processed_candle: Optional[pd.Timestamp] = field(default=None)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    # ── Pending order tracking ──────────────────────────────────────────────
    pending: Optional[PendingOrder] = field(default=None)

    # ── Startup recovery flag ───────────────────────────────────────────────
    startup_recovery_done: bool = field(default=False)

    # ── Cycle counters ──────────────────────────────────────────────────────
    cycles_total: int = field(default=0)
    cycles_error: int = field(default=0)

    # ── Lock accessor ────────────────────────────────────────────────────────

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    # ── Candle guard ─────────────────────────────────────────────────────────

    def is_duplicate_candle(self, candle_time: Optional[pd.Timestamp]) -> bool:
        """Return True if we already processed this candle (guard against double-fire)."""
        if candle_time is None or self.last_processed_candle is None:
            return False
        return candle_time == self.last_processed_candle

    def mark_candle_processed(self, candle_time: Optional[pd.Timestamp]) -> None:
        if candle_time is not None:
            self.last_processed_candle = candle_time
            log.debug("Candle processed: %s", candle_time)

    # ── Pending order helpers ─────────────────────────────────────────────────

    def set_pending(self, order: PendingOrder) -> None:
        if self.pending is not None:
            log.warning(
                "Overwriting tracked pending %s with %s",
                self.pending.order_link_id,
                order.order_link_id,
            )
        self.pending = order
        log.debug("Tracking pending order: %s", order)

    def clear_pending(self, reason: str = "") -> None:
        if self.pending is not None:
            log.info("Clearing pending order %s (%s)", self.pending.order_link_id, reason or "unspecified")
        self.pending = None

    def has_pending(self) -> bool:
        return self.pending is not None

    def pending_is_expired(self, expiry_min: int) -> bool:
        if self.pending is None:
            return False
        return self.pending.is_expired(expiry_min)

    def pending_age_minutes(self) -> float:
        if self.pending is None:
            return 0.0
        return self.pending.age_minutes()

    # ── Signal comparison helpers ─────────────────────────────────────────────

    def pending_matches_signal(
        self,
        side: str,
        entry: float,
        sl: float,
        tp: float,
        pip_size: float,
    ) -> bool:
        """
        Compare existing pending against new signal prices.
        Returns True only if side + all prices are within 1 pip tolerance.
        Mirrors the MT5 same_entry / same_sl / same_tp / same_type checks.
        """
        if self.pending is None:
            return False
        tol = pip_size
        return (
            self.pending.side.value == side
            and abs(self.pending.entry - entry) < tol
            and abs(self.pending.sl - sl) < tol
            and abs(self.pending.tp - tp) < tol
        )

    def pending_side_matches(self, side: str) -> bool:
        if self.pending is None:
            return False
        return self.pending.side.value == side

    # ── Startup recovery ──────────────────────────────────────────────────────

    def mark_recovery_done(self) -> None:
        self.startup_recovery_done = True
        log.info("[%s] Startup recovery complete.", self.symbol)

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def summary(self) -> dict:
        return {
            "symbol": self.symbol,
            "last_candle": str(self.last_processed_candle),
            "pending": str(self.pending) if self.pending else None,
            "recovery_done": self.startup_recovery_done,
            "cycles": self.cycles_total,
            "errors": self.cycles_error,
        }


# ── Order link ID generator ───────────────────────────────────────────────────

import hashlib as _hashlib


def make_order_link_id(
    symbol: str,
    magic: int,
    side: str,
    signal_bar_time: pd.Timestamp,
    prefix: str = "ema",
) -> str:
    """
    Deterministic, idempotent orderLinkId (max 36 chars for Bybit).

    Same inputs → same ID → exchange deduplicates on re-submission.
    Pattern: <prefix>-<magic_short>-<side_char>-<bar_unix>
    """
    bar_unix = int(signal_bar_time.timestamp())
    raw = f"{prefix}-{magic}-{side[0]}-{bar_unix}"
    if len(raw) <= 36:
        return raw
    # Hash if too long
    digest = _hashlib.md5(raw.encode()).hexdigest()[:12]
    return f"{prefix}-{digest}"


def make_order_link_id_from_str(
    symbol: str,
    magic: int,
    side: str,
    signal_bar_time_iso: str,
) -> str:
    ts = pd.Timestamp(signal_bar_time_iso)
    return make_order_link_id(symbol, magic, side, ts)
