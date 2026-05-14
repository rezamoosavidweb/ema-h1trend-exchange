"""
Position sizing for Bybit Linear Perpetuals — fixed-USDT risk model.

Formula:
    sl_dist = abs(entry - sl)       (price distance to stop)
    qty     = risk_cash / sl_dist   (base-asset units)

risk_cash is a fixed dollar amount (e.g. $20) set via RISK_FIXED_USDT.
This guarantees every trade wins/loses exactly risk_cash USDT at TP/SL,
regardless of SL distance or symbol price level.

After computing raw qty we:
  1. Validate raw qty >= minOrderQty (raise InvalidQtyError if not)
  2. Normalize to qtyStep (floor — never round up to avoid over-sizing)
  3. Clamp to maxOrderQty
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
    risk_cash: float,
    entry: float,
    sl: float,
) -> float:
    """
    Raw (un-normalized) quantity from fixed-USDT risk sizing.

        sl_dist = abs(entry - sl)
        qty     = risk_cash / sl_dist

    Ensures loss at SL == risk_cash and profit at TP == risk_cash * RR.
    """
    sl_dist = abs(entry - sl)
    if sl_dist < 1e-12:
        raise InvalidQtyError(
            f"SL distance near zero (entry={entry} sl={sl}) — cannot size position."
        )
    return risk_cash / sl_dist


def compute_qty(
    risk_cash: float,
    entry: float,
    sl: float,
    leverage: int,
    info: InstrumentInfo,
) -> float:
    """
    Full position sizing pipeline: raw qty → validate → normalize.

    Returns the exact qty string-safe float ready to send to Bybit.
    Raises InvalidQtyError if below minOrderQty.
    leverage is kept for margin-check callers but does not affect qty.
    """
    raw = compute_raw_qty(risk_cash, entry, sl)

    if raw < info.min_qty:
        raise InvalidQtyError(
            f"Computed qty {raw:.6f} is below minOrderQty={info.min_qty} "
            f"for {info.symbol}. Increase RISK_FIXED_USDT or tighten SL distance."
        )

    qty = normalize_qty(raw, info)

    log.debug(
        "Sizing: risk_cash=%.4f entry=%.5f sl=%.5f "
        "sl_dist=%.5f → raw_qty=%.6f → normalized_qty=%.6f",
        risk_cash, entry, sl, abs(entry - sl), raw, qty,
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


def compute_raw_qty_fee_adjusted(
    risk_net: float,
    entry: float,
    sl: float,
    entry_fee_rate: float,
    exit_fee_rate: float,
) -> float:
    """
    Fee-adjusted position sizing: net P&L at SL == -risk_net after fees.

        fee_per_unit = entry * (entry_fee_rate + exit_fee_rate)
        qty = risk_net / (sl_dist + fee_per_unit)

    With this qty and fee_adjusted_tp(), both win (at TP) and loss (at SL)
    net exactly risk_net USDT after trading fees.
    """
    sl_dist = abs(entry - sl)
    if sl_dist < 1e-12:
        raise InvalidQtyError(
            f"SL distance near zero (entry={entry} sl={sl}) — cannot size position."
        )
    fee_per_unit = entry * (entry_fee_rate + exit_fee_rate)
    return risk_net / (sl_dist + fee_per_unit)


def compute_qty_fee_adjusted(
    risk_net: float,
    entry: float,
    sl: float,
    entry_fee_rate: float,
    exit_fee_rate: float,
    leverage: int,
    info: InstrumentInfo,
) -> float:
    """Full pipeline for fee-adjusted sizing: raw → validate → normalize."""
    raw = compute_raw_qty_fee_adjusted(risk_net, entry, sl, entry_fee_rate, exit_fee_rate)

    if raw < info.min_qty:
        raise InvalidQtyError(
            f"Computed qty {raw:.6f} is below minOrderQty={info.min_qty} "
            f"for {info.symbol}. Increase RISK_FIXED_USDT or tighten SL distance."
        )

    qty = normalize_qty(raw, info)

    fee_per_unit = entry * (entry_fee_rate + exit_fee_rate)
    log.debug(
        "Fee-adjusted sizing: risk_net=%.4f entry=%.5f sl=%.5f "
        "sl_dist=%.5f fee_per_unit=%.5f → raw_qty=%.6f → normalized_qty=%.6f",
        risk_net, entry, sl, abs(entry - sl), fee_per_unit, raw, qty,
    )

    return qty


def fee_adjusted_tp(
    entry: float,
    sl: float,
    side: str,
    entry_fee_rate: float,
    exit_fee_rate: float,
) -> float:
    """
    TP price that makes net P&L at TP == +risk_net, symmetric with the loss at SL.

        tp_dist = sl_dist + 2 * entry * (entry_fee_rate + exit_fee_rate)

    Use together with compute_qty_fee_adjusted() for exact symmetry.
    """
    sl_dist = abs(entry - sl)
    fee_dist = entry * (entry_fee_rate + exit_fee_rate)
    tp_dist = sl_dist + 2 * fee_dist
    return entry + tp_dist if side == "buy" else entry - tp_dist


def risk_summary(
    risk_cash: float,
    entry: float,
    sl: float,
    tp: float,
    side: str,
    qty: float,
    leverage: int = 1,
) -> dict:
    """Return a dict with risk breakdown for logging."""
    if side == "buy":
        risk_per_unit = entry - sl
        reward_per_unit = tp - entry
    else:
        risk_per_unit = sl - entry
        reward_per_unit = entry - tp

    margin_used = (qty * entry) / leverage
    actual_risk = risk_per_unit * qty
    reward_cash = reward_per_unit * qty
    rr = reward_per_unit / max(risk_per_unit, 1e-12)

    return {
        "risk_cash": round(risk_cash, 4),
        "actual_risk": round(actual_risk, 4),
        "reward_cash": round(reward_cash, 4),
        "rr": round(rr, 3),
        "qty": qty,
        "margin_used": round(margin_used, 4),
        "leverage": leverage,
        "risk_per_unit": round(risk_per_unit, 5),
        "reward_per_unit": round(reward_per_unit, 5),
    }
