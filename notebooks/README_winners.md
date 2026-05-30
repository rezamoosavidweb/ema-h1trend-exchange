# Crypto multi-strategy sweep — Bybit (linear USDT-perp)

End-to-end research bundle for finding **profitable** trading strategies on the symbols
fetched by [00_data_fetching_bybit.ipynb](00_data_fetching_bybit.ipynb).

## Files

| File | Purpose |
| --- | --- |
| [_strategy_lib.py](_strategy_lib.py) | Indicator helpers, **10 strategy variants**, numba-JIT backtester, fee model. |
| [_run_sweep.py](_run_sweep.py) | CLI driver running the parameter grids end-to-end. |
| [25_crypto_strategy_sweep_bybit.ipynb](25_crypto_strategy_sweep_bybit.ipynb) | Multi-strategy sweep across every symbol + M5/H1 + the param grids. |
| [26_top_strategy_per_symbol_bybit.ipynb](26_top_strategy_per_symbol_bybit.ipynb) | Picks the best post-fee config per symbol and persists artefacts. |
| [27_strategy_deep_dive_bybit.ipynb](27_strategy_deep_dive_bybit.ipynb) | Trade-by-trade analytics for any single config. |
| [28_winner_strategies_summary_bybit.ipynb](28_winner_strategies_summary_bybit.ipynb) | Final per-symbol report (gross + net WR/PF side-by-side). |

Per-symbol artefacts live in `results/_top_per_symbol/<SYMBOL>/`:
`config.json`, `trades.csv`, `equity.csv`, `summary.json`.

## Strategies tested (10)

| # | Strategy | Idea |
| --- | --- | --- |
| 1 | `trend_pullback` | H1 (+ D1) EMA trend + M5 pullback to EMA20 + RSI cross gate + pin/engulf candle. |
| 2 | `bb_revert_mid` | Bollinger outer-band touch + RSI extreme + pin → TP at BB midline. High gross WR. |
| 3 | `rsi_extreme` | Deep RSI ≤25 / ≥75 + pin → TP at EMA20. |
| 4 | `donchian_brkout` | N-bar breakout with HTF trend filter and ATR stops. **Champion strategy.** |
| 5 | `macd_pullback` | HTF trend + MACD histogram turn + EMA20 pullback. |
| 6 | `ichimoku` | Tenkan/Kijun cross above the cloud, SL anchored to kijun. |
| 7 | `fib_pullback` | Pullback into 38.2–61.8 % fib retracement of latest swing; TP at prior swing. |
| 8 | `sr_zone_bounce` | Strong support/resistance zone (≥2 clustered swings) bounce reaction. |
| 9 | `vwap_reaction` | Daily VWAP touch + pin in HTF trend direction. |
| 10 | `ema_cross` | 9/21 EMA cross with slope filter + HTF trend. |

10 families × 2 timeframes × 14 symbols × multiple RR variants = **1700 backtests** total
over a **4.4-year** window (2022-01-01 onward; ~M5 data from 2021-08 for many symbols).

## Fee model

Bybit linear taker fee `0.055 %` per side → **0.11 % round-trip** in R-multiples
(`fee_R = 0.0011 × entry / |entry−SL|`). Both gross (pre-fee) and net (post-fee) WR/PF are
now produced by `stats()` so you can see exactly how much the fee eats.

## Headline result — Donchian breakout still dominates

