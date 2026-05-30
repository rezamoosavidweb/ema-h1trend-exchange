"""
Reusable crypto strategy library for Bybit-linear backtests.

Loads OHLCV CSVs written by 00_data_fetching_bybit.ipynb and provides
multiple strategy variants with a uniform backtester so we can sweep
across strategies / symbols / timeframes and rank by win-rate and
trades-per-month.

All time logic is "broker-local" — the CSVs are saved in Asia/Tehran
local time but we strip the tz and treat the index as naive. NY session
filters use a fixed offset (default: subtract 7h to get NY hour). For
24x7 crypto this offset is mostly cosmetic; we still expose it so the
same code can be re-used for FX symbols.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Callable

import numpy as np
import pandas as pd

try:
    from numba import njit
    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False
    def njit(*args, **kwargs):  # noqa: D401
        # Support both `@njit` and `@njit(cache=True, ...)` usage when numba is missing.
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        def _decorator(f):
            return f
        return _decorator


@njit(cache=True)
def _backtest_loop(o, h, l, c, sig, sl_pre, tp_pre, has_tp,
                   rr, max_hold, one_at_a_time, fee_frac):
    n = len(o)
    # output arrays — preallocate max possible
    out_side    = np.zeros(n, dtype=np.int8)
    out_e_idx   = np.zeros(n, dtype=np.int32)
    out_x_idx   = np.zeros(n, dtype=np.int32)
    out_entry   = np.zeros(n, dtype=np.float64)
    out_sl      = np.zeros(n, dtype=np.float64)
    out_tp      = np.zeros(n, dtype=np.float64)
    out_exit    = np.zeros(n, dtype=np.float64)
    out_reason  = np.zeros(n, dtype=np.int8)  # 0=time,1=tp,2=sl
    out_gross_r = np.zeros(n, dtype=np.float64)
    out_net_r   = np.zeros(n, dtype=np.float64)
    k = 0

    in_trade = False
    cur_side = 0
    cur_entry = 0.0
    cur_sl = 0.0
    cur_tp = 0.0
    cur_eidx = 0

    for i in range(n - 1):
        if in_trade:
            hi = h[i]; lo = l[i]
            hit_sl = (cur_side == 1 and lo <= cur_sl) or (cur_side == -1 and hi >= cur_sl)
            hit_tp = (cur_side == 1 and hi >= cur_tp) or (cur_side == -1 and lo <= cur_tp)
            exit_now = False; reason = 0; px = 0.0
            if hit_sl:
                exit_now = True; reason = 2; px = cur_sl
            elif hit_tp:
                exit_now = True; reason = 1; px = cur_tp
            elif i - cur_eidx >= max_hold:
                exit_now = True; reason = 0; px = c[i]
            if exit_now:
                out_side[k]   = cur_side
                out_e_idx[k]  = cur_eidx
                out_x_idx[k]  = i
                out_entry[k]  = cur_entry
                out_sl[k]     = cur_sl
                out_tp[k]     = cur_tp
                out_exit[k]   = px
                out_reason[k] = reason
                r_unit = abs(cur_entry - cur_sl)
                gross_r = ((px - cur_entry) * cur_side) / r_unit if r_unit > 0 else 0.0
                fee_r   = (fee_frac * cur_entry) / r_unit if r_unit > 0 else 0.0
                out_gross_r[k] = gross_r
                out_net_r[k]   = gross_r - fee_r
                k += 1
                in_trade = False
        if (not in_trade or not one_at_a_time) and sig[i] != 0:
            ei = i + 1
            if ei >= n:
                break
            ep = o[ei]
            sl = sl_pre[i]
            if np.isnan(sl):
                continue
            r = abs(ep - sl)
            if r <= 0.0:
                continue
            if has_tp and not np.isnan(tp_pre[i]):
                tp = tp_pre[i]
                if (sig[i] == 1 and tp <= ep) or (sig[i] == -1 and tp >= ep):
                    continue
            else:
                tp = ep + rr * r * sig[i]
            cur_side  = sig[i]
            cur_entry = ep
            cur_sl    = sl
            cur_tp    = tp
            cur_eidx  = ei
            in_trade = True
    return (out_side[:k], out_e_idx[:k], out_x_idx[:k], out_entry[:k],
            out_sl[:k], out_tp[:k], out_exit[:k], out_reason[:k],
            out_gross_r[:k], out_net_r[:k])


# ───────────────────────────── constants ──────────────────────────────
DATA_DIR = Path(__file__).resolve().parent / "data"

# Bybit linear USDT-perp typical fee (taker). Charged per side as fraction
# of notional. 0.055 % per side ≈ 0.11 % round-trip.
TAKER_FEE_BPS = 5.5   # one-way
ROUND_TRIP_BPS = 2 * TAKER_FEE_BPS

# Broker -> NY hour offset. For Tehran-saved CSVs, NY = local - 7.5h
# but we just use the integer that matches notebook 24's logic.
BROKER_TO_NY_H = 7

# Bars per month per timeframe (approx — used to derive trades/month).
BARS_PER_MONTH = {
    "M1":  60 * 24 * 30,
    "M5":  12 * 24 * 30,
    "M15": 4  * 24 * 30,
    "M30": 2  * 24 * 30,
    "H1":  1  * 24 * 30,
    "H4":  6  * 30,
    "D1":  30,
}


# ───────────────────────────── data loading ────────────────────────────
def list_symbols(data_dir: Path = DATA_DIR) -> List[str]:
    if not data_dir.exists():
        return []
    return sorted([p.name for p in data_dir.iterdir()
                   if p.is_dir() and not p.name.startswith("_")
                   and p.name not in {"test", "trades"}])


_OHLCV_CACHE: Dict[Tuple[str, str, Optional[str], Optional[str]], pd.DataFrame] = {}


def load_ohlcv(symbol: str, tf: str,
               date_from: Optional[str] = None,
               date_to:   Optional[str] = None,
               data_dir: Path = DATA_DIR) -> pd.DataFrame:
    key = (symbol, tf, date_from, date_to)
    cached = _OHLCV_CACHE.get(key)
    if cached is not None:
        return cached.copy()
    path = data_dir / symbol / tf / "ohlcv.csv"
    if not path.exists():
        _OHLCV_CACHE[key] = pd.DataFrame()
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(None)
    df = df.sort_values("time").reset_index(drop=True)
    keep = [c for c in ("time", "open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].copy()
    if date_from:
        df = df[df["time"] >= pd.Timestamp(date_from)]
    if date_to:
        df = df[df["time"] <= pd.Timestamp(date_to)]
    df = df.reset_index(drop=True)
    _OHLCV_CACHE[key] = df
    return df.copy()


# ───────────────────────────── indicators ──────────────────────────────
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0); loss = (-d).clip(lower=0)
    ag = gain.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    al = loss.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = ag / al.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up   = df["high"].diff()
    down = -df["low"].diff()
    plus_dm  = pd.Series(np.where((up > down) & (up > 0), up, 0.0),   index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift()).abs(),
                    (df["low"]  - df["close"].shift()).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=1/n, adjust=False, min_periods=n).mean()  / atr_w.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/n, adjust=False, min_periods=n).mean() / atr_w.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False, min_periods=n).mean().fillna(0)


def macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.Series:
    f = ema(close, fast); s = ema(close, slow)
    line = f - s
    return line - ema(line, sig)


def donchian(df: pd.DataFrame, n: int) -> Tuple[pd.Series, pd.Series]:
    return df["high"].rolling(n).max(), df["low"].rolling(n).min()


# ───────────────────────────── Ichimoku ────────────────────────────────
def ichimoku(df: pd.DataFrame,
             tenkan: int = 9, kijun: int = 26, senkou_b: int = 52,
             displacement: int = 26) -> pd.DataFrame:
    """Standard Ichimoku Kinko Hyo components."""
    out = pd.DataFrame(index=df.index)
    h_t = df["high"].rolling(tenkan).max();   l_t = df["low"].rolling(tenkan).min()
    h_k = df["high"].rolling(kijun).max();    l_k = df["low"].rolling(kijun).min()
    h_b = df["high"].rolling(senkou_b).max(); l_b = df["low"].rolling(senkou_b).min()
    out["tenkan"] = (h_t + l_t) / 2.0
    out["kijun"]  = (h_k + l_k) / 2.0
    # Senkou A/B displaced forward by `displacement` bars (these define the cloud)
    out["senkou_a"] = ((out["tenkan"] + out["kijun"]) / 2.0).shift(displacement)
    out["senkou_b"] = ((h_b + l_b) / 2.0).shift(displacement)
    # Chikou span = close shifted *back* — for our usage we read forward state at
    # bar i so just keep raw close; we do not use chikou directly.
    out["cloud_top"]    = np.maximum(out["senkou_a"], out["senkou_b"])
    out["cloud_bottom"] = np.minimum(out["senkou_a"], out["senkou_b"])
    return out


# ─────────────────────── Swing high/low detector ───────────────────────
def swing_points(df: pd.DataFrame, lookback: int = 5) -> Tuple[pd.Series, pd.Series]:
    """Fractal-style swing highs / lows.

    A bar i is a swing high if its high is the max of [i-lookback..i+lookback].
    Returns two boolean Series, aligned with df.index. Forward-looking — we
    only consume them at i + lookback to stay causally clean.
    """
    h = df["high"]; l = df["low"]
    win = 2 * lookback + 1
    is_high = (h == h.rolling(win, center=True).max())
    is_low  = (l == l.rolling(win, center=True).min())
    return is_high.fillna(False), is_low.fillna(False)


def latest_swing_levels(df: pd.DataFrame, lookback: int = 5, n_recent: int = 1) -> Tuple[pd.Series, pd.Series]:
    """Forward-fill the most recent swing-high / swing-low price levels (causal,
    shifted by `lookback` bars so we only confirm a swing once enough future bars
    have been observed)."""
    is_h, is_l = swing_points(df, lookback)
    sh = df["high"].where(is_h).shift(lookback)
    sl = df["low"].where(is_l).shift(lookback)
    return sh.ffill(), sl.ffill()


def vwap_daily(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP, reset each calendar day from df['time']."""
    if "volume" not in df.columns or df["volume"].sum() == 0:
        return pd.Series(np.nan, index=df.index)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df["time"].dt.date
    g = df.assign(_tpv=typical * df["volume"], _v=df["volume"]).groupby(day)
    cum_tpv = g["_tpv"].cumsum()
    cum_v   = g["_v"].cumsum().replace(0, np.nan)
    return (cum_tpv / cum_v).ffill()


