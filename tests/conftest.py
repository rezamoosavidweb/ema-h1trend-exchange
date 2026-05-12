"""
Shared pytest fixtures for the Bybit trading system tests.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from models.order import InstrumentInfo


# ── Common instrument fixtures ────────────────────────────────────────────────

@pytest.fixture
def btc_info() -> InstrumentInfo:
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


@pytest.fixture
def eth_info() -> InstrumentInfo:
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


# ── OHLCV data factories ──────────────────────────────────────────────────────

@pytest.fixture
def make_ohlcv():
    def _make(n: int = 300, base: float = 50000.0, seed: int = 42, freq: str = "5min") -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        prices = base + np.cumsum(rng.normal(0, 100, n))
        spread = rng.uniform(50, 300, n)
        highs = prices + spread
        lows = prices - spread
        opens = prices + rng.normal(0, 30, n)
        vols = rng.uniform(1, 10, n)
        idx = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
        return pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": prices, "volume": vols},
            index=idx,
        )
    return _make


@pytest.fixture
def bull_m5_context(make_ohlcv):
    """250-bar M5 context with a forced bull trend."""
    from strategy.crypto_core import add_emas, merge_h1_trend_onto_m5
    n = 250
    idx_m5 = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    prices = 50000 + np.arange(n) * 10.0
    m5 = pd.DataFrame(
        {"open": prices, "high": prices + 30, "low": prices - 30,
         "close": prices, "volume": 1.0},
        index=idx_m5,
    )
    n_h1 = n // 12 + 10
    idx_h1 = pd.date_range("2024-01-01", periods=n_h1, freq="h", tz="UTC")
    h1_prices = 50000 + np.arange(n_h1) * 120.0
    h1 = pd.DataFrame(
        {"open": h1_prices, "high": h1_prices + 60, "low": h1_prices - 60,
         "close": h1_prices, "volume": 1.0},
        index=idx_h1,
    )
    m5 = add_emas(m5)
    h1 = add_emas(h1)
    return merge_h1_trend_onto_m5(m5, h1)
