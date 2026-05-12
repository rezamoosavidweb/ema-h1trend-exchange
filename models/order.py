"""
Domain models for orders, positions and signals.

These are pure data containers with no external dependencies —
safe to use in any layer of the application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ── Enums ─────────────────────────────────────────────────────────────────────

class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    def bybit(self) -> str:
        """Bybit API string: 'Buy' or 'Sell'."""
        return "Buy" if self == Side.BUY else "Sell"

    def opposite(self) -> "Side":
        return Side.SELL if self == Side.BUY else Side.BUY


class OrderStatus(str, Enum):
    PENDING = "pending"       # conditional/stop order waiting for trigger
    TRIGGERED = "triggered"   # trigger hit, limit order active
    FILLED = "filled"         # fully executed
    CANCELLED = "cancelled"
    EXPIRED = "expired"       # manually expired by the bot


class TriggerDirection(int, Enum):
    RISES_TO = 1   # BUY_STOP equivalent — triggers when price rises to triggerPrice
    FALLS_TO = 2   # SELL_STOP equivalent — triggers when price falls to triggerPrice

    @classmethod
    def for_side(cls, side: Side) -> "TriggerDirection":
        return cls.RISES_TO if side == Side.BUY else cls.FALLS_TO


# ── Signal (output of strategy layer) ────────────────────────────────────────

@dataclass(frozen=True)
class Signal:
    """Raw output of compute_pending_setup() enriched with bar metadata."""
    side: Side
    entry: float
    sl: float
    tp: float
    qty: float
    signal_bar_time: datetime
    trend: str            # "bull" | "bear"
    pip_size: float       # used for price comparison tolerance

    @property
    def risk_per_unit(self) -> float:
        if self.side == Side.BUY:
            return self.entry - self.sl
        return self.sl - self.entry

    def is_geometry_valid(self) -> bool:
        if self.side == Side.BUY:
            return self.sl < self.entry < self.tp
        return self.tp < self.entry < self.sl

    def __str__(self) -> str:
        return (
            f"Signal({self.side.value} entry={self.entry:.5f} sl={self.sl:.5f} "
            f"tp={self.tp:.5f} qty={self.qty:.4f} bar={self.signal_bar_time.isoformat()})"
        )


# ── Pending order (tracked in bot state) ─────────────────────────────────────

@dataclass
class PendingOrder:
    """Represents one live conditional/stop order on Bybit."""
    order_link_id: str
    side: Side
    entry: float
    sl: float
    tp: float
    qty: float
    created_at: datetime
    signal_bar_time: datetime
    symbol: str
    bybit_order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING

    def is_same_as_signal(self, sig: Signal, tolerance: float) -> bool:
        """True if all prices and side match within tolerance (no modify needed)."""
        return (
            self.side == sig.side
            and abs(self.entry - sig.entry) < tolerance
            and abs(self.sl - sig.sl) < tolerance
            and abs(self.tp - sig.tp) < tolerance
        )

    def age_minutes(self, now: Optional[datetime] = None) -> float:
        t = now or datetime.now(timezone.utc)
        if self.signal_bar_time.tzinfo is None:
            ref = self.signal_bar_time.replace(tzinfo=timezone.utc)
        else:
            ref = self.signal_bar_time
        return (t - ref).total_seconds() / 60.0

    def is_expired(self, expiry_min: int, now: Optional[datetime] = None) -> bool:
        return self.age_minutes(now) >= expiry_min

    def __str__(self) -> str:
        return (
            f"PendingOrder(link={self.order_link_id} {self.side.value} "
            f"entry={self.entry:.5f} sl={self.sl:.5f} tp={self.tp:.5f} "
            f"qty={self.qty:.4f} status={self.status.value})"
        )


# ── Position ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Position:
    """A live position returned from Bybit get_positions()."""
    symbol: str
    side: Side
    size: float          # contracts (base asset qty)
    entry_price: float
    unrealized_pnl: float
    leverage: float
    position_idx: int    # 0=one-way, 1=buy-hedge, 2=sell-hedge

    @property
    def is_open(self) -> bool:
        return self.size > 0


# ── Instrument info (fetched once and cached) ─────────────────────────────────

@dataclass(frozen=True)
class InstrumentInfo:
    """Tick size + quantity constraints for one Bybit symbol."""
    symbol: str
    tick_size: float      # priceFilter.tickSize
    qty_step: float       # lotSizeFilter.qtyStep
    min_qty: float        # lotSizeFilter.minOrderQty
    max_qty: float        # lotSizeFilter.maxOrderQty
    min_notional: float   # lotSizeFilter.minOrderAmt  (if available, else 0)
    price_scale: int      # decimal places for price
    qty_scale: int        # decimal places for qty

    @classmethod
    def from_bybit(cls, raw: dict) -> "InstrumentInfo":
        pf = raw["priceFilter"]
        lf = raw["lotSizeFilter"]
        tick = float(pf["tickSize"])
        step = float(lf["qtyStep"])

        def _scale(v: float) -> int:
            s = f"{v:.10f}".rstrip("0")
            return len(s.split(".")[1]) if "." in s else 0

        return cls(
            symbol=raw["symbol"],
            tick_size=tick,
            qty_step=step,
            min_qty=float(lf["minOrderQty"]),
            max_qty=float(lf["maxOrderQty"]),
            min_notional=float(lf.get("minOrderAmt", 0) or 0),
            price_scale=_scale(tick),
            qty_scale=_scale(step),
        )


# ── Wallet / balance ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WalletBalance:
    """USDT unified wallet snapshot."""
    total_equity: float
    available_balance: float
    used_margin: float
    coin: str = "USDT"

    @property
    def free(self) -> float:
        return self.available_balance
