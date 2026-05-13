"""
Position sizing for Bybit Linear Perpetuals — margin-based model.

Formula:
    margin  = balance * margin_pct          (e.g. 1% of wallet)
    qty     = (margin * leverage) / entry   (base-asset units)

The exchange requires exactly `margin` USDT of free balance regardless of
leverage. With higher leverage the position notional grows but margin stays
fixed at margin_pct% of wallet.

After computing raw qty we:
  1. Normalize to qtyStep (floor — never round up to avoid over-sizing)
  2. Clamp to [minOrderQty, maxOrderQty]
  3. Validate margin availability
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from core.exceptions import InsufficientMarginError, InvalidQtyError
from exchange.precision import normalize_qty
from models.order import InstrumentInfo, WalletBalance

log = logging.getLogger(__name__)


def compute_raw_qty(
    balance: float,
    margin_pct: float,
    entry: float,
    leverage: int,
) -> float:
    """
    Raw (un-normalized) quantity from margin-based sizing.

        margin = balance * margin_pct
        qty    = (margin * leverage) / entry

    The actual margin committed to the exchange is always `margin` USDT.
    Leverage multiplies position size but not the capital committed.
    """
    margin = balance * margin_pct
    return (margin * leverage) / max(entry, 1e-12)


def compute_qty(
    balance: float,
    margin_pct: float,
    entry: float,
    leverage: int,
    info: InstrumentInfo,
) -> float:
    """
    Full position sizing pipeline: raw qty → normalize → validate.

    Returns the exact qty string-safe float ready to send to Bybit.
    Raises InvalidQtyError if below minimum after normalization.
    """
    raw = compute_raw_qty(balance, margin_pct, entry, leverage)
    qty = normalize_qty(raw, info)

    log.debug(
        "Sizing: balance=%.2f margin_pct=%.4f entry=%.5f leverage=%dx "
        "→ margin=%.4f → raw_qty=%.6f → normalized_qty=%.6f",
        balance, margin_pct, entry, leverage,
        balance * margin_pct, raw, qty,
    )

    if qty < info.min_qty - 1e-9:
        raise InvalidQtyError(
            f"Computed qty {qty} (raw={raw:.6f}) is below minOrderQty={info.min_qty} "
            f"for {info.symbol}. Increase balance/margin_pct or leverage."
        )

    return qty


def check_margin_available(
    balance: WalletBalance,
    qty: float,
    entry: float,
    leverage: int,
    min_free_fraction: float = 0.05,
) -> None:
    """
    Verify there is enough free margin to open the position.

    required_margin = (qty * entry) / leverage
    Raises InsufficientMarginError if free balance < required * (1 + safety_buffer).
    """
    if leverage <= 0:
        leverage = 1
    required_margin = (qty * entry) / leverage
    safety = required_margin * 1.1  # 10% buffer for fees + price movement
    free = balance.available_balance

    if free < safety:
        raise InsufficientMarginError(
            f"Insufficient margin: need ≈{safety:.2f} USDT (incl. buffer), "
            f"have {free:.2f} USDT free. "
            f"(qty={qty} entry={entry} leverage={leverage}x)"
        )


def risk_summary(
    balance: float,
    margin_pct: float,
    entry: float,
    sl: float,
    tp: float,
    side: str,
    qty: float,
    leverage: int = 1,
) -> dict:
    """Return a dict with margin/risk breakdown for logging."""
    if side == "buy":
        risk_per_unit = entry - sl
        reward_per_unit = tp - entry
    else:
        risk_per_unit = sl - entry
        reward_per_unit = entry - tp

    margin_used = (qty * entry) / leverage
    risk_cash = risk_per_unit * qty
    reward_cash = reward_per_unit * qty
    rr = reward_per_unit / max(risk_per_unit, 1e-12)

    return {
        "balance": round(balance, 2),
        "margin_pct": round(margin_pct * 100, 3),
        "margin_used": round(margin_used, 4),
        "leverage": leverage,
        "risk_cash": round(risk_cash, 4),
        "reward_cash": round(reward_cash, 4),
        "rr": round(rr, 3),
        "qty": qty,
        "risk_per_unit": round(risk_per_unit, 5),
        "reward_per_unit": round(reward_per_unit, 5),
    }
