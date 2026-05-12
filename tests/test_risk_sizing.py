"""
Tests for risk/sizing.py — position sizing math.
Verifies the formula is identical to MT5 compute_pending_setup().
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
    def test_buy_formula(self):
        # qty = (balance * risk) / (entry - sl)
        # = (10000 * 0.01) / (50100 - 49000) = 100 / 1100 ≈ 0.0909
        raw = compute_raw_qty(10000, 0.01, 50100, 49000, "buy")
        assert abs(raw - 100.0 / 1100.0) < 1e-10

    def test_sell_formula(self):
        raw = compute_raw_qty(10000, 0.01, 49900, 51000, "sell")
        assert abs(raw - 100.0 / 1100.0) < 1e-10

    def test_matches_compute_pending_setup(self):
        """Verify compute_raw_qty matches the MT5 strategy formula exactly."""
        balance, risk, entry, sl = 10000.0, 0.01, 50100.0, 49000.0
        raw = compute_raw_qty(balance, risk, entry, sl, "buy")
        expected = (balance * risk) / (entry - sl)
        assert abs(raw - expected) < 1e-12


class TestComputeQty:
    def test_normalized_to_step(self):
        info = _info(step=0.001)
        qty = compute_qty(10000, 0.01, 50100, 49000, "buy", info)
        # Raw ≈ 0.09090..., floored to 0.090
        assert qty == 0.090

    def test_raises_if_below_min(self):
        # Very tight SL distance → large risk_per_unit → tiny qty
        info = _info(step=0.001, min_qty=0.001)
        with pytest.raises(InvalidQtyError):
            # 0.001% risk, 1-pip SL → qty will be too small
            compute_qty(1.0, 0.00001, 50000, 49999, "buy", info)


class TestRiskSummary:
    def test_rr_ratio(self):
        summary = risk_summary(
            balance=10000,
            risk_per_trade=0.01,
            entry=50000,
            sl=49000,
            tp=51000,
            side="buy",
            qty=0.1,
        )
        assert summary["rr"] == pytest.approx(1.0, rel=1e-3)

    def test_risk_cash(self):
        summary = risk_summary(
            balance=10000,
            risk_per_trade=0.01,
            entry=50000,
            sl=49000,
            tp=51000,
            side="buy",
            qty=0.1,
        )
        # risk_cash = risk_per_unit * qty = 1000 * 0.1 = 100
        assert summary["risk_cash"] == pytest.approx(100.0, rel=1e-3)