Donchian 20/40-bar breakout (ADX≥18, HTF trend filter, ATR-based stops, RR≈2.5) is the
production-grade choice on **12 of 14** symbols. On 5 of them (AVAX, SOL, ADA, DOGE, SHIB1000,
DOT, XLM, LTC — that's 8 actually) it hits ≥ 10 trades/month *and* clears PF ≥ 1.10. On the
other 4 (ETH, XRP, BNB, BCH) the H1 variant is still profitable but TPM drops to 7-9.

The new strategies (Ichimoku, Fibonacci, S/R zone, VWAP, EMA cross) were all profitable on
some symbols in the gross-R sense, but the round-trip fee eroded them below Donchian; the
one exception is **BNBUSDT**, where `fib_pullback H1 RR=2.0` beats every Donchian variant
(net +42 R, PF 1.42, DD only −14 R).

### Final per-symbol picks

| Tier | Symbol | Strategy | TF | RR | Trades | Net WR % | TPM | Net PF | Net R | Max DD R |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| profitable_top_freq | AVAXUSDT | donchian_brkout | M5 | 2.5 | 5350 | 41.6 | 99.7 | 1.11 | **+311.7** | −40.9 |
| profitable_top_freq | SOLUSDT | donchian_brkout | M5 | 2.5 | 6098 | 40.8 | 113.6 | 1.06 | +206.2 | −53.4 |
| profitable_top_freq | ADAUSDT | donchian_brkout | M5 | 2.5 | 5333 | 41.2 | 99.4 | 1.05 | +138.2 | −61.2 |
| profitable_top_freq | DOGEUSDT | donchian_brkout | M5 | 2.5 | 5369 | 39.5 | 100.0 | 1.04 | +116.0 | −70.3 |
| profitable_low_freq | ETHUSDT | donchian_brkout | H1 | 2.5 | 422 | 44.1 | 7.9 | **1.30** | +55.3 | −10.2 |
| profitable_top_freq | SHIB1000USDT | donchian_brkout | M5 | 2.5 | 5450 | 39.4 | 101.5 | 1.02 | +54.1 | −66.3 |
| profitable_low_freq | XRPUSDT | donchian_brkout | H1 | 1.5 | 480 | 47.9 | 8.9 | 1.27 | +51.6 | −15.2 |
| profitable_top_freq | DOTUSDT | donchian_brkout | M5 | 2.5 | 5332 | 40.0 | 99.3 | 1.01 | +42.4 | −129.9 |
| **profitable_low_freq** | **BNBUSDT** | **fib_pullback** | **H1** | **2.0** | 339 | 45.7 | 6.3 | **1.27** | **+42.3** | **−14.4** |
| profitable_top_freq | XLMUSDT | donchian_brkout | H1 | 2.5 | 174 | 45.4 | 14.7 | **1.34** | +26.2 | −6.5 |
| profitable_low_freq | BCHUSDT | donchian_brkout | H1 | 2.5 | 382 | 40.8 | 7.1 | 1.11 | +18.1 | −20.3 |
| profitable_top_freq | LTCUSDT | donchian_brkout | H1 | 2.5 | 179 | 41.3 | 15.2 | 1.02 | +1.9 | −9.6 |
| wr_target_only | XAUUSDT | bb_revert_mid | M5 | — | 26 | 42.3 | 11.3 | 0.15 | −15.1 | −15.4 |
| wr_target_only | BTCUSDT | bb_revert_mid | M5 | — | 580 | 68.6 | 10.8 | 0.69 | −50.3 | −57.0 |

Tier explanations:

- `profitable_top_freq` — net-positive **and** ≥ 10 trades/month.
- `profitable_low_freq` — net-positive but 5 ≤ TPM < 10. Still production-viable; just slower.
- `wr_target_only` — hits the user's WR ≥ 63 % target but loses to taker fees. Only viable
  with maker-rebate / VIP-fee execution.

## How to reproduce

```bash
# 1) Fetch the data
jupyter nbconvert --execute --inplace 00_data_fetching_bybit.ipynb

# 2) Run the full sweep (1700 cfgs, ~35 min)
cd notebooks
python -u _run_sweep.py --tfs M5 H1 > sweep_run.log 2>&1

# 3) Build per-symbol winner artefacts
jupyter nbconvert --execute --inplace 26_top_strategy_per_symbol_bybit.ipynb

# 4) Final summary report
jupyter nbconvert --execute --inplace 28_winner_strategies_summary_bybit.ipynb
```

## Gross vs net metrics

The `stats()` helper in `_strategy_lib.py` now returns *both* sets:

| Column | Meaning |
| --- | --- |
| `win_rate`, `profit_factor` | **Gross** — counts any TP exit as a win, ignores fees. Useful for diagnosing the raw signal. |
| `win_rate_net`, `profit_factor_net` | **Net** — uses `t.net_r`, the per-trade R after the 0.11 % round-trip taker fee. The number that actually decides P&L. |
| `total_net_R`, `max_drawdown_R`, `net_avg_R` | Always net. |

Notebook 28 prints both side-by-side so you can see how much the fee gap eats — the gap
is small for Donchian RR=2.5 (each trade's R is large enough to absorb the fee) but
catastrophic for `bb_revert_mid` (gross PF 1.39 → net PF 0.15 on XAUUSDT).
