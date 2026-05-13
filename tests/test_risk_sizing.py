"""
Tests for risk/sizing.py — fixed-USDT risk-based position sizing.
"""

from __future__ import annotations

import pytest
from models.order import InstrumentInfo
from risk.sizing import compute_raw_qty, compute_qty, risk_summary
from core.exceptions import InvalidQtyError


def _info(step: float = 0.001, min_qty: float = 0.001, max_qty: float = 100.0) -> InstrumentInfo:
    def _scale(v):
        s = f"{v:.10f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0
    return InstrumentInfo(
        symbol="BTCUSDT",
        tick_size=0.5,
        qty_step=step,
        min_qty=min_qty,
        max_qty=max_qty,
        min_notional=0.0,
        price_scale=1,
        qty_scale=_scale(step),
    )


class TestComputeRawQty:
    def test_basic_formula(self):
        # qty = risk_cash / sl_distance = 20 / 500 = 0.04
        raw = compute_raw_qty(20.0, 50500, 50000)
        assert abs(raw - 0.04) < 1e-10

    def test_risk_at_sl_equals_risk_cash(self):
        # qty × sl_distance must equal risk_cash exactly
        risk_cash = 20.0
        entry, sl = 50500.0, 50000.0
        raw = compute_raw_qty(risk_cash, entry, sl)
        assert abs(raw * abs(entry - sl) - risk_cash) < 1e-9

    def test_wider_sl_gives_smaller_qty(self):
        # Wider SL → smaller qty to keep risk fixed at $20
        raw_tight = compute_raw_qty(20.0, 50500, 50000)   # 500 dist
        raw_wide  = compute_raw_qty(20.0, 51000, 50000)   # 1000 dist
        assert raw_tight > raw_wide

    def test_higher_risk_cash_gives_larger_qty(self):
        raw_10 = compute_raw_qty(10.0, 50500, 50000)
        raw_20 = compute_raw_qty(20.0, 50500, 50000)
        assert abs(raw_20 - raw_10 * 2) < 1e-10

    def test_zero_sl_distance_raises(self):
        with pytest.raises(InvalidQtyError):
            compute_raw_qty(20.0, 50000, 50000)

    def test_sell_side_same_as_buy_same_distance(self):
        # sell: entry < sl, but abs distance is same → same qty
        raw_buy  = compute_raw_qty(20.0, 50500, 50000)   # entry > sl
        raw_sell = compute_raw_qty(20.0, 50000, 50500)   # entry < sl
        assert abs(raw_buy - raw_sell) < 1e-10


class TestComputeQty:
    def test_normalized_to_step(self):
        info = _info(step=0.001)
        # raw = 20 / 500 = 0.04 → already on 0.001 step
        qty = compute_qty(20.0, 50500, 50000, 1, info)
        assert qty == pytest.approx(0.04, rel=1e-6)

    def test_floored_to_step(self):
        info = _info(step=0.01)
        # raw = 20 / 333 ≈ 0.0600... → floored to 0.06
        qty = compute_qty(20.0, 50333, 50000, 1, info)
        assert qty == pytest.approx(0.06, rel=1e-4)

    def test_raises_if_below_min(self):
        info = _info(step=0.001, min_qty=1.0)
        with pytest.raises(InvalidQtyError):
            # risk_cash=$0.001, sl_dist=500 → qty=0.000002, below min=1.0
            compute_qty(0.001, 50500, 50000, 1, info)

    def test_leverage_does_not_change_qty(self):
        info = _info(step=0.001)
        qty_1x  = compute_qty(20.0, 50500, 50000, 1,  info)
        qty_10x = compute_qty(20.0, 50500, 50000, 10, info)
        assert qty_1x == qty_10x


class TestRiskSummary:
    def test_rr_ratio(self):
        summary = risk_summary(
            risk_cash=20.0,
            entry=50000,
            sl=49000,
            tp=51000,
            side="buy",
            qty=0.02,
            leverage=1,
        )
        assert summary["rr"] == pytest.approx(1.0, rel=1e-3)

    def test_actual_risk_equals_risk_cash(self):
        # qty=0.02, sl_dist=1000 → actual_risk = 0.02 * 1000 = 20
        summary = risk_summary(
            risk_cash=20.0,
            entry=50000,
            sl=49000,
            tp=51000,
            side="buy",
            qty=0.02,
            leverage=1,
        )
        assert summary["actual_risk"] == pytest.approx(20.0, rel=1e-3)

    def test_margin_used(self):
        # margin_used = qty * entry / leverage = 0.02 * 50000 / 1 = 1000
        summary = risk_summary(
            risk_cash=20.0,
            entry=50000,
            sl=49000,
            tp=51000,
            side="buy",
            qty=0.02,
            leverage=1,
        )
        assert summary["margin_used"] == pytest.approx(1000.0, rel=1e-3)

    def test_sell_side_rr(self):
        summary = risk_summary(
            risk_cash=20.0,
            entry=50000,
            sl=51000,
            tp=49000,
            side="sell",
            qty=0.02,
            leverage=1,
        )
        assert summary["rr"] == pytest.approx(1.0, rel=1e-3)
        assert summary["actual_risk"] == pytest.approx(20.0, rel=1e-3)
