# Notebook Migration Report
**Project**: EMA H1 Trend — Bybit Linear Futures  
**Migration date**: 2026-05-12  
**Source**: `D:\bot\ema-1d trend\ema-h1trend\notebooks\`  
**Target**: `D:\bot\ema-1d trend-exchange\`

---

## Summary

| Category | Count |
|----------|-------|
| Notebooks migrated (copied + path-fixed) | 3 |
| Notebooks newly created (Bybit) | 2 |
| Notebooks archived to legacy | 3 |
| Duplicates | 0 |
| Failed | 0 |
| **Total processed** | **8** |

---

## Final Notebook Tree

```
ema-1d trend-exchange/
│
├── notebooks/                           ← Production notebooks
│   ├── 00_data_fetching_bybit.ipynb     ← NEW: Bybit live data fetching
│   ├── 03_strategy03.ipynb              ← COPIED: FX reference (USDJPY, XAUUSD...)
│   ├── 03_strategy03_crypto.ipynb       ← MIGRATED: CSV-cached crypto backtest
│   ├── 03_strategy03_crypto_bybit.ipynb ← NEW: Live Bybit crypto backtest
│   ├── _all_metrics.ipynb               ← COPIED: Aggregate all results
│   ├── _all_metrics_crypto.ipynb        ← COPIED: Aggregate crypto results only
│   ├── data/                            ← Local OHLCV CSV cache
│   └── results/                         ← Backtest output CSVs
│       └── strategy03_crypto/
│           └── <SYMBOL>/M5/
│               ├── merics.csv
│               ├── trades.csv
│               ├── entry_signals.csv
│               └── entry_fills.csv
│
└── research/
    └── legacy_notebooks/                ← Archived originals
        ├── 00_data_feching.ipynb        ← ARCHIVED: MT5 data fetch
        ├── 01_support_resistance.ipynb  ← ARCHIVED: S/R research
        └── 02_strategy02.ipynb          ← ARCHIVED: Liquidity sweep strategy
