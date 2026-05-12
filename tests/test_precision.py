"""
Tests for exchange/precision.py — price + qty normalization.

These tests have no external dependencies and verify the tick/step rounding
math that is critical for preventing exchange 'invalid price' rejections.
"""

from __future__ import annotations

import pytest
from models.order import InstrumentInfo
from exchange.precision import (
    normalize_price,
    normalize_qty,
    price_to_str,
    qty_to_str,
    snap_signal_to_ticks,
    validate_order_geometry,
)
from core.exceptions import InvalidPriceError


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _btc_info() -> InstrumentInfo:
    return InstrumentInfo(
        symbol="BTCUSDT",
        tick_size=0.5,
        qty_step=0.001,
        min_qty=0.001,
        max_qty=100.0,
        min_notional=0.0,
        price_scale=1,
        qty_scale=3,
    )


def _eth_info() -> InstrumentInfo:
    return InstrumentInfo(
        symbol="ETHUSDT",
        tick_size=0.01,
        qty_step=0.01,
        min_qty=0.01,
        max_qty=10000.0,
        min_notional=0.0,
        price_scale=2,
        qty_scale=2,
    )


# ── Price normalization ───────────────────────────────────────────────────────

class TestNormalizePrice:
    def test_nearest_on_grid(self):
        assert normalize_price(50000.0, 0.5) == 50000.0

    def test_nearest_rounds_half_up(self):
        result = normalize_price(50000.25, 0.5)
        assert result == 50000.5

    def test_nearest_rounds_down(self):
        result = normalize_price(50000.1, 0.5)
        assert result == 50000.0

    def test_up_mode(self):
        result = normalize_price(50000.1, 0.5, mode="up")
        assert result == 50000.5

    def test_down_mode(self):
        result = normalize_price(50000.9, 0.5, mode="down")
        assert result == 50000.5

    def test_eth_tick_size(self):
        result = normalize_price(1800.123, 0.01)
        assert result == 1800.12

    def test_price_to_str_btc(self):
        assert price_to_str(50000.5, 0.5) == "50000.5"

    def test_price_to_str_eth(self):
        assert price_to_str(1800.12, 0.01) == "1800.12"


# ── Qty normalization ─────────────────────────────────────────────────────────

class TestNormalizeQty:
    def test_on_grid(self):
        info = _btc_info()
        assert normalize_qty(0.001, info) == 0.001

    def test_floor_to_step(self):
        info = _btc_info()
        # 0.0019 should floor to 0.001 (not round to 0.002)
        result = normalize_qty(0.0019, info)
        assert result == 0.001

    def test_below_min_clamped_to_min(self):
        info = _btc_info()
        result = normalize_qty(0.0001, info)
        assert result == 0.001  # clamped to min

    def test_above_max_clamped_to_max(self):
        info = _btc_info()
        result = normalize_qty(999.0, info)
        assert result == 100.0

    def test_qty_to_str(self):
        info = _btc_info()
        assert qty_to_str(0.001, info) == "0.001"
        assert qty_to_str(0.123, info) == "0.123"

    def test_eth_qty(self):
        info = _eth_info()
        result = normalize_qty(1.237, info)
        assert result == 1.23  # floor, not round


# ── Signal snap ───────────────────────────────────────────────────────────────

class TestSnapSignalToTicks:
    def test_buy_snap(self):
        info = _btc_info()
        entry, sl, tp = snap_signal_to_ticks("buy", 50001.1, 49998.7, 50003.3, info)
        # entry snapped UP, sl DOWN, tp UP
        assert entry == 50001.5
        assert sl == 49998.5
        assert tp == 50003.5

    def test_sell_snap(self):
        info = _btc_info()
        entry, sl, tp = snap_signal_to_ticks("sell", 49998.7, 50001.1, 49996.3, info)
        # entry snapped DOWN, sl UP, tp DOWN
        assert entry == 49998.5
        assert sl == 50001.5
        assert tp == 49996.0


# ── Geometry validation ───────────────────────────────────────────────────────

class TestValidateGeometry:
    def test_buy_valid(self):
        info = _btc_info()
        # Should not raise
        validate_order_geometry("buy", 50000.0, 49000.0, 51000.0, info)

    def test_buy_invalid_sl_above_entry(self):
        info = _btc_info()
        with pytest.raises(InvalidPriceError):
            validate_order_geometry("buy", 50000.0, 51000.0, 52000.0, info)

    def test_sell_valid(self):
        info = _btc_info()
        validate_order_geometry("sell", 50000.0, 51000.0, 49000.0, info)

    def test_sell_invalid_tp_above_entry(self):
        info = _btc_info()
        with pytest.raises(InvalidPriceError):
            validate_order_geometry("sell", 50000.0, 51000.0, 51000.0, info)