# ───────────────────────────── HTF helpers ─────────────────────────────
def htf_trend_dir(htf: pd.DataFrame, ema_len: int = 50) -> pd.DataFrame:
    h = htf.copy()
    e = ema(h["close"], ema_len)
    slope = e.diff()
    h["htf_ema"]      = e
    h["htf_trend"]    = np.where((h["close"] > e) & (slope > 0), 1,
                          np.where((h["close"] < e) & (slope < 0), -1, 0))
    h["htf_rsi"]      = rsi(h["close"], 14).fillna(50)
    return h[["time", "htf_ema", "htf_trend", "htf_rsi"]]


def merge_htf(base: pd.DataFrame, htf: pd.DataFrame, prefix: str) -> pd.DataFrame:
    cols = htf.rename(columns={
        "htf_ema":   f"{prefix}_ema",
        "htf_trend": f"{prefix}_trend",
        "htf_rsi":   f"{prefix}_rsi",
    })
    return pd.merge_asof(base.sort_values("time"),
                         cols.sort_values("time"),
                         on="time", direction="backward")


# ───────────────────────────── trade objects ───────────────────────────
@dataclass
class Trade:
    side: int
    entry_idx: int
    entry_time: pd.Timestamp
    entry: float
    sl: float
    tp: float
    exit_idx: int = -1
    exit_time: Optional[pd.Timestamp] = None
    exit: float = 0.0
    reason: str = ""
    r_multiple: float = 0.0
    net_r: float = 0.0


