"""
Sweep driver: runs many parameter grids across many strategies and many
symbols, then filters to (win_rate >= 63%) AND (trades_per_month >= 10).

Outputs:
  results/_sweep_crypto/sweep_all.csv      — every config evaluated
  results/_sweep_crypto/sweep_winners.csv  — configs meeting the WR + frequency bar
"""
from __future__ import annotations

import argparse, itertools, json, sys, time
from pathlib import Path
from typing import Dict, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _strategy_lib import (
    list_symbols, run_strategy, summarise,
    strategy_trend_pullback, strategy_bb_mean_revert,
    strategy_donchian_breakout, strategy_macd_pullback,
    strategy_bb_revert_midline, strategy_rsi_extreme_reversal,
    strategy_ichimoku, strategy_fib_pullback, strategy_sr_zone_bounce,
    strategy_vwap_reaction, strategy_ema_cross,
)

OUT_DIR = Path(__file__).resolve().parent / "results" / "_sweep_crypto"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Strategy grids (tight, focused on combinations likely to give WR>=63%) ──
TREND_GRID_M5 = {
    "rsi_os":       [35],
    "rsi_ob":       [65],
    "rsi_memory":   [10],
    "wick_ratio":   [0.55],
    "pullback_atr": [0.4],
    "session":      [(0, 24)],
    "adx_min":      [0, 18],
    "atr_min_mult": [0.0, 0.7],
    "sl_lookback":  [12],
    "sl_buf_atr":   [0.10],
    "confirms":     [("f_candle", "f_ema")],
}

TREND_GRID_H1 = {
    "rsi_os":       [35],
    "rsi_ob":       [65],
    "rsi_memory":   [5, 10],
    "wick_ratio":   [0.55],
    "pullback_atr": [0.4],
    "session":      [(0, 24)],
    "adx_min":      [0, 18],
    "atr_min_mult": [0.0],
    "sl_lookback":  [12],
    "sl_buf_atr":   [0.10],
    "confirms":     [("f_candle", "f_ema")],
}

BB_REVERT_GRID = {
    "bb_std":       [2.0, 2.5],
    "rsi_os":       [30],
    "rsi_ob":       [70],
    "require_pin":  [True],
    "session":      [(0, 24)],
    "adx_max":      [0],
    "sl_atr_mult":  [2.5, 3.5, 4.0],
    "sl_method":    ["atr"],
    "trade_with_htf": [False, True],
    "tp_target":    ["mid"],
}

RSI_EXTREME_GRID = {
    "rsi_os":     [25],
    "rsi_ob":     [75],
    "rsi_memory": [3, 5],
    "wick_ratio": [0.55, 0.6],
    "session":    [(0, 24)],
    "adx_max":    [0, 25],
    "sl_lookback":[8],
    "sl_buf_atr": [0.20],
    "tp_ema_len": [20],
}

DONCHIAN_GRID = {
    "don_len":          [20, 40],
    "session":          [(0, 24)],
    "adx_min":          [18],
    "sl_atr_mult":      [1.5],
    "sl_lookback":      [12],
    "sl_buf_atr":       [0.10],
    "require_htf_trend":[True],
}

MACD_GRID = {
    "ema_fast":     [20],
    "pullback_atr": [0.4],
    "session":      [(0, 24)],
    "adx_min":      [0, 18],
    "sl_lookback":  [12],
    "sl_buf_atr":   [0.10],
}

ICHIMOKU_GRID = {
    "tenkan": [9], "kijun": [26], "senkou_b": [52], "displacement": [26],
    "require_above_cloud": [True],
    "require_tk_cross":    [True],
    "session": [(0, 24)],
    "adx_min": [18],
    "sl_method": ["kijun"],
    "sl_atr_mult": [1.5],
    "sl_lookback": [12],
    "sl_buf_atr":  [0.10],
    "use_htf_trend": [True],
}

FIB_GRID = {
    "swing_lookback": [5, 8],
    "fib_min": [0.382], "fib_max": [0.618],
    "require_pin": [True],
    "wick_ratio": [0.55],
    "session": [(0, 24)],
    "adx_min": [0],
    "sl_method": ["swing"],
    "sl_atr_mult": [1.5],
    "sl_buf_atr":  [0.20],
    "use_htf_trend": [True],
    "tp_target": [None],
}

