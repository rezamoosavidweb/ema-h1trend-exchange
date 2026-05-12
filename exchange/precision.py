"""
Price and quantity precision helpers for Bybit Linear Futures.

Every number sent to the API must be a string formatted to the exact
decimal precision returned by instruments_info (tickSize / qtyStep).
Rounding errors cause 'invalid price' and 'invalid qty' rejections.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Union

from core.exceptions import InvalidPriceError, InvalidQtyError
from models.order import InstrumentInfo

Number = Union[float, int, Decimal]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _to_decimal(v: Number) -> Decimal:
    return Decimal(str(v))


def _step_scale(step: float) -> int:
    """Number of decimal places in step (e.g. 0.001 → 3, 0.5 → 1, 1.0 → 0)."""
    d = _to_decimal(step)
    sign, digits, exp = d.as_tuple()
    return max(0, -exp)


# ── Price normalization ────────────────────────────────────────────────────────

def normalize_price(price: float, tick_size: float, *, mode: str = "nearest") -> float:
    """
    Snap price to the nearest valid tick grid point.

    mode:
      "nearest" — standard round
      "up"      — ceil to next tick (used for buy-side floors)
      "down"    — floor to prev tick (used for sell-side caps)
    """
    if tick_size <= 0:
        return price

    t = _to_decimal(tick_size)
    p = _to_decimal(price)
    ratio = p / t

    if mode == "up":
        snapped = (ratio.to_integral_value(rounding=ROUND_HALF_UP) if ratio == ratio.to_integral_value()
                   else ratio.__ceil__()) * t
    elif mode == "down":
        snapped = ratio.__floor__() * t
    else:  # nearest
        snapped = ratio.to_integral_value(rounding=ROUND_HALF_UP) * t

    scale = _step_scale(tick_size)
    return float(round(snapped, scale))


def price_to_str(price: float, tick_size: float) -> str:
    """Format price as string with exactly the right decimal places for the API."""
    scale = _step_scale(tick_size)
    return f"{price:.{scale}f}"


# ── Quantity normalization ─────────────────────────────────────────────────────

def normalize_qty(
    raw_qty: float,
    info: InstrumentInfo,
) -> float:
    """
    Round qty DOWN to the nearest qtyStep and clamp to [minOrderQty, maxOrderQty].

    Bybit rejects qty not divisible by qtyStep.
    Always round DOWN to avoid exceeding intended risk.
    """
    if info.qty_step <= 0:
        return max(info.min_qty, min(raw_qty, info.max_qty))

    step = _to_decimal(info.qty_step)
    qty = _to_decimal(raw_qty)

    # Floor to step boundary (never round up — would increase risk)
    floored = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
    result = float(round(floored, info.qty_scale))

    result = max(info.min_qty, min(result, info.max_qty))
    return result


def qty_to_str(qty: float, info: InstrumentInfo) -> str:
    """Format qty as string with exactly the right decimal places for the API."""
    return f"{qty:.{info.qty_scale}f}"


# ── Validation ────────────────────────────────────────────────────────────────

def validate_price(price: float, info: InstrumentInfo, label: str = "price") -> None:
    if not math.isfinite(price) or price <= 0:
        raise InvalidPriceError(f"{label}={price} is not finite/positive")
    remainder = round(price / info.tick_size, 10) % 1
    if remainder > 1e-8 and remainder < (1 - 1e-8):
        raise InvalidPriceError(
            f"{label}={price} not on tick grid (tickSize={info.tick_size})"
        )


def validate_qty(qty: float, info: InstrumentInfo) -> None:
    if not math.isfinite(qty) or qty <= 0:
        raise InvalidQtyError(f"qty={qty} is not finite/positive")
    if qty < info.min_qty - 1e-9:
        raise InvalidQtyError(
            f"qty={qty} below minOrderQty={info.min_qty} for {info.symbol}"
        )
    if qty > info.max_qty + 1e-9:
        raise InvalidQtyError(
            f"qty={qty} above maxOrderQty={info.max_qty} for {info.symbol}"
        )


def validate_order_geometry(
    side: str,
    entry: float,
    sl: float,
    tp: float,
    info: InstrumentInfo,
) -> None:
    """Ensure SL < entry < TP (buy) or TP < entry < SL (sell) and all on tick grid."""
    validate_price(entry, info, "entry")
    validate_price(sl, info, "sl")
    validate_price(tp, info, "tp")

    if side == "buy":
        if not (sl < entry < tp):
            raise InvalidPriceError(
                f"Buy geometry invalid: sl={sl} < entry={entry} < tp={tp} required"
            )
    else:
        if not (tp < entry < sl):
            raise InvalidPriceError(
                f"Sell geometry invalid: tp={tp} < entry={entry} < sl={sl} required"
            )


# ── Snap full signal prices to tick grid ─────────────────────────────────────

def snap_signal_to_ticks(
    side: str,
    entry: float,
    sl: float,
    tp: float,
    info: InstrumentInfo,
) -> tuple[float, float, float]:
    """
    Snap raw signal prices to Bybit tick grid.

    Buy stop: entry snapped UP (must be above market), sl snapped DOWN, tp snapped UP.
    Sell stop: entry snapped DOWN (must be below market), sl snapped UP, tp snapped DOWN.

    Returns (entry, sl, tp) all on the tick grid.
    """
    if side == "buy":
        entry = normalize_price(entry, info.tick_size, mode="up")
        sl = normalize_price(sl, info.tick_size, mode="down")
        tp = normalize_price(tp, info.tick_size, mode="up")
    else:
        entry = normalize_price(entry, info.tick_size, mode="down")
        sl = normalize_price(sl, info.tick_size, mode="up")
        tp = normalize_price(tp, info.tick_size, mode="down")

    return entry, sl, tp