# ───────────────────────────── backtest core ───────────────────────────
def backtest(df: pd.DataFrame, *,
             rr: float = 2.0,
             max_hold_bars: int = 96,
             one_at_a_time: bool = True,
             fee_bps_round_trip: float = ROUND_TRIP_BPS,
             tp_col: Optional[str] = None) -> List[Trade]:
    """
    Walk-forward bar-by-bar backtest. df must contain 'signal' column with
    {-1, 0, +1} entries, an 'atr' column, and an 'sl_price' column (the
    structural stop already computed at signal time). Entries fill at the
    *next* bar's open.

    Fees are deducted in R units: 0.011% * entry_price / |entry-sl|
    is added to the loser-side and subtracted from winner-side.

    If `tp_col` is given, that column's value at the signal bar is used as
    the take-profit *price* (instead of rr * stop distance). Used for mean
    reversion variants that aim at BB midline / EMA / fixed level.
    """
    fee_frac = fee_bps_round_trip / 10000.0
    o = df["open"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    l = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    sig = df["signal"].to_numpy().astype(np.int64)
    sl_pre = df["sl_price"].to_numpy(dtype=np.float64)
    has_tp = tp_col is not None and tp_col in df.columns
    tp_pre = df[tp_col].to_numpy(dtype=np.float64) if has_tp else np.zeros(len(df))
    times = df["time"].to_numpy()

    sides, eidxs, xidxs, entries, sls, tps, exits, reasons, gr, nr = _backtest_loop(
        o, h, l, c, sig, sl_pre, tp_pre, has_tp,
        float(rr), int(max_hold_bars), bool(one_at_a_time), float(fee_frac),
    )
    trades: List[Trade] = []
    reason_map = {0: "time", 1: "tp", 2: "sl"}
    for j in range(len(sides)):
        trades.append(Trade(
            side=int(sides[j]),
            entry_idx=int(eidxs[j]),
            entry_time=times[int(eidxs[j])],
            entry=float(entries[j]),
            sl=float(sls[j]),
            tp=float(tps[j]),
            exit_idx=int(xidxs[j]),
            exit_time=times[int(xidxs[j])],
            exit=float(exits[j]),
            reason=reason_map.get(int(reasons[j]), "?"),
            r_multiple=float(gr[j]),
            net_r=float(nr[j]),
        ))
    return trades


# ───────────────────────────── metrics ─────────────────────────────────
def trades_per_month(trades: List[Trade], df: pd.DataFrame) -> float:
    if not trades or df.empty:
        return 0.0
    span_days = (df["time"].iloc[-1] - df["time"].iloc[0]).total_seconds() / 86400
    months = max(span_days / 30.0, 1e-6)
    return len(trades) / months


def stats(trades: List[Trade], df: Optional[pd.DataFrame] = None,
          rr: float = 2.0) -> Dict[str, float]:
    if not trades:
        return dict(trades=0,
                    win_rate=0.0, win_rate_net=0.0,
                    avg_R=0.0, net_avg_R=0.0,
                    expectancy_R=0.0,
                    profit_factor=0.0, profit_factor_net=0.0,
                    trades_per_month=0.0, total_net_R=0.0,
                    max_drawdown_R=0.0)
    # Gross (pre-fee) wins/losses
    g_wins   = [t for t in trades if t.r_multiple > 0]
    g_losses = [t for t in trades if t.r_multiple <= 0]
    gw = sum(t.r_multiple for t in g_wins)
    gl = -sum(t.r_multiple for t in g_losses)
    wr_gross = len(g_wins) / len(trades)
    # Net (post-fee) — TP exits with tiny gross R can flip to net loss after the
    # 0.11 % round-trip taker fee, so net WR/PF can be materially lower than
    # gross. These are the numbers you trade on in live execution.
    n_wins   = [t for t in trades if t.net_r > 0]
    n_losses = [t for t in trades if t.net_r <= 0]
    nw = sum(t.net_r for t in n_wins)
    nl = -sum(t.net_r for t in n_losses)
    wr_net = len(n_wins) / len(trades)
    nets = np.array([t.net_r for t in trades])
    cum = nets.cumsum()
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak).min() if len(cum) else 0.0
    return dict(
        trades=len(trades),
        win_rate=100.0 * wr_gross,
        win_rate_net=100.0 * wr_net,
        avg_R=float(np.mean([t.r_multiple for t in trades])),
        net_avg_R=float(nets.mean()),
        expectancy_R=wr_gross * rr - (1 - wr_gross),
        profit_factor=(gw / gl) if gl > 0 else float("inf"),
        profit_factor_net=(nw / nl) if nl > 0 else float("inf"),
        trades_per_month=trades_per_month(trades, df) if df is not None else 0.0,
        total_net_R=float(nets.sum()),
        max_drawdown_R=float(dd),
    )


# ───────────────────────────── filters / signals ───────────────────────
def f_pin_engulf(df: pd.DataFrame, wick_ratio: float = 0.55) -> pd.Series:
    rng = (df["high"] - df["low"]).clip(lower=1e-9)
    upper = df["high"] - df[["open", "close"]].max(axis=1)
    lower = df[["open", "close"]].min(axis=1) - df["low"]
    bull_pin = (lower / rng >= wick_ratio) & (df["close"] > df["open"])
    bear_pin = (upper / rng >= wick_ratio) & (df["close"] < df["open"])
    po, pc = df["open"].shift(1), df["close"].shift(1)
    bull_eng = (pc < po) & (df["close"] > df["open"]) & (df["close"] >= po) & (df["open"] <= pc)
    bear_eng = (pc > po) & (df["close"] < df["open"]) & (df["close"] <= po) & (df["open"] >= pc)
    long  = bull_pin | bull_eng
    short = bear_pin | bear_eng
    return pd.Series(np.where(long, 1, np.where(short, -1, 0)), index=df.index)


def f_rsi_cross(df: pd.DataFrame, os_: float, ob: float) -> pd.Series:
    p = df["rsi"].shift(1)
    long  = (p <= os_) & (df["rsi"] >  os_)
    short = (p >= ob)  & (df["rsi"] <  ob)
    return pd.Series(np.where(long, 1, np.where(short, -1, 0)), index=df.index)


def f_rsi_recent(f_rsi_col: pd.Series, memory: int) -> pd.Series:
    long_fresh  = (f_rsi_col ==  1).rolling(memory).max().fillna(0).astype(bool)
    short_fresh = (f_rsi_col == -1).rolling(memory).max().fillna(0).astype(bool)
    return pd.Series(np.where(long_fresh, 1, np.where(short_fresh, -1, 0)),
                     index=f_rsi_col.index)


def f_ema_pullback(df: pd.DataFrame, ema_col: str, tol_atr: float) -> pd.Series:
    tol = tol_atr * df["atr"]
    tl = (df["low"]  <= df[ema_col] + tol) & (df["close"] > df[ema_col])
    ts = (df["high"] >= df[ema_col] - tol) & (df["close"] < df[ema_col])
    return pd.Series(np.where(tl, 1, np.where(ts, -1, 0)), index=df.index)


def f_bb_touch(df: pd.DataFrame) -> pd.Series:
    long  = df["low"]  <= df["bb_lo"]
    short = df["high"] >= df["bb_up"]
    return pd.Series(np.where(long, 1, np.where(short, -1, 0)), index=df.index)


# ─────────────────────────── feature builders ──────────────────────────
def add_base_features(df: pd.DataFrame,
                      ema_fast: int = 20,
                      bb_period: int = 20, bb_std: float = 2.0,
                      rsi_n: int = 14, atr_n: int = 14) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = ema(df["close"], ema_fast)
    df["rsi"]      = rsi(df["close"], rsi_n)
    df["atr"]      = atr(df, atr_n)
    mid = df["close"].rolling(bb_period).mean()
    std = df["close"].rolling(bb_period).std()
    df["bb_mid"] = mid
    df["bb_up"]  = mid + bb_std * std
    df["bb_lo"]  = mid - bb_std * std
    df["adx"]    = adx(df, 14)
    return df


