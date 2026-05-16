"""
Market data fetcher — pulls OHLCV from Bybit and converts to the same
DataFrame format expected by the strategy layer.

The output schema is deliberately identical to what the MT5 source produced:
  - DatetimeIndex (UTC)
  - columns: open, high, low, close, volume (lowercase, float)
  - Sorted ascending
  - Forming (live) candle is ALWAYS dropped before returning
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from core.constants import BARS_ENTRY, BARS_TREND, MIN_WARMUP_BARS_M5, MIN_WARMUP_BARS_H1
from core.exceptions import InsufficientDataError, StaleDataError
from exchange.bybit_client import BybitClient

log = logging.getLogger(__name__)

# Maximum bars Bybit returns per kline call
_BYBIT_MAX_KLINE = 1000

# Maximum expected gap between candles before StaleData is raised
_STALE_CANDLE_SECONDS = {
    "5": 5 * 60 * 3,     # 3 M5 candles = 15 min
    "60": 60 * 60 * 3,   # 3 H1 candles = 3 hours
    "1": 1 * 60 * 5,
    "15": 15 * 60 * 3,
    "240": 240 * 60 * 3,
    "D": 24 * 60 * 60 * 3,
}


def _klines_to_df(raw: list[list]) -> pd.DataFrame:
    """
    Convert Bybit kline list → OHLCV DataFrame.

    Bybit returns newest-first: [[ts_ms, o, h, l, c, vol, turnover], ...]
    We reverse to chronological, set UTC DatetimeIndex, cast to float.
    """
    if not raw:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    rows = list(reversed(raw))  # Bybit newest-first → oldest-first
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "turnover"])

    df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("time").sort_index()

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    return df[["open", "high", "low", "close", "volume"]]


def _drop_forming_candle(df: pd.DataFrame, interval_str: str) -> pd.DataFrame:
    """
    Remove the last (still-forming) candle from a sorted OHLCV frame.

    Strategy requires only CLOSED bars to avoid lookahead bias.
    """
    if len(df) <= 1:
        return df.iloc[:0].copy()
    return df.iloc[:-1].copy()


def _validate_min_bars(
    df: pd.DataFrame,
    min_required: int,
    label: str,
) -> None:
    if len(df) < min_required:
        raise InsufficientDataError(
            f"Only {len(df)} closed {label} bars; need >= {min_required} "
            f"for EMA warmup. Try increasing bars_entry / bars_trend."
        )


def _check_staleness(df: pd.DataFrame, interval_str: str) -> None:
    """Warn (don't raise) if the most recent bar is older than expected."""
    if df.empty:
        return
    max_gap = _STALE_CANDLE_SECONDS.get(interval_str)
    if max_gap is None:
        return
    last_ts = df.index[-1]
    now = datetime.now(tz=timezone.utc)
    age = (now - last_ts.to_pydatetime()).total_seconds()
    if age > max_gap:
        log.warning(
            "Stale data detected: last %s bar is %ds old (max expected %ds). "
            "Possible feed gap or exchange maintenance.",
            interval_str, int(age), max_gap,
        )


class DataFetcher:
    """
    Fetches and prepares OHLCV data for both the entry (M5) and trend (H1) timeframes.

    Usage:
        fetcher = DataFetcher(client, symbol="BTCUSDT")
        m5, h1 = await fetcher.fetch_closed_frames()
    """

    def __init__(
        self,
        client: BybitClient,
        symbol: str,
        tf_entry: str = "5",
        tf_trend: str = "60",
        bars_entry: int = BARS_ENTRY,
        bars_trend: int = BARS_TREND,
    ) -> None:
        self._client = client
        self.symbol = symbol
        self.tf_entry = tf_entry
        self.tf_trend = tf_trend
        self.bars_entry = min(bars_entry, _BYBIT_MAX_KLINE)
        self.bars_trend = min(bars_trend, _BYBIT_MAX_KLINE)

    async def _fetch_ohlcv(self, interval: str, bars: int) -> pd.DataFrame:
        raw = await self._client.get_kline(self.symbol, interval, bars)
        df = _klines_to_df(raw)
        log.debug("Fetched %d raw %s bars for %s", len(df), interval, self.symbol)
        return df

    async def fetch_closed_frames(
        self,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """
        Fetch M5 + H1 OHLCV, drop forming candle from each, validate min bars.

        Returns:
            (m5_closed, h1_closed) — both sorted ascending with UTC DatetimeIndex.
        """
        m5_raw, h1_raw = await _gather(
            self._fetch_ohlcv(self.tf_entry, self.bars_entry),
            self._fetch_ohlcv(self.tf_trend, self.bars_trend),
        )

        m5 = _drop_forming_candle(m5_raw, self.tf_entry)
        h1 = _drop_forming_candle(h1_raw, self.tf_trend)

        _validate_min_bars(m5, MIN_WARMUP_BARS_M5 + 1, f"M{self.tf_entry}")
        _validate_min_bars(h1, MIN_WARMUP_BARS_H1 + 1, f"H{self.tf_trend}")

        _check_staleness(m5, self.tf_entry)

        log.info(
            "Data ready | %s M%s=%d bars [%s → %s]  H%s=%d bars",
            self.symbol,
            self.tf_entry,
            len(m5),
            m5.index[0].strftime("%Y-%m-%d %H:%M"),
            m5.index[-1].strftime("%Y-%m-%d %H:%M"),
            self.tf_trend,
            len(h1),
        )
        return m5, h1

    async def fetch_m5_frame(self) -> pd.DataFrame:
        """
        Fetch only the entry-TF (M5) frame, drop forming candle, validate min bars.
        Used by strategies that need no H1 trend (e.g. Order Block Reaction).
        """
        m5_raw = await self._fetch_ohlcv(self.tf_entry, self.bars_entry)
        m5     = _drop_forming_candle(m5_raw, self.tf_entry)
        _validate_min_bars(m5, MIN_WARMUP_BARS_M5 + 1, f"M{self.tf_entry}")
        _check_staleness(m5, self.tf_entry)
        log.info(
            "Data ready | %s M%s=%d bars [%s → %s]",
            self.symbol,
            self.tf_entry,
            len(m5),
            m5.index[0].strftime("%Y-%m-%d %H:%M"),
            m5.index[-1].strftime("%Y-%m-%d %H:%M"),
        )
        return m5

    async def peek_latest_candle_time(self) -> Optional[pd.Timestamp]:
        """
        Cheap peek at the timestamp of the last closed candle for the duplicate guard.
        Fetches only 2 bars, returns the second-to-last (last closed).
        """
        try:
            raw = await self._client.get_kline(self.symbol, self.tf_entry, 3)
            df = _klines_to_df(raw)
            if len(df) >= 2:
                # Second from end = last closed (last = forming)
                return df.index[-2]
        except Exception as exc:
            log.warning("peek_latest_candle_time failed: %s", exc)
        return None


# ── asyncio.gather helper ────────────────────────────────────────────────────

import asyncio


async def _gather(*coros):
    return await asyncio.gather(*coros)