SR_ZONE_GRID = {
    "swing_lookback": [8],
    "zone_atr_band":  [0.5],
    "min_touches":    [2, 3],
    "touch_window":   [200],
    "require_pin":    [True],
    "wick_ratio":     [0.55],
    "session":        [(0, 24)],
    "adx_max":        [35],
    "sl_atr_mult":    [1.5, 2.0],
    "sl_buf_atr":     [0.20],
    "use_htf_trend":  [False, True],
    "rr_for_tp":      [1.2],
}

VWAP_GRID = {
    "require_pin":    [True],
    "wick_ratio":     [0.55],
    "touch_tol_atr":  [0.25],
    "session":        [(0, 24)],
    "adx_max":        [35],
    "sl_atr_mult":    [1.5],
    "sl_buf_atr":     [0.15],
    "use_htf_trend":  [True],
}

EMA_CROSS_GRID = {
    "ema_fast_n":     [9],
    "ema_slow_n":     [21],
    "require_slope":  [True],
    "session":        [(0, 24)],
    "adx_min":        [18],
    "sl_atr_mult":    [1.5],
    "sl_buf_atr":     [0.10],
    "sl_lookback":    [12],
    "use_htf_trend":  [True],
}

# RR grid per strategy (mean-revert uses tp_col so RR is unused but kept for API uniformity)
STRATS_M5 = [
    ("trend_pullback",  strategy_trend_pullback,       TREND_GRID_M5,    True,  [0.75, 1.0, 1.5, 2.0]),
    ("bb_revert_mid",   strategy_bb_revert_midline,    BB_REVERT_GRID,   False, [None]),
    ("rsi_extreme",     strategy_rsi_extreme_reversal, RSI_EXTREME_GRID, False, [None]),
    ("donchian_brkout", strategy_donchian_breakout,    DONCHIAN_GRID,    False, [1.0, 1.5, 2.0, 2.5]),
    ("macd_pullback",   strategy_macd_pullback,        MACD_GRID,        False, [1.0, 1.5]),
    ("ichimoku",        strategy_ichimoku,             ICHIMOKU_GRID,    False, [2.0, 2.5, 3.0]),
    ("fib_pullback",    strategy_fib_pullback,         FIB_GRID,         False, [1.5, 2.0]),
    ("sr_zone_bounce",  strategy_sr_zone_bounce,       SR_ZONE_GRID,     False, [1.2, 1.5]),
    ("vwap_reaction",   strategy_vwap_reaction,        VWAP_GRID,        False, [1.5, 2.0]),
    ("ema_cross",       strategy_ema_cross,            EMA_CROSS_GRID,   False, [1.5, 2.0]),
]

STRATS_H1 = [
    ("trend_pullback",  strategy_trend_pullback,       TREND_GRID_H1,    True,  [1.0, 1.5, 2.0, 2.5]),
    ("donchian_brkout", strategy_donchian_breakout,    DONCHIAN_GRID,    False, [1.5, 2.0, 2.5]),
    ("ichimoku",        strategy_ichimoku,             ICHIMOKU_GRID,    False, [2.0, 2.5, 3.0]),
    ("fib_pullback",    strategy_fib_pullback,         FIB_GRID,         False, [1.5, 2.0, 2.5]),
    ("sr_zone_bounce",  strategy_sr_zone_bounce,       SR_ZONE_GRID,     False, [1.5, 2.0]),
    ("ema_cross",       strategy_ema_cross,            EMA_CROSS_GRID,   False, [1.5, 2.0, 2.5]),
]