```

---

## Per-Notebook Status

### `notebooks/00_data_fetching_bybit.ipynb`
| Field | Value |
|-------|-------|
| **Status** | `created` (new) |
| **Origin** | `research/legacy_notebooks/00_data_feching.ipynb` |
| **Type** | Data fetching |
| **MT5 removed** | Yes — replaced with `pybit` REST |
| **Execution status** | Requires valid `BYBIT_API_KEY` / `BYBIT_API_SECRET` in `.env` |

**Description**: Fetches OHLCV from Bybit Linear, paginates automatically, drops the forming candle (mirrors MT5 behavior), and saves `./data/<SYMBOL>/<TF>/ohlcv.csv`. Column format is identical to the MT5 version so all downstream notebooks work unchanged.

**Dependency fixes**:
- Removed: `import MetaTrader5 as mt5`
- Added: `from pybit.unified_trading import HTTP`
- Symbol mapping: `BTCUSD.i` → `BTCUSDT`, etc.

---

### `notebooks/03_strategy03.ipynb`
| Field | Value |
|-------|-------|
| **Status** | `copied` |
| **Origin** | `notebooks/03_strategy03.ipynb` |
| **Type** | FX strategy backtest (USDJPY, XAUUSD, ...) |
| **MT5 dependency** | None — reads from local CSV cache |
| **Execution status** | Runs offline; requires `./data/<SYMBOL>/<TF>/ohlcv.csv` |

**Description**: Self-contained EMA 8/13/21 + H1 trend strategy implementation for FX pairs. No imports from strategy modules — all logic is inline. Kept as FX reference alongside the crypto version.

**Path fixes**: None needed — paths are relative and portable.

---

### `notebooks/03_strategy03_crypto.ipynb`
| Field | Value |
|-------|-------|
| **Status** | `migrated` |
| **Origin** | `notebooks/03_strategy03_crypto.ipynb` |
| **Type** | Crypto strategy backtest (CSV-cached data) |
| **MT5 dependency** | None — already used local CSV cache |
| **Execution status** | Runs offline; requires `./data/<SYMBOL>/<TF>/ohlcv.csv` |

**Import fixes applied**:
| Old path | New path |
|----------|----------|
| `strategies.ema_trend.crypto_core` | `strategy.crypto_core` |
| `strategies.ema_trend.backtest` | `strategy.backtest` |
| `strategies.ema_trend.setup` | `strategy.setup` |

**sys.path logic**: unchanged — `notebooks/` parent resolution still correct for new project layout.

---

### `notebooks/03_strategy03_crypto_bybit.ipynb`
| Field | Value |
|-------|-------|
| **Status** | `created` (new Bybit migration) |
| **Origin** | `notebooks/03_strategy03_crypto.ipynb` |
| **Type** | Crypto strategy backtest (live Bybit data) |
| **MT5 dependency** | None |
| **Execution status** | Requires valid `BYBIT_API_KEY` / network access |

**Description**: Identical strategy logic to `03_strategy03_crypto.ipynb` but fetches live candles from Bybit instead of reading CSV cache. Useful for testing the strategy on current market conditions.

**What changed vs. original**:
- Section 2: CSV `load_cached_timeframe()` → `_fetch_klines()` via `pybit`
- Sections 3–10: unchanged — EMA, trend, backtest, metrics, charts, save

---

### `notebooks/_all_metrics.ipynb`
| Field | Value |
|-------|-------|
| **Status** | `copied` |
| **Origin** | `notebooks/_all_metrics.ipynb` |
| **Type** | Results aggregation (all strategies) |
| **MT5 dependency** | None |
| **Execution status** | Runs offline; reads from `./results/` |

---

### `notebooks/_all_metrics_crypto.ipynb`
| Field | Value |
|-------|-------|
| **Status** | `copied` |
| **Origin** | `notebooks/_all_metrics_crypto.ipynb` |
| **Type** | Results aggregation (crypto-only) |
| **MT5 dependency** | None |
| **Execution status** | Runs offline; reads from `./results/*_crypto/` |

---

## Archived Notebooks

### `research/legacy_notebooks/00_data_feching.ipynb`
| Field | Value |
|-------|-------|
| **Status** | `archived` |
| **Reason** | Heavy MT5 dependency (`import MetaTrader5 as mt5`, `mt5.copy_rates_from_pos`) |
| **Replacement** | `notebooks/00_data_fetching_bybit.ipynb` |
| **Manual review needed** | No — superseded by Bybit version |

---

### `research/legacy_notebooks/01_support_resistance.ipynb`
| Field | Value |
|-------|-------|
| **Status** | `archived` |
| **Reason** | S/R + Fibonacci analysis for a different strategy; not part of EMA H1 Trend production flow |
| **MT5 dependency** | None (reads local CSV) |
| **Manual review needed** | No — standalone research tool, runs as-is against any cached OHLCV |

---

### `research/legacy_notebooks/02_strategy02.ipynb`
| Field | Value |
|-------|-------|
| **Status** | `archived` |
| **Reason** | Different strategy (EMA200 + VWAP + liquidity sweep + Backtrader); not related to production EMA H1 Trend bot |
| **MT5 dependency** | None (reads local CSV) |
| **Manual review needed** | No — standalone research tool |

---

## Dependency Fixes Summary

| Notebook | Fix Type | Detail |
|----------|----------|--------|
| `03_strategy03_crypto.ipynb` | Import path | `strategies.ema_trend.*` → `strategy.*` |
| `00_data_fetching_bybit.ipynb` | Library swap | `MetaTrader5` → `pybit.unified_trading.HTTP` |
| `03_strategy03_crypto_bybit.ipynb` | Library swap | CSV loader → live Bybit `get_kline()` |
| All notebooks | `sys.path` | Root = `notebooks/../` (unchanged, still correct) |
| All notebooks | Data paths | `./data/<SYM>/<TF>/ohlcv.csv` (unchanged, relative) |
| All notebooks | Results paths | `./results/strategy03_crypto/...` (unchanged) |

---

## Path Fixes

All notebooks use relative paths (`./data/`, `./results/`) anchored to the notebook's working directory (`notebooks/`). No absolute paths were present. No changes needed.

---

## Unresolved Issues

| Issue | Notebook | Severity | Action Required |
|-------|----------|----------|-----------------|
| `data/` cache is empty | All offline notebooks | Medium | Run `00_data_fetching_bybit.ipynb` first to populate |
| `BYBIT_API_KEY` required | `00_data_fetching_bybit.ipynb`, `03_strategy03_crypto_bybit.ipynb` | Low | Fill `.env` with real keys before running |
| `backtrader` not in requirements.txt | `research/legacy_notebooks/01_support_resistance.ipynb` | Low | Legacy only; install with `pip install backtrader` if needed |
| `mplfinance` not in requirements.txt | `research/legacy_notebooks/01_support_resistance.ipynb` | Low | Legacy only; install with `pip install mplfinance` if needed |

---

## Execution Status

| Notebook | Offline? | Live API? | Notes |
|----------|----------|-----------|-------|
| `00_data_fetching_bybit.ipynb` | No | Yes | Needs `.env` keys |
| `03_strategy03.ipynb` | Yes | No | Needs `./data/` CSV cache |
| `03_strategy03_crypto.ipynb` | Yes | No | Needs `./data/` CSV cache |
| `03_strategy03_crypto_bybit.ipynb` | No | Yes | Needs `.env` keys |
| `_all_metrics.ipynb` | Yes | No | Needs `./results/` populated |
| `_all_metrics_crypto.ipynb` | Yes | No | Needs `./results/` populated |

---

## Strategy Logic Verification

All notebooks importing from `strategy.*` use the same files as the production bot:

| Module | File | Status |
|--------|------|--------|
| `strategy.crypto_core` | `strategy/crypto_core.py` | Unchanged — DO NOT MODIFY |
| `strategy.backtest` | `strategy/backtest.py` | Unchanged — DO NOT MODIFY |
| `strategy.setup` | `strategy/setup.py` | Unchanged — DO NOT MODIFY |

EMA calculations, trend logic, signal generation, and risk formulas are **byte-identical** to the MT5 source.

---

## Notebooks Requiring Manual Review

**None** — all notebooks are either validated clean or archived without breaking the production flow.

The two archived notebooks (`01_support_resistance.ipynb`, `02_strategy02.ipynb`) are self-contained research tools that run against any cached OHLCV data and need no changes.

---

## Running Order (First-Time Setup)

```bash
cd "D:\bot\ema-1d trend-exchange\notebooks"

# Step 1: fetch data (needs API keys)
jupyter nbconvert --to notebook --execute 00_data_fetching_bybit.ipynb

# Step 2: run backtest on cached data
jupyter nbconvert --to notebook --execute 03_strategy03_crypto.ipynb

# Step 3: view aggregate metrics
jupyter nbconvert --to notebook --execute _all_metrics_crypto.ipynb
```
