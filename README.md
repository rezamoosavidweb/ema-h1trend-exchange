# EMA H1 Trend — Bybit Linear Futures Bot

Production-grade migration of the MT5 EMA H1 Trend + M5 swing strategy to **Bybit Linear Futures** (USDT-margined perpetuals).

> **Strategy logic is 100% identical to the MT5 version.**
> Only the execution layer changed: MT5 terminal → Bybit REST API via `pybit`.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Folder Reference](#folder-reference)
- [Configuration](#configuration)
- [Running the Bot](#running-the-bot)
- [Multi-Symbol Mode](#multi-symbol-mode)
- [Backtest Mode](#backtest-mode)
- [MT5 → Bybit Mapping](#mt5--bybit-mapping)
- [Order Lifecycle](#order-lifecycle)
- [Edge Cases Handled](#edge-cases-handled)
- [Testing](#testing)
- [Deployment](#deployment)
- [FAQ](#faq)

---

## How It Works

The strategy fires on every closed **M5 candle**:

```
Every 5 minutes:
  1. Fetch 500+ M5 candles  +  300+ H1 candles  from Bybit
  2. Add EMA-8 / EMA-13 / EMA-21 on both timeframes
  3. Merge H1 trend onto M5  (backward, no lookahead)
  4. Compute signal at the last closed M5 bar:
       H1 trend = "bull" → BUY_STOP above swing high + offset
       H1 trend = "bear" → SELL_STOP below swing low  - offset
       H1 trend = "flat" → no order
  5. Sync one conditional stop-entry order on Bybit:
       - No signal   → cancel existing pending (if any)
       - Signal expired (>60 min old) → cancel
       - New signal, no pending → create
       - Signal changed prices → modify
       - Signal changed side → cancel + recreate
       - Signal unchanged → do nothing
  6. Sleep until next M5 candle boundary
```

**Position sizing** (identical to MT5):
```
risk_cash     = balance × risk_per_trade       (e.g. 10000 × 0.01 = $100)
risk_per_unit = |entry_price − sl_price|        (price distance)
qty           = risk_cash / risk_per_unit        (in base asset: BTC, ETH, ...)
```

---

## Quick Start

### 1. Prerequisites

```
Python 3.11+
Bybit account with API key (read + trade permissions)
```

### 2. Install

```bash
cd "D:\bot\ema-1d trend-exchange"
pip install -r requirements.txt
```

### 3. Configure

```bash
copy .env.example .env
```

Open `.env` and fill in at minimum:

```env
BYBIT_API_KEY=your_key_here
BYBIT_API_SECRET=your_secret_here
SYMBOL=BTCUSDT
```

### 4. Test with dry-run first

```bash
python app/main.py --symbol BTCUSDT --dry-run
```

Signals are generated and logged but **no orders are placed**.

### 5. Go live

```bash
python app/main.py --symbol BTCUSDT
```

---

## Project Structure

```
ema-1d trend-exchange/
│
├── app/                        ← Application entrypoint
│   └── main.py                 ← CLI parser + asyncio loop startup
│
├── core/                       ← Shared constants & exceptions
│   ├── constants.py            ← EMA periods, default params, Bybit strings
│   └── exceptions.py           ← Typed exception hierarchy
│
├── config/                     ← Configuration system
│   └── settings.py             ← Pydantic Settings (reads .env)
│
├── models/                     ← Pure data containers (no IO)
│   └── order.py                ← Signal, PendingOrder, Position,
│                                   InstrumentInfo, WalletBalance
├── exchange/                   ← Bybit API layer
│   ├── bybit_client.py         ← Async REST client (retry + backoff)
│   ├── precision.py            ← tickSize / qtyStep normalization
│   └── bybit_ws.py             ← WebSocket scaffold (future real-time feeds)
│
├── strategy/                   ← ⚠️  NEVER MODIFY — identical to MT5
│   ├── crypto_core.py          ← EMA, trend, compute_pending_setup()
│   ├── setup.py                ← list_setup_signals(), walk-forward sim
│   └── backtest.py             ← run_backtest() engine
│
├── market/                     ← Market data
│   └── data_fetcher.py         ← Bybit kline → pandas DataFrame
│
├── risk/                       ← Position sizing
│   └── sizing.py               ← compute_qty(), margin check
│
├── state/                      ← Runtime state (in-memory only)
│   └── bot_state.py            ← Candle guard, pending tracking, orderLinkId
│
├── execution/                  ← Order execution engine
│   ├── order_manager.py        ← create / modify / cancel conditional orders
│   └── reconciler.py           ← Startup recovery + per-cycle sync
│
├── services/                   ← Orchestration
│   ├── trading_service.py      ← Main async loop (one symbol)
│   └── multi_symbol_runner.py  ← Run multiple symbols concurrently
│
├── storage/                    ← Persistence
│   └── signal_log.py           ← Append-only CSV audit log
│
├── telemetry/                  ← Observability
│   └── logging.py              ← UTC timestamps, JSON output option
│
├── tests/                      ← Unit tests (no exchange needed)
│   ├── conftest.py             ← Shared fixtures
│   ├── test_precision.py       ← Tick/qty rounding
│   ├── test_strategy_parity.py ← EMA + signal math (parity with MT5)
│   ├── test_risk_sizing.py     ← Risk formula
│   └── test_bot_state.py       ← Candle guard + expiry logic
│
├── scripts/
│   └── run.py                  ← Alternative entrypoint (same as app/main.py)
│
├── .env.example                ← Copy to .env and fill in your keys
├── requirements.txt
└── pyproject.toml
```

---

## Folder Reference

### `app/` — Entry Point

**`main.py`** — The only file you run directly.

- Parses CLI arguments
- Pushes overrides into environment variables
- Calls `configure_logging()`
- Creates `TradingService` (single) or `MultiSymbolRunner` (multiple)
- Installs `SIGINT` / `SIGTERM` handlers for graceful shutdown
- Starts the `asyncio` event loop

```bash
python app/main.py --symbol BTCUSDT --risk 0.01 --leverage 2
```

---

### `core/` — Shared Foundations

**`constants.py`** — Single source of truth for all magic numbers.

| Constant | Value | Meaning |
|---|---|---|
| `EMA_FAST` | 8 | Fast EMA period |
| `EMA_MID` | 13 | Mid EMA period |
| `EMA_SLOW` | 21 | Slow EMA period |
| `MIN_WARMUP_BARS_M5` | 200 | Minimum M5 bars before signals are valid |
| `MIN_WARMUP_BARS_H1` | 200 | Minimum H1 bars before EMAs are stable |
| `DEFAULT_LOOKBACK_BARS` | 5 | Swing window (HH / LL lookback) |
| `DEFAULT_PENDING_EXPIRY_MIN` | 60 | Minutes before a pending order expires |
| `DEFAULT_RR` | 1.0 | TP = 1× the SL distance |
| `BYBIT_CATEGORY` | `"linear"` | USDT-margined perpetuals |

**`exceptions.py`** — Every error has a specific type:

```
TradingBotError
├── ExchangeError
│   ├── ExchangeConnectionError   ← network timeout / DNS
│   ├── ExchangeAuthError         ← bad API key
│   ├── ExchangeRateLimitError    ← 429 / retCode 10006
│   ├── ExchangeMaintenanceError  ← Bybit maintenance window
│   ├── OrderError
│   │   ├── OrderNotFoundError    ← orderLinkId not on exchange
│   │   ├── InsufficientMarginError
│   │   ├── InvalidPriceError     ← price not on tick grid
│   │   └── InvalidQtyError       ← below minOrderQty
│   └── DataError
│       ├── InsufficientDataError ← not enough bars for warmup
│       └── StaleDataError        ← feed gap detected
└── ConfigError
```

---

### `config/` — Settings

**`settings.py`** — All configuration in one place via `pydantic_settings`.

Values are read in this priority order:
1. CLI arguments (pushed to env in `app/main.py`)
2. Environment variables
3. `.env` file
4. Defaults in the code

| Setting | Env Var | Default | Description |
|---|---|---|---|
| `bybit_api_key` | `BYBIT_API_KEY` | *required* | API key |
| `bybit_api_secret` | `BYBIT_API_SECRET` | *required* | API secret |
| `bybit_testnet` | `BYBIT_TESTNET` | `false` | Use testnet |
| `symbol` | `SYMBOL` | `BTCUSDT` | Trading symbol |
| `risk_per_trade` | `RISK_PER_TRADE` | `0.01` | 1% risk per trade |
| `leverage` | `LEVERAGE` | `1` | Leverage (1 = no leverage) |
| `rr` | `RR` | `1.0` | Reward:risk ratio |
| `pending_expiry_min` | `PENDING_EXPIRY_MIN` | `60` | Order TTL in minutes |
| `lookback_bars` | `LOOKBACK_BARS` | `5` | Swing window size |
| `pending_offset_ticks` | `PENDING_OFFSET_TICKS` | `3.0` | Ticks beyond swing boundary |
| `dry_run` | `DRY_RUN` | `false` | Log only, no real orders |
| `log_level` | `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING |
| `signal_log_csv` | `SIGNAL_LOG_CSV` | *(off)* | Path to audit CSV |

**Auto-derived values** (computed, not set):
- `magic` — stable hash of symbol name (ensures parallel instances don't collide)
- `pip_size` — from symbol prefix: BTC=1.0, ETH=0.01, XRP=0.0001, etc.

---

### `models/` — Data Containers

**`order.py`** — Pure Python dataclasses, no IO.

| Class | Purpose |
|---|---|
| `Signal` | Output of `compute_pending_setup()`: side, entry, sl, tp, qty |
| `PendingOrder` | One live conditional order tracked in memory |
| `Position` | Live Bybit position snapshot |
| `InstrumentInfo` | tickSize, qtyStep, minQty, maxQty for one symbol |
| `WalletBalance` | USDT equity, available balance, used margin |
| `Side` | Enum: `BUY` / `SELL` with `.bybit()` → `"Buy"` / `"Sell"` |
| `TriggerDirection` | `RISES_TO=1` (BUY_STOP) / `FALLS_TO=2` (SELL_STOP) |

---

### `exchange/` — Bybit API Layer

**`bybit_client.py`** — All API calls go through here.

Every method is `async`. The synchronous `pybit` calls run in `asyncio.to_thread()`.
Retry with exponential backoff is built in — you never need to retry manually.

Key methods:

| Method | Replaces MT5 |
|---|---|
| `get_instrument_info(symbol)` | `mt5.symbol_info()` |
| `get_kline(symbol, interval, limit)` | `mt5.copy_rates_from_pos()` |
| `get_balance()` | `mt5.account_info().balance` |
| `get_positions(symbol)` | `mt5.positions_get()` |
| `get_open_stop_orders(symbol)` | `mt5.orders_get()` |
| `place_conditional_order(...)` | `mt5.order_send(TRADE_ACTION_PENDING)` |
| `amend_conditional_order(...)` | `mt5.order_send(TRADE_ACTION_MODIFY)` |
| `cancel_order(symbol, link_id)` | `mt5.order_send(TRADE_ACTION_REMOVE)` |
| `set_leverage(symbol, n)` | MT5 broker setting |

**`precision.py`** — Prevents `"invalid price"` and `"invalid qty"` rejections.

```python
# Snap raw signal prices to Bybit tick grid
entry, sl, tp = snap_signal_to_ticks("buy", raw_entry, raw_sl, raw_tp, info)

# Normalize qty to qtyStep (always floors — never rounds up)
qty = normalize_qty(raw_qty, info)

# Format for API call (correct decimal places)
price_str = price_to_str(entry, info.tick_size)   # e.g. "50000.5"
qty_str   = qty_to_str(qty, info)                  # e.g. "0.001"
```

**`bybit_ws.py`** — WebSocket scaffold. Currently a stub — the bot uses REST polling.
Extend this file when real-time feeds are needed (reduces latency from ~5min to ~0).

---

### `strategy/` — ⚠️ DO NOT MODIFY

These three files are **character-for-character identical** to the MT5 source.
Any change here changes trading behavior.

**`crypto_core.py`**
- `add_emas(df)` — adds `ema_8`, `ema_13`, `ema_21` columns
- `h1_trend_series(h1)` — `"bull"` / `"bear"` / `"flat"` per H1 bar
- `merge_h1_trend_onto_m5(m5, h1)` — backward `merge_asof`, no lookahead
- `compute_pending_setup(m5_ctx, bar_index, ...)` — the core signal function

**`setup.py`**
- `list_setup_signals(data, ...)` — every bar where a signal would be placed
- `_simulate_walk_forward(data, ...)` — full backtest simulation with fills

**`backtest.py`**
- `run_backtest(data, ...)` — walk-forward engine used by `--backtest` CLI

---

### `market/` — Data Fetching

**`data_fetcher.py`** — Fetches and prepares OHLCV for the strategy.

```
Bybit kline API (newest-first)
    → reverse to chronological
    → set UTC DatetimeIndex
    → drop forming (live) candle   ← critical: strategy needs CLOSED bars only
    → validate min bars (≥200 for EMA warmup)
    → check for staleness (warn if last bar is old)
```

Output is a standard `pandas.DataFrame` with columns `open, high, low, close, volume`
and a **UTC DatetimeIndex** — identical to what the MT5 source produced.

---

### `risk/` — Position Sizing

**`sizing.py`** — The exact same formula as MT5:

```python
risk_cash     = balance * risk_per_trade
risk_per_unit = abs(entry - sl)
raw_qty       = risk_cash / risk_per_unit

# Then normalize:
qty = normalize_qty(raw_qty, info)   # floor to qtyStep, clamp to [min, max]
```

Also includes `check_margin_available()` — warns before sending an order that
would be rejected for insufficient margin.

---

### `state/` — Runtime State

**`bot_state.py`** — Everything the bot remembers between cycles.

| Property | Purpose |
|---|---|
| `last_processed_candle` | Duplicate candle guard (restart-safe) |
| `pending` | The single `PendingOrder` currently tracked |
| `startup_recovery_done` | Has reconciler run at startup? |

Key methods:

```python
state.is_duplicate_candle(ts)            # True if we already processed this bar
state.mark_candle_processed(ts)          # Record bar as done
state.pending_is_expired(expiry_min)     # True if signal bar is too old
state.pending_matches_signal(...)        # True if prices unchanged (no modify needed)
state.pending_side_matches(side)         # True if side unchanged
```

`make_order_link_id(symbol, magic, side, signal_bar_time)` — generates the
`orderLinkId` sent to Bybit. It is **deterministic** — same inputs → same ID,
so if the bot restarts and re-submits the same signal, Bybit deduplicates it.

---

### `execution/` — Order Execution Engine

**`order_manager.py`** — Direct replacement for MT5 order functions.

The `sync_pending_orders()` method mirrors `sync_pending_orders()` from the MT5 `main.py`:

```
sync_pending_orders(has_position, signals_df, m5_ctx, balance, ...):

  if has_position:
      return                           ← already in a trade, do nothing

  if no signals:
      return                           ← nothing to do

  last_signal = signals_df.iloc[-1]
  bars_passed = (current_time - signal_time) / tf_minutes

  if bars_passed >= expiry_bars:       ← signal too old
      cancel pending if exists
      return

  compute qty from live balance

  if no pending:
      create_pending_order()           ← MT5: TRADE_ACTION_PENDING BUY_STOP/SELL_STOP

  elif pending.matches(signal):
      do nothing                       ← MT5: no-op

  elif pending.side != signal.side:
      cancel + recreate                ← MT5: REMOVE + PENDING (type cannot be modified)

  else:
      modify_pending_order()           ← MT5: TRADE_ACTION_MODIFY
      if modify fails:
          cancel + recreate
```

**`reconciler.py`** — Exchange ↔ local state synchronization.

- **Startup** (`reconcile_on_startup`):
  Scans Bybit for any orders from a previous session matching the `ema-` prefix.
  Adopts the most recent one. Cancels duplicates. Logs open positions.

- **Per-cycle** (`reconcile_cycle`):
  Checks if the tracked pending still exists on exchange.
  If it was filled, cancelled, or triggered externally → clears local state.

---

### `services/` — Orchestration

**`trading_service.py`** — The main async loop for one symbol.

```
TradingService.run():
    _initialize()                      ← instrument info, leverage, position mode, recovery
    _loop():
        while running:
            peek latest candle time
            if duplicate → sleep → continue

            _run_cycle():
                fetch M5 + H1 data
                build_context()        ← add EMAs + merge H1 trend
                list_setup_signals()   ← generate signals
                reconcile_cycle()      ← sync with exchange state
                get_balance()
                has_open_position()
                sync_pending_orders()  ← create / modify / cancel

            mark candle processed
            sleep until next M5 boundary
```

**`multi_symbol_runner.py`** — Runs multiple `TradingService` instances concurrently
using `asyncio.gather()`. Each symbol is independent — different state, different orders.

---

### `storage/` — Persistence

**`signal_log.py`** — Appends one CSV row per signal the strategy generates.

This is equivalent to `strategy03_signals.csv` from the MT5 version.
Use it to compare signal history between MT5 and Bybit — they should be identical
for the same market data.

Columns: `logged_at_utc`, `symbol`, `side`, `model_entry`, `model_sl`, `model_tp`,
`adjusted_entry`, `normalized_qty`, `balance`, `action`, `order_link_id`, ...

Enable via `.env`:
```env
SIGNAL_LOG_CSV=logs/signals.csv
```

---

### `telemetry/` — Logging

**`logging.py`** — Sets up logging for the whole process.

Always UTC timestamps. Two output formats:

```bash
# Human-readable (default)
2024-01-15 08:35:00 UTC | INFO     | services.trading_service | CYCLE #42 | BTCUSDT

# JSON (for Loki / ELK / Datadog)
{"ts":"2024-01-15T08:35:00.123Z","level":"INFO","logger":"services.trading_service","msg":"CYCLE #42 | BTCUSDT"}
```

Enable JSON: `LOG_JSON=true` in `.env` or `--log-json` on CLI.

---

### `tests/` — Unit Tests

Tests have **zero external dependencies** — no exchange connection, no real data.
All tests use synthetic OHLCV data generated in fixtures.

| File | What it tests |
|---|---|
| `test_strategy_parity.py` | EMA math, trend detection, signal geometry, risk formula — verifies parity with MT5 |
| `test_precision.py` | Tick rounding (nearest / up / down), qty floor, string formatting |
| `test_risk_sizing.py` | `compute_qty` formula matches MT5, qty validation |
| `test_bot_state.py` | Candle guard, pending tracking, expiry, orderLinkId determinism |

```bash
pytest tests/ -v                 # run all tests
pytest tests/test_strategy_parity.py -v   # run one file
pytest tests/ -k "test_buy"     # run by name pattern
```

---

## Configuration

### `.env` file (full reference)

```env
# ── Required ────────────────────────────────────────────────────────
BYBIT_API_KEY=abc123
BYBIT_API_SECRET=xyz789
BYBIT_TESTNET=false            # true = testnet

# ── Symbol ──────────────────────────────────────────────────────────
SYMBOL=BTCUSDT                 # or ETHUSDT, SOLUSDT, etc.

# ── Strategy (match your MT5 settings exactly) ───────────────────────
RISK_PER_TRADE=0.01            # 1% of account balance
LEVERAGE=1                     # 1x = full collateral, no leverage
RR=1.0                         # TP distance = 1× SL distance
LOOKBACK_BARS=5                # bars in swing window
PENDING_OFFSET_TICKS=3.0       # ticks beyond swing high/low
PENDING_EXPIRY_MIN=60          # cancel pending after 60 minutes

# ── Execution ────────────────────────────────────────────────────────
DRY_RUN=false                  # true = no real orders
POSITION_MODE=one_way          # one_way | hedge

# ── Logging ──────────────────────────────────────────────────────────
LOG_LEVEL=INFO                 # DEBUG | INFO | WARNING | ERROR
LOG_JSON=false
LOG_FILE=logs/bot.log          # optional file output

# ── Audit ────────────────────────────────────────────────────────────
SIGNAL_LOG_CSV=logs/signals.csv
```

---

## Running the Bot

### Single symbol

```bash
python app/main.py --symbol BTCUSDT
```

### With custom risk

```bash
python app/main.py --symbol ETHUSDT --risk 0.005 --leverage 2
```

### Dry-run (no real orders)

```bash
python app/main.py --symbol BTCUSDT --dry-run --log-level DEBUG
```

### On Bybit testnet

```bash
python app/main.py --symbol BTCUSDT --testnet --dry-run
```

### Write logs to file

```bash
python app/main.py --symbol BTCUSDT --log-file logs/btc.log
```

### Keep signal audit log

```bash
python app/main.py --symbol BTCUSDT --signal-log logs/signals.csv
```

---

## Multi-Symbol Mode

Run multiple symbols in the **same process**, same event loop:

```bash
python app/main.py --symbols BTCUSDT,ETHUSDT,SOLUSDT
```

Each symbol has its own:
- Independent candle timing
- Independent pending order
- Independent state (no cross-contamination)
- Auto-derived magic number (no collision)

Or run separate processes (recommended for production):

```bash
# Terminal 1
python app/main.py --symbol BTCUSDT

# Terminal 2
python app/main.py --symbol ETHUSDT

# Terminal 3
python app/main.py --symbol SOLUSDT
```

---

## Backtest Mode

Run the **exact same walk-forward engine** as the MT5 version, but using live Bybit data:

```bash
python app/main.py --backtest --symbol BTCUSDT --backtest-bars 1000
```

```bash
python app/main.py --backtest \
    --symbol ETHUSDT \
    --backtest-bars 2000 \
    --risk 0.01 \
    --start-balance 10000 \
    --backtest-out results/eth/
```

Output:
```
=======================================================
Backtest Results | ETHUSDT
=======================================================
Trades:        47
Win rate:      53.2%
Net PnL:       812.34 USDT
Profit factor: 1.247
Start balance: 10000.00
End balance:   10812.34
Return:        8.12%
=======================================================
```

Saves `results/eth/trades.csv` and `results/eth/metrics.csv`.

---

## MT5 → Bybit Mapping

| MT5 Concept | Bybit Equivalent |
|---|---|
| `ORDER_TYPE_BUY_STOP` | `place_order(side="Buy", triggerDirection=1)` |
| `ORDER_TYPE_SELL_STOP` | `place_order(side="Sell", triggerDirection=2)` |
| `TRADE_ACTION_PENDING` | `place_order(triggerPrice=..., orderType="Limit")` |
| `TRADE_ACTION_MODIFY` | `amend_order(orderLinkId=...)` |
| `TRADE_ACTION_REMOVE` | `cancel_order(orderLinkId=...)` |
| `ORDER_TIME_GTC` | `timeInForce="GTC"` |
| `ORDER_TIME_SPECIFIED` + expiry | Manual tracking → cancel when `bars_passed ≥ expiry_bars` |
| Magic number | `orderLinkId` prefix: `ema-{magic}-{side}-{unix_ts}` |
| `mt5.positions_get()` | `get_positions(category="linear")` |
| `mt5.orders_get()` | `get_open_orders(orderFilter="StopOrder")` |
| `symbol_info.volume_step` | `lotSizeFilter.qtyStep` |
| `symbol_info.trade_tick_size` | `priceFilter.tickSize` |
| `symbol_info.volume_min/max` | `lotSizeFilter.minOrderQty / maxOrderQty` |
| `mt5.account_info().balance` | `get_wallet_balance()` → USDT equity |
| `snap_price_to_tick()` | `normalize_price(mode="up"/"down"/"nearest")` |
| `normalize_volume()` | `normalize_qty()` (always floors — same risk) |
| `ORDER_FILLING_IOC/FOK` | Not needed — Bybit limit stop orders are GTC |

---

## Order Lifecycle

```
Signal bar closes (M5)
        │
        ▼
compute_pending_setup()
  returns: side, entry, sl, tp, qty
        │
        ▼
snap prices to tickSize grid
normalize qty to qtyStep (floor)
validate geometry: sl < entry < tp (buy) or tp < entry < sl (sell)
        │
        ▼
place_order(
  category="linear",
  symbol="BTCUSDT",
  side="Buy",
  orderType="Limit",
  qty="0.001",
  price="50000.5",            ← limit price = trigger price
  triggerPrice="50000.5",
  triggerBy="LastPrice",
  triggerDirection=1,         ← rises to (BUY_STOP equivalent)
  timeInForce="GTC",
  orderLinkId="ema-20260510-b-1705312200",
  stopLoss="49000.0",         ← SL attached at creation
  takeProfit="51000.0",       ← TP attached at creation
  positionIdx=0,              ← one-way mode
)
        │
        ├── On trigger: Bybit places limit buy at triggerPrice
        │       → position opens
        │       → SL and TP activate automatically
        │
        └── On expiry (bars_passed ≥ 12):
                → bot calls cancel_order(orderLinkId=...)
```

---

## Edge Cases Handled

| Scenario | Handling |
|---|---|
| Bot restarts during open position | Reconciler detects position → loop skips order sync |
| Bot restarts during pending order | Reconciler adopts orphan order into state |
| Two pending orders (race condition) | Reconciler cancels all but the most recent |
| Filled order (bot was offline) | `reconcile_cycle()` detects missing order → clears state |
| Triggered but not filled | Bybit places limit order → eventually fills or times out |
| Price snapped past signal entry | Stale signal check (tp_dist < 50% of original sl_dist) → skip |
| Insufficient margin | `InsufficientMarginError` caught → logs error, skips cycle |
| Rate limit hit | Auto-sleep + exponential backoff (up to 60s) |
| Exchange maintenance | `ExchangeMaintenanceError` → 30s sleep + retry |
| Network timeout | `ExchangeConnectionError` → retry up to `MAX_RETRIES` times |
| Stale candles (feed gap) | Warning logged, strategy continues with available data |
| Duplicate candle processing | `BotState.is_duplicate_candle()` guard |
| Same signal, prices unchanged | `pending_matches_signal()` → no API call |
| Side flip (bull→bear) | Cancel existing + recreate opposite side |
| Invalid tick grid price | `InvalidPriceError` → logged, order skipped |
| Qty below broker minimum | `InvalidQtyError` → logged, order skipped |
| Leverage not set | `set_leverage()` at startup (idempotent) |
| Position mode mismatch | `set_position_mode()` at startup (idempotent) |
| SIGINT / SIGTERM | Graceful shutdown, no orphan orders left |

---

## Testing

```bash
cd "D:\bot\ema-1d trend-exchange"

# Run all tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_strategy_parity.py -v

# Run tests matching a name
pytest tests/ -k "test_buy_geometry" -v

# Run with coverage (requires pytest-cov)
pip install pytest-cov
pytest tests/ --cov=. --cov-report=term-missing
```

**All tests are offline** — no Bybit connection, no real data needed.

The most important test file is `test_strategy_parity.py`.
If all tests there pass, the Bybit signals are mathematically identical to MT5.

---

## Deployment

### Windows (Task Scheduler)

Create a `.bat` file:

```batch
@echo off
cd /d "D:\bot\ema-1d trend-exchange"
python app\main.py --symbol BTCUSDT --log-file logs\btc.log
```

Schedule it as a startup task (run as administrator, user logged on or not).

### Windows Service (NSSM)

```bash
# Download nssm from https://nssm.cc
nssm install EmaBot "python" "D:\bot\ema-1d trend-exchange\app\main.py --symbol BTCUSDT"
nssm set EmaBot AppDirectory "D:\bot\ema-1d trend-exchange"
nssm set EmaBot AppEnvironmentExtra BYBIT_API_KEY=xxx BYBIT_API_SECRET=yyy
nssm start EmaBot
```

### Linux (systemd)

```ini
# /etc/systemd/system/ema-bot.service
[Unit]
Description=EMA H1 Trend Bybit Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/ema-bot
ExecStart=/opt/ema-bot/venv/bin/python app/main.py --symbol BTCUSDT
EnvironmentFile=/opt/ema-bot/.env
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable ema-bot
systemctl start ema-bot
systemctl status ema-bot
journalctl -u ema-bot -f
```

---

## FAQ

**Q: Why is `strategy/` marked "DO NOT MODIFY"?**
A: The strategy logic (EMA periods, trend rules, swing window, risk formula) must be
byte-for-byte identical to the MT5 source. Any change here means the Bybit bot
trades a different strategy than the MT5 backtest was validated against.

**Q: How do I change the strategy parameters?**
A: Via `.env` or CLI flags — `LOOKBACK_BARS`, `PENDING_OFFSET_TICKS`, `RR`, etc.
These are the same parameters you configured in MT5.

**Q: What leverage should I use?**
A: Start with `LEVERAGE=1`. The risk sizing formula already accounts for balance
correctly at 1x. Higher leverage increases margin efficiency but adds liquidation risk.

**Q: Why does the bot use REST polling instead of WebSocket?**
A: The strategy fires once per 5-minute candle. REST polling is sufficient and simpler.
`exchange/bybit_ws.py` is a scaffold ready for WebSocket integration if lower latency
is needed in future.

**Q: What is `orderLinkId`?**
A: Bybit's custom client order ID (max 36 chars). We set it to a deterministic hash
of `symbol + magic + side + signal_bar_unix_time`. This means if the bot crashes
and re-submits the same signal, Bybit deduplicates it — the order is only placed once.

**Q: How do I check if the bot is working?**
A: 1. Check logs — every cycle prints signal info, order actions, and balance.
2. Check Bybit → Orders → Conditional Orders for your symbol.
3. Check `SIGNAL_LOG_CSV` for a history of all signals generated.

**Q: Can I run Bybit and MT5 simultaneously on the same symbol?**
A: Not recommended — they will fight over the same position.
Use different symbols or disable one before enabling the other.
