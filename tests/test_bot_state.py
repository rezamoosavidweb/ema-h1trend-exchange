"""
Tests for state/bot_state.py — duplicate candle guard and pending order tracking.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from models.order import OrderStatus, PendingOrder, Side
from state.bot_state import BotState, make_order_link_id


def _make_pending(link_id: str = "ema-test-b-123456", side: str = "buy") -> PendingOrder:
    return PendingOrder(
        order_link_id=link_id,
        side=Side.BUY if side == "buy" else Side.SELL,
        entry=50000.0,
        sl=49000.0,
        tp=51000.0,
        qty=0.1,
        created_at=datetime.now(timezone.utc),
        signal_bar_time=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
        symbol="BTCUSDT",
    )


class TestBotState:
    def test_duplicate_candle_guard(self):
        state = BotState("BTCUSDT")
        ts = pd.Timestamp("2024-01-01 12:00", tz="UTC")
        state.mark_candle_processed(ts)
        assert state.is_duplicate_candle(ts) is True

    def test_new_candle_not_duplicate(self):
        state = BotState("BTCUSDT")
        ts1 = pd.Timestamp("2024-01-01 12:00", tz="UTC")
        ts2 = pd.Timestamp("2024-01-01 12:05", tz="UTC")
        state.mark_candle_processed(ts1)
        assert state.is_duplicate_candle(ts2) is False

    def test_none_candle_not_duplicate(self):
        state = BotState("BTCUSDT")
        assert state.is_duplicate_candle(None) is False

    def test_set_and_clear_pending(self):
        state = BotState("BTCUSDT")
        pending = _make_pending()
        state.set_pending(pending)
        assert state.has_pending() is True
        state.clear_pending()
        assert state.has_pending() is False

    def test_pending_matches_signal_exact(self):
        state = BotState("BTCUSDT")
        pending = _make_pending()
        state.set_pending(pending)
        assert state.pending_matches_signal("buy", 50000.0, 49000.0, 51000.0, pip_size=1.0) is True

    def test_pending_matches_within_tolerance(self):
        state = BotState("BTCUSDT")
        pending = _make_pending()
        state.set_pending(pending)
        # Difference < 1 pip
        assert state.pending_matches_signal("buy", 50000.3, 49000.3, 51000.3, pip_size=1.0) is True

    def test_pending_mismatch_side(self):
        state = BotState("BTCUSDT")
        pending = _make_pending(side="buy")
        state.set_pending(pending)
        assert state.pending_matches_signal("sell", 50000.0, 51000.0, 49000.0, pip_size=1.0) is False

    def test_pending_expiry(self):
        from datetime import timedelta
        state = BotState("BTCUSDT")
        # Signal bar was 70 minutes ago
        old_bar = datetime.now(timezone.utc) - timedelta(minutes=70)
        pending = PendingOrder(
            order_link_id="test",
            side=Side.BUY,
            entry=50000.0,
            sl=49000.0,
            tp=51000.0,
            qty=0.1,
            created_at=datetime.now(timezone.utc),
            signal_bar_time=old_bar,
            symbol="BTCUSDT",
        )
        state.set_pending(pending)
        assert state.pending_is_expired(expiry_min=60) is True
        assert state.pending_is_expired(expiry_min=120) is False


class TestMakeOrderLinkId:
    def test_length_within_36(self):
        ts = pd.Timestamp("2024-01-01 12:00", tz="UTC")
        link = make_order_link_id("BTCUSDT", 20260510, "buy", ts)
        assert len(link) <= 36

    def test_deterministic(self):
        ts = pd.Timestamp("2024-01-01 12:00", tz="UTC")
        a = make_order_link_id("BTCUSDT", 20260510, "buy", ts)
        b = make_order_link_id("BTCUSDT", 20260510, "buy", ts)
        assert a == b

    def test_different_side(self):
        ts = pd.Timestamp("2024-01-01 12:00", tz="UTC")
        buy = make_order_link_id("BTCUSDT", 20260510, "buy", ts)
        sell = make_order_link_id("BTCUSDT", 20260510, "sell", ts)
        assert buy != sell