def structural_sl_series(df: pd.DataFrame,
                         lookback: int = 12,
                         buf_atr: float = 0.10) -> Tuple[pd.Series, pd.Series]:
    lows  = df["low"].rolling(lookback).min().shift(1)
    highs = df["high"].rolling(lookback).max().shift(1)
    buf = buf_atr * df["atr"]
    sl_long  = lows  - buf
    sl_short = highs + buf
    return sl_long, sl_short


# ─────────────────────────── strategy variants ─────────────────────────
def strategy_trend_pullback(m5: pd.DataFrame, htf: pd.DataFrame, daily: Optional[pd.DataFrame],
                            *,
                            ema_fast: int = 20,
                            htf_ema: int = 50,
                            d1_ema: int = 50,
                            rsi_os: float = 35.0, rsi_ob: float = 65.0,
                            rsi_memory: int = 10,
                            wick_ratio: float = 0.55,
                            pullback_atr: float = 0.4,
                            session: Tuple[int, int] = (0, 24),
                            adx_min: float = 0.0,
                            atr_min_mult: float = 0.0,
                            sl_lookback: int = 12,
                            sl_buf_atr: float = 0.10,
                            confirms: Tuple[str, ...] = ("f_candle", "f_ema"),
                            ) -> pd.DataFrame:
    df = add_base_features(m5, ema_fast=ema_fast)
    htf_feat = htf_trend_dir(htf, ema_len=htf_ema)
    df = merge_htf(df, htf_feat, "h1")
    if daily is not None and not daily.empty:
        d1_feat = htf_trend_dir(daily, ema_len=d1_ema)
        df = merge_htf(df, d1_feat, "d1")
        same = (df["h1_trend"] == df["d1_trend"]) & (df["h1_trend"] != 0)
        df["trend_dir"] = np.where(same, df["h1_trend"], 0).astype(int)
    else:
        df["trend_dir"] = df["h1_trend"].astype(int)

    df["f_candle"] = f_pin_engulf(df, wick_ratio=wick_ratio)
    df["f_ema"]    = f_ema_pullback(df, "ema_fast", pullback_atr)
    df["f_bb"]     = f_bb_touch(df)
    df["f_rsi"]    = f_rsi_cross(df, rsi_os, rsi_ob)
    df["f_rsiR"]   = f_rsi_recent(df["f_rsi"], rsi_memory)

    sh, eh = session
    ny_h = (df["time"].dt.hour - BROKER_TO_NY_H) % 24
    df["in_session"] = (ny_h >= sh) & (ny_h < eh) if (sh != 0 or eh != 24) else True

    atr_med = df["atr"].rolling(500, min_periods=50).median()
    atr_ok  = df["atr"] >= (atr_min_mult * atr_med) if atr_min_mult > 0 else True
    adx_ok  = df["adx"] >= adx_min if adx_min > 0 else True

    base = df["in_session"] & atr_ok & adx_ok

    rl = (df["f_rsiR"] == 1); rs = (df["f_rsiR"] == -1)
    conf_vals = df[list(confirms)].values
    cl = (conf_vals ==  1).any(axis=1)
    cs = (conf_vals == -1).any(axis=1)
    long  = (df["trend_dir"] ==  1) & base & rl & cl
    short = (df["trend_dir"] == -1) & base & rs & cs
    df["signal"] = np.where(long, 1, np.where(short, -1, 0))

    sl_long, sl_short = structural_sl_series(df, sl_lookback, sl_buf_atr)
    df["sl_price"] = np.where(df["signal"] == 1, sl_long,
                       np.where(df["signal"] == -1, sl_short, np.nan))
    return df


def strategy_bb_mean_revert(m5: pd.DataFrame, htf: pd.DataFrame,
                            *,
                            bb_period: int = 20, bb_std: float = 2.0,
                            rsi_os: float = 30.0, rsi_ob: float = 70.0,
                            wick_ratio: float = 0.55,
                            require_pin: bool = True,
                            counter_htf: bool = False,
                            session: Tuple[int, int] = (0, 24),
                            adx_max: float = 30.0,
                            sl_lookback: int = 8,
                            sl_buf_atr: float = 0.10,
                            ) -> pd.DataFrame:
    df = add_base_features(m5, bb_period=bb_period, bb_std=bb_std)
    htf_feat = htf_trend_dir(htf, ema_len=50)
    df = merge_htf(df, htf_feat, "h1")

    df["f_candle"] = f_pin_engulf(df, wick_ratio=wick_ratio)
    df["f_bb"]     = f_bb_touch(df)
    df["f_rsi"]    = f_rsi_cross(df, rsi_os, rsi_ob)

    sh, eh = session
    ny_h = (df["time"].dt.hour - BROKER_TO_NY_H) % 24
    df["in_session"] = (ny_h >= sh) & (ny_h < eh) if (sh != 0 or eh != 24) else True
    adx_ok = df["adx"] <= adx_max if adx_max > 0 else True

    base = df["in_session"] & adx_ok

    long_sig  = (df["f_bb"] ==  1) & (df["f_rsi"] ==  1) & base
    short_sig = (df["f_bb"] == -1) & (df["f_rsi"] == -1) & base
    if require_pin:
        long_sig  &= (df["f_candle"] ==  1)
        short_sig &= (df["f_candle"] == -1)
    # Optional: allow only signals counter to the HTF trend (mean revert),
    # or trade with the HTF trend at oversold pullbacks (default).
    if counter_htf:
        long_sig  &= (df["h1_trend"] == -1)
        short_sig &= (df["h1_trend"] ==  1)
    else:
        long_sig  &= (df["h1_trend"] !=  -1)
        short_sig &= (df["h1_trend"] !=  1)

    df["signal"] = np.where(long_sig, 1, np.where(short_sig, -1, 0))

    sl_long, sl_short = structural_sl_series(df, sl_lookback, sl_buf_atr)
    df["sl_price"] = np.where(df["signal"] == 1, sl_long,
                       np.where(df["signal"] == -1, sl_short, np.nan))
    return df


