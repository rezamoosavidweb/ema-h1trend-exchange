"""
Tests for risk/sizing.py — margin-based position sizing.
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
    def test_margin_formula_1x(self):
        # qty = (balance * margin_pct * leverage) / entry
        # = (10000 * 0.01 * 1) / 50100 = 100 / 50100 ≈ 0.001996
        raw = compute_raw_qty(10000, 0.01, 50100, 1)
        assert abs(raw - 100.0 / 50100.0) < 1e-10

    def test_leverage_scales_qty_linearly(self):
        raw_1x = compute_raw_qty(10000, 0.01, 50100, 1)
        raw_10x = compute_raw_qty(10000, 0.01, 50100, 10)
        assert abs(raw_10x - raw_1x * 10) < 1e-10

    def test_margin_committed_constant_across_leverage(self):
        """margin_used = qty * entry / leverage must always equal balance * margin_pct."""
        balance, margin_pct, entry = 10000.0, 0.01, 50100.0
        for lev in [1, 5, 10, 20]:
            raw = compute_raw_qty(balance, margin_pct, entry, lev)
            margin_used = raw * entry / lev
            assert abs(margin_used - balance * margin_pct) < 1e-6

    def test_higher_price_gives_smaller_qty(self):
        raw_low = compute_raw_qty(10000, 0.01, 1000, 1)
        raw_high = compute_raw_qty(10000, 0.01, 100000, 1)
        assert raw_low > raw_high


class TestComputeQty:
    def test_normalized_to_step(self):
        info = _info(step=0.001)
        # qty = (10000 * 0.01 * 1) / 50100 = 0.001996... → floored to 0.001
        qty = compute_qty(10000, 0.01, 50100, 1, info)
        assert qty == 0.001

    def test_leverage_increases_qty(self):
        info = _info(step=0.001, min_qty=0.001, max_qty=10000.0)
        qty_1x = compute_qty(10000, 0.01, 50100, 1, info)
        qty_10x = compute_qty(10000, 0.01, 50100, 10, info)
        assert qty_10x > qty_1x

    def test_raises_if_below_min(self):
        info = _info(step=0.001, min_qty=1.0)
        with pytest.raises(InvalidQtyError):
            # tiny balance → margin too small → qty below minQty
            compute_qty(1.0, 0.0001, 50000, 1, info)


class TestRiskSummary:
    def test_rr_ratio(self):
        summary = risk_summary(
            balance=10000,
            margin_pct=0.01,
            entry=50000,
            sl=49000,
            tp=51000,
            side="buy",
            qty=0.1,
            leverage=1,
        )
        assert summary["rr"] == pytest.approx(1.0, rel=1e-3)

    def test_margin_used(self):
        summary = risk_summary(
            balance=10000,
            margin_pct=0.01,
            entry=50000,
            sl=49000,
            tp=51000,
            side="buy",
            qty=0.002,
            leverage=1,
        )
        # margin_used = qty * entry / leverage = 0.002 * 50000 / 1 = 100
        assert summary["margin_used"] == pytest.approx(100.0, rel=1e-3)

    def test_margin_pct_in_summary(self):
        summary = risk_summary(
            balance=10000,
            margin_pct=0.01,
            entry=50000,
            sl=49000,
            tp=51000,
            side="buy",
            qty=0.002,
            leverage=1,
        )
        assert summary["margin_pct"] == pytest.approx(1.0, rel=1e-3)
