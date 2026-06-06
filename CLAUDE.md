# CLAUDE.md — ema-h1trend-exchange

Live crypto trading bot on **Bybit** (USDT perpetuals, `category="linear"`). A
per-symbol "winner" strategy runs on a fixed schedule, places market orders with
exchange-side SL/TP, and streams order/position events to Telegram.

## How it runs

- **Entry point:** [app/run_multi_scalper_bybit.py](app/run_multi_scalper_bybit.py)
  (`amain`). Launched/looped by `run_multi_scalper_bybit_loop.bat`.
- **Config:** environment variables in `.env` (loaded via `load_dotenv` at startup).
  Per-symbol strategy params come from `notebooks/results/_top_per_symbol/<SYM>/config.json`.
- **Strategy (current):** `donchian_brkout`, base TF **H1**, **RR 2.5**, trade only
  in the H1-trend direction (so every trade is "aligned" by construction).
- **Restart to apply config changes:** stop the loop, restart `run_multi_scalper_bybit_loop.bat`.
  A `bot_run_started` event (with `run_id` + UTC timestamp + active config) is written
  to `logs/bybit_bot/_portfolio.jsonl` and announced on Telegram — this marks the exact
  activation time of any change.

## Logs (source of truth)

- **Per-symbol per-day:** `logs/bybit_bot/<SYMBOL>-<YYYY-MM-DD>.jsonl` — one JSON event
  per line. Event types: `cycle` (has `diag`), `signal`, `market_order_placed`,
  `position_closed`, `skip` (with `reason`), `balance`, `bot_start`.
- **WS journal:** `logs/bybit_bot/_ws_events-<date>.jsonl` — authoritative order/position
  stream from the private WebSocket.
- **Portfolio:** `logs/bybit_bot/_portfolio.jsonl` — `bot_run_started` / `bot_run_stopped`.
- **PnL caveat:** `position_closed` log lines understate results (a `nan`/`pnl=0` bug).
  For true realised PnL use `scripts/reconstruct_pnl.py` or the `_ws_events` journal.

## Analysis notebook

[notebooks/01_closed_trades_charts_bybit.ipynb](notebooks/01_closed_trades_charts_bybit.ipynb)
fetches closed trades + live state from Bybit:
- SECTION 6/7 — account PnL summary + last closed trades.
- SECTION 8 — **open positions & pending orders** (live), with SL/TP, current price,
  uPnL, →SL%/→TP% distance, planned RR, and each conditional order linked to its
  parent position.
- SECTION 9 — per-trade **SL%/TP% distances** (read from the bot logs, since the
  exchange's closed-PnL records don't store SL/TP) + "was the TP too far?" diagnosis.

## ATR regime filter  ⭐ (added 2026-06-06)

**What:** before opening a NEW position (only when flat), skip the entry if the base-TF
ATR sits in the bottom `MIN_ATR_PERCENTILE` of its own recent range (a low-volatility /
compression regime). Donchian breakouts fail in compression — in the 7-day demo sample,
trades taken in the bottom ATR regime won **~13%** vs **~55%** otherwise; filtering them
roughly **doubled** net PnL and cut max drawdown ~73% (in-sample, optimistic).

**Why ATR percentile and not "SL < 1%":** SL distance ≈ Donchian width ≈ a *proxy* for
low ATR (corr 0.94). Multi-dimensional analysis showed ATR percentile is the single
robust driver (corr +0.43, monotonic across thresholds); ADX is non-predictive/inverse,
and trend-alignment is constant (always true). So the rule is intentionally **one
condition**, not a fragile multi-condition combo.

**Config (`.env`):**
- `MIN_ATR_PERCENTILE` — threshold, e.g. `30`. **`0` disables the filter (rollback).**
- `ATR_PCTILE_LOOKBACK` — rolling window in base-TF bars (default `200`).

**Code:** `_atr_percentile()` computes the percentile (fail-open → returns `None` /
no filtering when ATR missing or < `ATR_PCTILE_MIN_BARS=50` bars). `detect_signal` writes
`diag["atr_pctile"]` into **every** `cycle` event (logged even when the filter is off, so
the regime is always recorded). The gate lives in `run_symbol_cycle` right after
`has_open` is computed. Module constants near `MAX_HOLD_BARS_BY_TF`.

**Rollback:** set `MIN_ATR_PERCENTILE=0` in `.env` and restart. No code change needed.

### How to verify the filter's effect later

Everything needed to reconstruct the counterfactual is in the logs:

1. **Activation time:** grep `logs/bybit_bot/_portfolio.jsonl` for `bot_run_started`
   where `min_atr_percentile > 0` — its `ts` is when the filter went live.
2. **Blocked trades (would-be entries):** `skip` events with `reason="low_vol_regime"`
   in `logs/bybit_bot/*.jsonl`. Each carries `bar_time, direction, entry, sl, tp,
   atr_pctile, min_atr_percentile` — enough to fetch klines from `bar_time` forward and
   simulate whether SL or TP would have hit first (counterfactual PnL).
   Paired `signal` events have `regime_blocked=true`.
3. **Regime context anytime:** `diag.atr_pctile` in every `cycle` event.
4. **Actual trades after activation:** `market_order_placed` events (and the WS journal)
   from the activation `ts` onward = what the filter *let through*.

To audit: "compare trades before vs after `<activation ts>`; for every
`skip: low_vol_regime` after it, simulate the would-be trade and sum the avoided PnL."

## Conventions

- All timestamps UTC. Money in USDT. Prices need per-symbol tick rounding
  (`normalize_price` / instrument `tick_size`).
- Closed-PnL `side` is the *closing* side (opposite of the position): `Buy`→was Short,
  `Sell`→was Long. Open-position `side` is the real direction (`Buy`=Long, `Sell`=Short).
- Don't commit/push unless asked. Never print or commit `.env` secrets.