def strategy_donchian_breakout(m5: pd.DataFrame, htf: pd.DataFrame,
                               *,
                               don_len: int = 20,
                               htf_ema: int = 50,
                               session: Tuple[int, int] = (0, 24),
                               adx_min: float = 18.0,
                               sl_atr_mult: float = 1.5,
                               sl_lookback: int = 12,
                               sl_buf_atr: float = 0.10,
                               require_htf_trend: bool = True,
                               ) -> pd.DataFrame:
    df = add_base_features(m5)
    htf_feat = htf_trend_dir(htf, ema_len=htf_ema)
    df = merge_htf(df, htf_feat, "h1")
    hh, ll = donchian(df, don_len)
    df["don_hi"], df["don_lo"] = hh.shift(1), ll.shift(1)

    long_brk  = df["high"] > df["don_hi"]
    short_brk = df["low"]  < df["don_lo"]

    sh, eh = session
    ny_h = (df["time"].dt.hour - BROKER_TO_NY_H) % 24
    df["in_session"] = (ny_h >= sh) & (ny_h < eh) if (sh != 0 or eh != 24) else True
    adx_ok = df["adx"] >= adx_min if adx_min > 0 else True
    base = df["in_session"] & adx_ok

    trend_long  = df["h1_trend"] ==  1 if require_htf_trend else True
    trend_short = df["h1_trend"] == -1 if require_htf_trend else True

    long_sig  = long_brk  & base & trend_long
    short_sig = short_brk & base & trend_short
    df["signal"] = np.where(long_sig, 1, np.where(short_sig, -1, 0))

    sl_long_struct, sl_short_struct = structural_sl_series(df, sl_lookback, sl_buf_atr)
    sl_long_atr  = df["close"] - sl_atr_mult * df["atr"]
    sl_short_atr = df["close"] + sl_atr_mult * df["atr"]
    sl_long  = np.minimum(sl_long_struct,  sl_long_atr)
    sl_short = np.maximum(sl_short_struct, sl_short_atr)
    df["sl_price"] = np.where(df["signal"] == 1, sl_long,
                       np.where(df["signal"] == -1, sl_short, np.nan))
    return df


def strategy_bb_revert_midline(m5: pd.DataFrame, htf: pd.DataFrame,
                                *,
                                bb_period: int = 20, bb_std: float = 2.0,
                                rsi_os: float = 30.0, rsi_ob: float = 70.0,
                                wick_ratio: float = 0.55,
                                require_pin: bool = True,
                                session: Tuple[int, int] = (0, 24),
                                adx_max: float = 0.0,
                                sl_atr_mult: float = 1.5,
                                sl_method: str = "atr",   # "atr" | "structural" | "outer_band"
                                sl_lookback: int = 8,
                                sl_buf_atr: float = 0.20,
                                trade_with_htf: bool = False,
                                tp_target: str = "mid",   # "mid" | "ema_fast"
                                ) -> pd.DataFrame:
    """BB outer-band touch + RSI extreme (+ optional pin) → TP at BB midline.

    SL strategy options:
      * "atr"        : entry ± sl_atr_mult * ATR (recommended for mean revert)
      * "structural" : last N-bar swing high/low ± sl_buf_atr*ATR
      * "outer_band" : just beyond the BB outer band that was touched
    """
    df = add_base_features(m5, bb_period=bb_period, bb_std=bb_std)
    htf_feat = htf_trend_dir(htf, ema_len=50)
    df = merge_htf(df, htf_feat, "h1")

    df["f_candle"] = f_pin_engulf(df, wick_ratio=wick_ratio)
    df["f_bb"]     = f_bb_touch(df)
    df["f_rsi"]    = f_rsi_cross(df, rsi_os, rsi_ob)

    sh, eh = session
    ny_h = (df["time"].dt.hour - BROKER_TO_NY_H) % 24
    df["in_session"] = (ny_h >= sh) & (ny_h < eh) if (sh != 0 or eh != 24) else True
    adx_ok = df["adx"] <= adx_max if adx_max > 0 else True
    base = df["in_session"] & adx_ok

    long_sig  = (df["f_bb"] ==  1) & (df["f_rsi"] ==  1) & base
    short_sig = (df["f_bb"] == -1) & (df["f_rsi"] == -1) & base
    if require_pin:
        long_sig  &= (df["f_candle"] ==  1)
        short_sig &= (df["f_candle"] == -1)
    if trade_with_htf:
        long_sig  &= (df["h1_trend"] !=  -1)
        short_sig &= (df["h1_trend"] !=  1)
    df["signal"] = np.where(long_sig, 1, np.where(short_sig, -1, 0))

    if sl_method == "atr":
        sl_long  = df["close"] - sl_atr_mult * df["atr"]
        sl_short = df["close"] + sl_atr_mult * df["atr"]
    elif sl_method == "outer_band":
        sl_long  = df["bb_lo"] - sl_buf_atr * df["atr"]
        sl_short = df["bb_up"] + sl_buf_atr * df["atr"]
    else:
        sl_long, sl_short = structural_sl_series(df, sl_lookback, sl_buf_atr)
    df["sl_price"] = np.where(df["signal"] == 1, sl_long,
                       np.where(df["signal"] == -1, sl_short, np.nan))
    df["tp_price"] = df["bb_mid"] if tp_target == "mid" else df["ema_fast"]
    return df


def strategy_rsi_extreme_reversal(m5: pd.DataFrame, htf: pd.DataFrame,
                                  *,
                                  rsi_os: float = 25.0, rsi_ob: float = 75.0,
                                  rsi_memory: int = 3,
                                  wick_ratio: float = 0.6,
                                  session: Tuple[int, int] = (0, 24),
                                  adx_max: float = 25.0,
                                  sl_lookback: int = 6,
                                  sl_buf_atr: float = 0.10,
                                  tp_ema_len: int = 20,
                                  ) -> pd.DataFrame:
    """RSI deep oversold/overbought + pin → TP at EMA20 (short-distance, high WR)."""
    df = add_base_features(m5, ema_fast=tp_ema_len)
    htf_feat = htf_trend_dir(htf, ema_len=50)
    df = merge_htf(df, htf_feat, "h1")

    df["f_candle"] = f_pin_engulf(df, wick_ratio=wick_ratio)
    df["f_rsi"]    = f_rsi_cross(df, rsi_os, rsi_ob)
    df["f_rsiR"]   = f_rsi_recent(df["f_rsi"], rsi_memory)

    sh, eh = session
    ny_h = (df["time"].dt.hour - BROKER_TO_NY_H) % 24
    df["in_session"] = (ny_h >= sh) & (ny_h < eh) if (sh != 0 or eh != 24) else True
    adx_ok = df["adx"] <= adx_max if adx_max > 0 else True
    base = df["in_session"] & adx_ok

    long_sig  = (df["f_rsiR"] ==  1) & (df["f_candle"] ==  1) & base
    short_sig = (df["f_rsiR"] == -1) & (df["f_candle"] == -1) & base
    df["signal"] = np.where(long_sig, 1, np.where(short_sig, -1, 0))

    sl_long, sl_short = structural_sl_series(df, sl_lookback, sl_buf_atr)
    df["sl_price"] = np.where(df["signal"] == 1, sl_long,
                       np.where(df["signal"] == -1, sl_short, np.nan))
    df["tp_price"] = df["ema_fast"]
    return df