def grid_product(grid: Dict[str, list]) -> List[dict]:
    keys = list(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*[grid[k] for k in keys])]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--tfs", nargs="*", default=["M5", "H1"])
    p.add_argument("--strategies", nargs="*", default=None)
    p.add_argument("--date-from", default="2022-01-01")
    p.add_argument("--date-to",   default=None)
    p.add_argument("--min-wr",      type=float, default=63.0)
    p.add_argument("--min-tpm",     type=float, default=10.0)
    p.add_argument("--max-per-strat", type=int, default=None)
    args = p.parse_args()

    symbols = args.symbols or list_symbols()
    # Skip FX-style symbols that may have appeared in the data dir.
    symbols = [s for s in symbols if "USDT" in s.upper() or "USD" in s.upper() and s not in {"DOGEUSD", "LTCUSD"}]
    symbols = [s for s in symbols if s not in {"DOGEUSD", "LTCUSD", "XAUUSD", "XAGUSDT"}]
    print(f"Symbols ({len(symbols)}): {symbols}")
    print(f"TFs     : {args.tfs}")

    all_rows = []
    t0 = time.time()
    total_runs = 0

    for tf in args.tfs:
        strats = STRATS_M5 if tf == "M5" else STRATS_H1
        htf = "H1" if tf == "M5" else "H4"
        max_hold = 96 if tf == "M5" else 48

        if args.strategies:
            strats = [s for s in strats if s[0] in set(args.strategies)]

        for sym in symbols:
            for sname, sfn, sgrid, use_daily, rr_grid in strats:
                combos = grid_product(sgrid)
                if args.max_per_strat:
                    combos = combos[:args.max_per_strat]
                for params in combos:
                    for rr in rr_grid:
                        kw = dict(use_daily=use_daily, max_hold_bars=max_hold,
                                  date_from=args.date_from, date_to=args.date_to)
                        if rr is not None:
                            kw["rr"] = float(rr)
                        try:
                            trades, df = run_strategy(sym, tf, htf, sfn, params, **kw)
                        except Exception as exc:
                            print(f"  ✗ {sym} {tf} {sname} rr={rr} {params}: {exc!r}")
                            continue
                        if df.empty:
                            continue
                        row = summarise(trades, df, sym, sname, params,
                                        rr if rr is not None else 0.0, tf, htf)
                        row["params_json"] = json.dumps(params, default=str)
                        del row["params"]
                        all_rows.append(row)
                        total_runs += 1
                        if total_runs % 100 == 0:
                            elapsed = time.time() - t0
                            print(f"... {total_runs} runs in {elapsed:.1f}s")

    df = pd.DataFrame(all_rows)
    if df.empty:
        print("No runs produced any output.")
        return
    # Composite score: rewards profitable strategies with good frequency and
    # reasonable drawdown. score = net_avg_R * sqrt(TPM) / (1 + |max_dd|/total_net_R^+)
    import numpy as np
    safe_net = np.maximum(df["total_net_R"].to_numpy(), 1e-9)
    dd_pen   = np.where(df["total_net_R"] > 0,
                        1.0 + np.abs(df["max_drawdown_R"]) / safe_net,
                        np.inf)
    df["score"] = df["net_avg_R"] * np.sqrt(np.maximum(df["trades_per_month"], 0)) / dd_pen
    df = df.sort_values("score", ascending=False)

    out_all = OUT_DIR / "sweep_all.csv"
    df.to_csv(out_all, index=False)
    print(f"\nAll runs       -> {out_all} ({len(df)})")

    # Winners by user's hard targets
    win = df[(df["win_rate"] >= args.min_wr) & (df["trades_per_month"] >= args.min_tpm)]
    out_w = OUT_DIR / "sweep_winners_wr.csv"
    win.to_csv(out_w, index=False)
    print(f"Winners (WR>={args.min_wr}, TPM>={args.min_tpm}): {len(win)} -> {out_w}")

    # Profit-aware winners: profitable + decent metrics
    prof = df[(df["total_net_R"] > 0) &
              (df["profit_factor"] >= 1.10) &
              (df["trades_per_month"] >= args.min_tpm) &
              (df["win_rate"] >= 50)]
    out_p = OUT_DIR / "sweep_winners_profitable.csv"
    prof.to_csv(out_p, index=False)
    print(f"Profitable     (PF>=1.10, TPM>={args.min_tpm}, WR>=50): {len(prof)} -> {out_p}")

    # Best per (symbol, base_tf) by composite score
    best = (df[df["total_net_R"] > 0]
            .sort_values("score", ascending=False)
            .groupby(["symbol", "base_tf"], as_index=False).head(3))
    out_b = OUT_DIR / "sweep_best_per_symbol.csv"
    best.to_csv(out_b, index=False)
    print(f"Best per symbol (top 3 profitable by score): {len(best)} -> {out_b}")

    if len(prof):
        cols = ["symbol", "strategy", "base_tf", "rr",
                "trades", "win_rate", "trades_per_month",
                "profit_factor", "total_net_R", "max_drawdown_R", "score"]
        print("\nTop 25 profitable by score:")
        print(prof[cols].head(25).to_string(index=False))


if __name__ == "__main__":
    main()
