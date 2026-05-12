"""
Position sizing for Bybit Linear Perpetuals.

The core formula is IDENTICAL to the MT5 version:
    qty = (balance * risk_per_trade) / risk_per_unit

For Bybit USDT-margined linear contracts:
  - qty is in BASE asset (e.g. BTC for BTCUSDT)
  - PnL = qty_base * (exit_price - entry_price)   [in USDT]
  - risk_per_unit = |entry_price - sl_price|       [in USDT per 1 base unit]

So the formula works directly without any lot-multiplier conversion.

After computing raw qty we:
  1. Normalize to qtyStep (floor — never round up to avoid over-sizing)
  2. Clamp to [minOrderQty, maxOrderQty]
  3. Validate margin availability

This module is pure and has no async code.
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
    risk_per_trade: float,
    entry: float,
    sl: float,
    side: str,
) -> float:
    """
    Raw (un-normalized) quantity from risk sizing.

    Identical to compute_pending_setup() internal formula:
        risk_cash = balance * risk_per_trade
        risk_per_unit = |entry - sl|
        qty = risk_cash / risk_per_unit
    """
    if side == "buy":
        risk_per_unit = entry - sl
    else:
        risk_per_unit = sl - entry

    risk_per_unit = max(risk_per_unit, 1e-12)
    risk_cash = balance * risk_per_trade
    return risk_cash / risk_per_unit


def compute_qty(
    balance: float,
    risk_per_trade: float,
    entry: float,
    sl: float,
    side: str,
    info: InstrumentInfo,
) -> float:
    """
    Full position sizing pipeline: raw qty → normalize → validate.

    Returns the exact qty string-safe float ready to send to Bybit.
    Raises InvalidQtyError if below minimum after normalization.
    """
    raw = compute_raw_qty(balance, risk_per_trade, entry, sl, side)
    qty = normalize_qty(raw, info)

    log.debug(
        "Sizing: balance=%.2f risk=%.4f entry=%.5f sl=%.5f side=%s "
        "→ raw_qty=%.6f → normalized_qty=%.6f",
        balance, risk_per_trade, entry, sl, side, raw, qty,
    )

    if qty < info.min_qty - 1e-9:
        raise InvalidQtyError(
            f"Computed qty {qty} (raw={raw:.6f}) is below minOrderQty={info.min_qty} "
            f"for {info.symbol}. Increase balance/risk or reduce SL distance."
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
    risk_per_trade: float,
    entry: float,
    sl: float,
    tp: float,
    side: str,
    qty: float,
) -> dict:
    """Return a dict with risk/reward breakdown for logging."""
    if side == "buy":
        risk_per_unit = entry - sl
        reward_per_unit = tp - entry
    else:
        risk_per_unit = sl - entry
        reward_per_unit = entry - tp

    risk_cash = risk_per_unit * qty
    reward_cash = reward_per_unit * qty
    rr = reward_per_unit / max(risk_per_unit, 1e-12)

    return {
        "balance": round(balance, 2),
        "risk_pct": round(risk_per_trade * 100, 3),
        "risk_cash": round(risk_cash, 4),
        "reward_cash": round(reward_cash, 4),
        "rr": round(rr, 3),
        "qty": qty,
        "risk_per_unit": round(risk_per_unit, 5),
        "reward_per_unit": round(reward_per_unit, 5),
    }