def strategy_macd_pullback(m5: pd.DataFrame, htf: pd.DataFrame,
                            *,
                            htf_ema: int = 50,
                            ema_fast: int = 20,
                            pullback_atr: float = 0.5,
                            session: Tuple[int, int] = (0, 24),
                            adx_min: float = 0.0,
                            sl_lookback: int = 12,
                            sl_buf_atr: float = 0.10,
                            ) -> pd.DataFrame:
    df = add_base_features(m5, ema_fast=ema_fast)
    df["macd_hist"] = macd_hist(df["close"])
    htf_feat = htf_trend_dir(htf, ema_len=htf_ema)
    df = merge_htf(df, htf_feat, "h1")
    df["f_ema"]    = f_ema_pullback(df, "ema_fast", pullback_atr)
    df["f_candle"] = f_pin_engulf(df, wick_ratio=0.55)

    macd_pos  = df["macd_hist"] > 0
    macd_neg  = df["macd_hist"] < 0
    macd_turn_up   = macd_pos & (df["macd_hist"].shift(1) <= 0)
    macd_turn_dn   = macd_neg & (df["macd_hist"].shift(1) >= 0)

    sh, eh = session
    ny_h = (df["time"].dt.hour - BROKER_TO_NY_H) % 24
    df["in_session"] = (ny_h >= sh) & (ny_h < eh) if (sh != 0 or eh != 24) else True
    adx_ok = df["adx"] >= adx_min if adx_min > 0 else True
    base = df["in_session"] & adx_ok

    long_sig  = (df["h1_trend"] ==  1) & ((df["f_ema"] ==  1) | (df["f_candle"] ==  1)) & macd_turn_up   & base
    short_sig = (df["h1_trend"] == -1) & ((df["f_ema"] == -1) | (df["f_candle"] == -1)) & macd_turn_dn & base
    df["signal"] = np.where(long_sig, 1, np.where(short_sig, -1, 0))

    sl_long, sl_short = structural_sl_series(df, sl_lookback, sl_buf_atr)
    df["sl_price"] = np.where(df["signal"] == 1, sl_long,
                       np.where(df["signal"] == -1, sl_short, np.nan))
    return df


def strategy_ichimoku(m5: pd.DataFrame, htf: pd.DataFrame,
                       *,
                       tenkan: int = 9, kijun: int = 26, senkou_b: int = 52,
                       displacement: int = 26,
                       require_above_cloud: bool = True,
                       require_tk_cross: bool = True,
                       session: Tuple[int, int] = (0, 24),
                       adx_min: float = 0.0,
                       sl_method: str = "kijun",    # "kijun" | "structural" | "atr"
                       sl_atr_mult: float = 1.5,
                       sl_lookback: int = 12,
                       sl_buf_atr: float = 0.10,
                       use_htf_trend: bool = True,
                       ) -> pd.DataFrame:
    """Ichimoku Kinko Hyo trend-following strategy.

    Long entry: tenkan crosses above kijun + close above cloud_top (+ optional HTF EMA up).
    Short entry: mirror. SL anchored to kijun line, ATR, or structural swing.
    """
    df = add_base_features(m5)
    ic = ichimoku(df, tenkan, kijun, senkou_b, displacement)
    df = pd.concat([df, ic], axis=1)
    htf_feat = htf_trend_dir(htf, ema_len=50)
    df = merge_htf(df, htf_feat, "h1")

    tk_cross_up = (df["tenkan"] > df["kijun"]) & (df["tenkan"].shift(1) <= df["kijun"].shift(1))
    tk_cross_dn = (df["tenkan"] < df["kijun"]) & (df["tenkan"].shift(1) >= df["kijun"].shift(1))
    above_cloud = df["close"] > df["cloud_top"]
    below_cloud = df["close"] < df["cloud_bottom"]

    long_cond  = tk_cross_up if require_tk_cross else (df["tenkan"] > df["kijun"])
    short_cond = tk_cross_dn if require_tk_cross else (df["tenkan"] < df["kijun"])
    if require_above_cloud:
        long_cond  &= above_cloud
        short_cond &= below_cloud

    sh, eh = session
    ny_h = (df["time"].dt.hour - BROKER_TO_NY_H) % 24
    df["in_session"] = (ny_h >= sh) & (ny_h < eh) if (sh != 0 or eh != 24) else True
    adx_ok = df["adx"] >= adx_min if adx_min > 0 else True
    base = df["in_session"] & adx_ok

    if use_htf_trend:
        long_cond  &= (df["h1_trend"] !=  -1)
        short_cond &= (df["h1_trend"] !=   1)

    df["signal"] = np.where(long_cond & base,  1,
                    np.where(short_cond & base, -1, 0))

    if sl_method == "kijun":
        sl_long  = df["kijun"] - sl_buf_atr * df["atr"]
        sl_short = df["kijun"] + sl_buf_atr * df["atr"]
    elif sl_method == "atr":
        sl_long  = df["close"] - sl_atr_mult * df["atr"]
        sl_short = df["close"] + sl_atr_mult * df["atr"]
    else:
        sl_long, sl_short = structural_sl_series(df, sl_lookback, sl_buf_atr)
    df["sl_price"] = np.where(df["signal"] == 1, sl_long,
                       np.where(df["signal"] == -1, sl_short, np.nan))
    return df


def strategy_fib_pullback(m5: pd.DataFrame, htf: pd.DataFrame,
                           *,
                           swing_lookback: int = 5,
                           fib_min: float = 0.382,
                           fib_max: float = 0.618,
                           require_pin: bool = True,
                           wick_ratio: float = 0.55,
                           session: Tuple[int, int] = (0, 24),
                           adx_min: float = 0.0,
                           sl_method: str = "swing",  # "swing" | "atr"
                           sl_atr_mult: float = 1.5,
                           sl_buf_atr: float = 0.15,
                           use_htf_trend: bool = True,
                           tp_target: str = "swing", # "swing" | "ema_fast" | None (uses RR)
                           ) -> pd.DataFrame:
    """Fibonacci retracement bounce in trend direction.

    For a confirmed uptrend, after a swing low (L) and a higher swing high (H), the
    pullback into the 38.2–61.8 % retracement of L→H is bought on a bullish reaction.
    SL goes below the swing low (or ATR-based). TP defaults to the prior swing high
    (or EMA20 / RR-based if `tp_target` is set otherwise).
    """
    df = add_base_features(m5)
    htf_feat = htf_trend_dir(htf, ema_len=50)
    df = merge_htf(df, htf_feat, "h1")
    sh, sl = latest_swing_levels(df, swing_lookback)
    df["swing_high"] = sh
    df["swing_low"]  = sl

    rng = (df["swing_high"] - df["swing_low"]).replace(0, np.nan)
    fib_long_top  = df["swing_high"] - fib_min * rng   # upper bound of buy zone
    fib_long_bot  = df["swing_high"] - fib_max * rng   # lower bound of buy zone
    fib_short_bot = df["swing_low"]  + fib_min * rng
    fib_short_top = df["swing_low"]  + fib_max * rng

    in_long_zone  = (df["low"]  <= fib_long_top) & (df["low"]  >= fib_long_bot)
    in_short_zone = (df["high"] >= fib_short_bot) & (df["high"] <= fib_short_top)

    df["f_candle"] = f_pin_engulf(df, wick_ratio=wick_ratio)
    bull = (df["f_candle"] ==  1) if require_pin else (df["close"] > df["open"])
    bear = (df["f_candle"] == -1) if require_pin else (df["close"] < df["open"])

    sh_, eh = session
    ny_h = (df["time"].dt.hour - BROKER_TO_NY_H) % 24
    df["in_session"] = (ny_h >= sh_) & (ny_h < eh) if (sh_ != 0 or eh != 24) else True
    adx_ok = df["adx"] >= adx_min if adx_min > 0 else True
    base = df["in_session"] & adx_ok

    long_cond  = in_long_zone  & bull & base & (df["swing_high"] > df["swing_low"])
    short_cond = in_short_zone & bear & base & (df["swing_high"] > df["swing_low"])
    if use_htf_trend:
        long_cond  &= (df["h1_trend"] ==  1)
        short_cond &= (df["h1_trend"] == -1)

    df["signal"] = np.where(long_cond, 1, np.where(short_cond, -1, 0))

    if sl_method == "atr":
        sl_long  = df["close"] - sl_atr_mult * df["atr"]
        sl_short = df["close"] + sl_atr_mult * df["atr"]
    else:
        sl_long  = df["swing_low"]  - sl_buf_atr * df["atr"]
        sl_short = df["swing_high"] + sl_buf_atr * df["atr"]
    df["sl_price"] = np.where(df["signal"] == 1, sl_long,
                       np.where(df["signal"] == -1, sl_short, np.nan))

    if tp_target == "swing":
        df["tp_price"] = np.where(df["signal"] == 1, df["swing_high"],
                           np.where(df["signal"] == -1, df["swing_low"], np.nan))
    elif tp_target == "ema_fast":
        df["tp_price"] = df["ema_fast"]
    return df


def strategy_sr_zone_bounce(m5: pd.DataFrame, htf: pd.DataFrame,
                             *,
                             swing_lookback: int = 8,
                             zone_atr_band: float = 0.5,   # zone half-width in ATR units
                             min_touches: int = 2,          # how many swings must agree
                             touch_window: int = 200,       # how recent the agreeing swings must be
                             require_pin: bool = True,
                             wick_ratio: float = 0.55,
                             session: Tuple[int, int] = (0, 24),
                             adx_max: float = 35.0,
                             sl_atr_mult: float = 1.5,
                             sl_buf_atr: float = 0.20,
                             use_htf_trend: bool = False,
                             rr_for_tp: float = 1.2,
                             ) -> pd.DataFrame:
    """Strong support/resistance bounce.

    A *strong* zone is one where `min_touches` swing highs (resistance) or swing
    lows (support) clustered within `touch_window` bars sit inside a band of
    `±zone_atr_band × ATR`. The first reaction candle after price re-enters the
    zone is taken in the bounce direction. SL = entry ± `sl_atr_mult × ATR`.
    """
    df = add_base_features(m5)
    htf_feat = htf_trend_dir(htf, ema_len=50)
    df = merge_htf(df, htf_feat, "h1")
    is_h, is_l = swing_points(df, swing_lookback)
    sh_levels = df["high"].where(is_h).shift(swing_lookback)
    sl_levels = df["low"].where(is_l).shift(swing_lookback)

    n = len(df)
    res_strong = np.zeros(n, dtype=bool)
    sup_strong = np.zeros(n, dtype=bool)
    res_price  = np.full(n, np.nan)
    sup_price  = np.full(n, np.nan)
    h_arr = df["high"].to_numpy(); l_arr = df["low"].to_numpy()
    sh_arr = sh_levels.to_numpy(); sl_arr = sl_levels.to_numpy()
    atr_arr = df["atr"].to_numpy()
    # For each bar, look back at most `touch_window` bars, count swing levels
    # within ±band of the current price; if >= min_touches, mark it as strong.
    for i in range(n):
        if np.isnan(atr_arr[i]):
            continue
        band = zone_atr_band * atr_arr[i]
        lo_i = max(0, i - touch_window)
        sh_recent = sh_arr[lo_i:i + 1]
        sl_recent = sl_arr[lo_i:i + 1]
        sh_clean = sh_recent[~np.isnan(sh_recent)]
        sl_clean = sl_recent[~np.isnan(sl_recent)]
        if sh_clean.size >= min_touches:
            # cluster: pick most common (mean of points within band of latest)
            latest = sh_clean[-1]
            cluster = sh_clean[np.abs(sh_clean - latest) <= band]
            if cluster.size >= min_touches and h_arr[i] >= latest - band and h_arr[i] <= latest + band:
                res_strong[i] = True
                res_price[i] = float(cluster.mean())
        if sl_clean.size >= min_touches:
            latest = sl_clean[-1]
            cluster = sl_clean[np.abs(sl_clean - latest) <= band]
            if cluster.size >= min_touches and l_arr[i] >= latest - band and l_arr[i] <= latest + band:
                sup_strong[i] = True
                sup_price[i] = float(cluster.mean())

    df["res_strong"] = res_strong
    df["sup_strong"] = sup_strong
    df["res_price"]  = res_price
    df["sup_price"]  = sup_price
    df["f_candle"]   = f_pin_engulf(df, wick_ratio=wick_ratio)

    sh_, eh = session
    ny_h = (df["time"].dt.hour - BROKER_TO_NY_H) % 24
    df["in_session"] = (ny_h >= sh_) & (ny_h < eh) if (sh_ != 0 or eh != 24) else True
    adx_ok = df["adx"] <= adx_max if adx_max > 0 else True
    base = df["in_session"] & adx_ok

    long_cond  = df["sup_strong"] & (df["f_candle"] ==  1 if require_pin else df["close"] > df["open"]) & base
    short_cond = df["res_strong"] & (df["f_candle"] == -1 if require_pin else df["close"] < df["open"]) & base
    if use_htf_trend:
        long_cond  &= (df["h1_trend"] !=  -1)
        short_cond &= (df["h1_trend"] !=   1)
    df["signal"] = np.where(long_cond, 1, np.where(short_cond, -1, 0))

    sl_long  = df["close"] - sl_atr_mult * df["atr"]
    sl_short = df["close"] + sl_atr_mult * df["atr"]
    df["sl_price"] = np.where(df["signal"] == 1, sl_long,
                       np.where(df["signal"] == -1, sl_short, np.nan))
    return df


def strategy_vwap_reaction(m5: pd.DataFrame, htf: pd.DataFrame,
                            *,
                            require_pin: bool = True,
                            wick_ratio: float = 0.55,
                            touch_tol_atr: float = 0.25,
                            session: Tuple[int, int] = (0, 24),
                            adx_max: float = 35.0,
                            sl_atr_mult: float = 1.5,
                            sl_buf_atr: float = 0.15,
                            use_htf_trend: bool = True,
                            ) -> pd.DataFrame:
    """Bounce off the daily VWAP in the direction of the HTF trend."""
    df = add_base_features(m5)
    htf_feat = htf_trend_dir(htf, ema_len=50)
    df = merge_htf(df, htf_feat, "h1")
    df["vwap"] = vwap_daily(df)
    df["f_candle"] = f_pin_engulf(df, wick_ratio=wick_ratio)
    tol = touch_tol_atr * df["atr"]
    touched_long  = (df["low"]  <= df["vwap"] + tol) & (df["close"] > df["vwap"])
    touched_short = (df["high"] >= df["vwap"] - tol) & (df["close"] < df["vwap"])

    sh, eh = session
    ny_h = (df["time"].dt.hour - BROKER_TO_NY_H) % 24
    df["in_session"] = (ny_h >= sh) & (ny_h < eh) if (sh != 0 or eh != 24) else True
    adx_ok = df["adx"] <= adx_max if adx_max > 0 else True
    base = df["in_session"] & adx_ok

    long_cond  = touched_long  & base
    short_cond = touched_short & base
    if require_pin:
        long_cond  &= (df["f_candle"] ==  1)
        short_cond &= (df["f_candle"] == -1)
    if use_htf_trend:
        long_cond  &= (df["h1_trend"] ==  1)
        short_cond &= (df["h1_trend"] == -1)
    df["signal"] = np.where(long_cond, 1, np.where(short_cond, -1, 0))

    sl_long  = df["close"] - sl_atr_mult * df["atr"]
    sl_short = df["close"] + sl_atr_mult * df["atr"]
    df["sl_price"] = np.where(df["signal"] == 1, sl_long,
                       np.where(df["signal"] == -1, sl_short, np.nan))
    return df


def strategy_ema_cross(m5: pd.DataFrame, htf: pd.DataFrame,
                        *,
                        ema_fast_n: int = 9,
                        ema_slow_n: int = 21,
                        require_slope: bool = True,
                        session: Tuple[int, int] = (0, 24),
                        adx_min: float = 18.0,
                        sl_atr_mult: float = 1.5,
                        sl_buf_atr: float = 0.10,
                        sl_lookback: int = 12,
                        use_htf_trend: bool = True,
                        ) -> pd.DataFrame:
    """Classic fast/slow EMA momentum cross with HTF trend filter."""
    df = add_base_features(m5)
    df["ema_x_fast"] = ema(df["close"], ema_fast_n)
    df["ema_x_slow"] = ema(df["close"], ema_slow_n)
    htf_feat = htf_trend_dir(htf, ema_len=50)
    df = merge_htf(df, htf_feat, "h1")

    cross_up = (df["ema_x_fast"] > df["ema_x_slow"]) & (df["ema_x_fast"].shift(1) <= df["ema_x_slow"].shift(1))
    cross_dn = (df["ema_x_fast"] < df["ema_x_slow"]) & (df["ema_x_fast"].shift(1) >= df["ema_x_slow"].shift(1))

    if require_slope:
        slope_up = df["ema_x_slow"] > df["ema_x_slow"].shift(3)
        slope_dn = df["ema_x_slow"] < df["ema_x_slow"].shift(3)
        cross_up &= slope_up
        cross_dn &= slope_dn

    sh, eh = session
    ny_h = (df["time"].dt.hour - BROKER_TO_NY_H) % 24
    df["in_session"] = (ny_h >= sh) & (ny_h < eh) if (sh != 0 or eh != 24) else True
    adx_ok = df["adx"] >= adx_min if adx_min > 0 else True
    base = df["in_session"] & adx_ok

    long_cond  = cross_up & base
    short_cond = cross_dn & base
    if use_htf_trend:
        long_cond  &= (df["h1_trend"] ==  1)
        short_cond &= (df["h1_trend"] == -1)
    df["signal"] = np.where(long_cond, 1, np.where(short_cond, -1, 0))

    sl_struct_long, sl_struct_short = structural_sl_series(df, sl_lookback, sl_buf_atr)
    sl_atr_long  = df["close"] - sl_atr_mult * df["atr"]
    sl_atr_short = df["close"] + sl_atr_mult * df["atr"]
    sl_long  = np.minimum(sl_struct_long,  sl_atr_long)
    sl_short = np.maximum(sl_struct_short, sl_atr_short)
    df["sl_price"] = np.where(df["signal"] == 1, sl_long,
                       np.where(df["signal"] == -1, sl_short, np.nan))
    return df


# ─────────────────────────── runner ────────────────────────────────────
def run_strategy(symbol: str, base_tf: str, htf: str,
                 strategy_fn: Callable, params: dict, *,
                 use_daily: bool = True,
                 rr: float = 2.0,
                 max_hold_bars: int = 96,
                 tp_col: Optional[str] = None,
                 date_from: Optional[str] = None,
                 date_to:   Optional[str] = None,
                 ) -> Tuple[List[Trade], pd.DataFrame]:
    m5 = load_ohlcv(symbol, base_tf, date_from, date_to)
    h1 = load_ohlcv(symbol, htf, date_from, date_to)
    if m5.empty or h1.empty or len(m5) < 500 or len(h1) < 100:
        return [], pd.DataFrame()
    d1 = load_ohlcv(symbol, "D1", date_from, date_to) if use_daily else pd.DataFrame()
    fn_sig = strategy_fn.__code__.co_varnames[:strategy_fn.__code__.co_argcount]
    if "daily" in fn_sig:
        df = strategy_fn(m5, h1, d1 if not d1.empty else None, **params)
    else:
        df = strategy_fn(m5, h1, **params)
    use_tp = tp_col if (tp_col and tp_col in df.columns) else (
        "tp_price" if "tp_price" in df.columns else None)
    trades = backtest(df, rr=rr, max_hold_bars=max_hold_bars, tp_col=use_tp)
    return trades, df


# ───────────────────────────── reporting ───────────────────────────────
def summarise(trades: List[Trade], df: pd.DataFrame,
              symbol: str, strategy: str, params: dict,
              rr: float, base_tf: str, htf: str) -> dict:
    s = stats(trades, df, rr=rr)
    return {
        "symbol": symbol,
        "strategy": strategy,
        "base_tf": base_tf,
        "htf": htf,
        "rr": rr,
        "params": params,
        **s,
    }
