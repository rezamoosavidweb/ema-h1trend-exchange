# Bybit Multi-Symbol Scalper

Live execution of the per-symbol winner strategies discovered by the sweep
notebooks ([25_crypto_strategy_sweep_bybit](../notebooks/25_crypto_strategy_sweep_bybit.ipynb)
→ [26_top_strategy_per_symbol_bybit](../notebooks/26_top_strategy_per_symbol_bybit.ipynb)
→ [28_winner_strategies_summary_bybit](../notebooks/28_winner_strategies_summary_bybit.ipynb)).

## Files

| Path | Role |
| --- | --- |
| [run_multi_scalper_bybit.py](run_multi_scalper_bybit.py) | The actual bot (async, single-process). |
| [../run_multi_scalper_bybit_loop.bat](../run_multi_scalper_bybit_loop.bat) | Forever-loop wrapper for Windows (60 s restart backoff). |
| [../notebooks/29_live_trades_replay_bybit.ipynb](../notebooks/29_live_trades_replay_bybit.ipynb) | Compare live signals/orders/closes against a replay backtest on the same window. |
| [../telegram_bot/telethon_notifier.py](../telegram_bot/telethon_notifier.py) | Telethon-based Telegram sender (SSL widened to certifi bundle). |

## What it does — one M5 cycle

For every winner symbol in `notebooks/results/_top_per_symbol/<SYM>/config.json`:

1. Fetch fresh M5 + HTF (and D1 if needed) klines from Bybit REST.
2. Run the symbol's *exact* winner strategy function from
   [`_strategy_lib.py`](../notebooks/_strategy_lib.py) — same code as backtest.
3. Read off the last closed bar's `signal`, `sl_price` (and `tp_price` for
   mean-revert strategies). Log every gate value into the diagnostics field.
4. Dedup against `logs/bybit_bot/_seen_signals/<SYM>.json` so we don't trade
   the same bar twice after a restart.
5. Reconcile: if a tracked position has disappeared, emit a `position_closed`
   event (with realised PnL from `get_closed_pnl`) and Telegram the operator.
6. On a fresh signal and no open position: size `qty = risk_usdt / abs(entry-SL)`,
   round to the instrument's `qty_step`, place a **market order** with SL/TP
   attached, and Telegram the entry.
7. Append every step to the per-symbol JSONL log.

After the cycle the bot sleeps until the next M5 boundary + 1 second.

## Logs (everything you need to audit)

```
logs/bybit_bot/
├── _portfolio.jsonl                  ← bot start/stop, cycle summaries
├── _seen_signals/<SYM>.json          ← dedup state (survives restarts)
├── _state/<SYM>.json                 ← last-known open position
└── <SYM>-YYYY-MM-DD.jsonl            ← per-symbol per-day event stream
```

Per-symbol log lines (`bot_start`, `cycle`, `signal`, `market_order_placed`,
`position_closed`, `skip`, …) carry the strategy name, RR, params, tick/qty
constraints, the full bar diagnostics, and the `run_id`. Replay notebook 29
parses these lines directly.

## How to launch

### One-off cycle (safe smoke test, no orders)

```bash
python app/run_multi_scalper_bybit.py --once --dry-run --no-telegram
```

### Two symbols, real loop, demo account

```bash
python app/run_multi_scalper_bybit.py --demo --symbols SOLUSDT XLMUSDT
```

### Production loop, full basket, real account

Edit [`run_multi_scalper_bybit_loop.bat`](../run_multi_scalper_bybit_loop.bat)
and set the `RUN_ARGS` line (e.g. `set RUN_ARGS=--risk-usdt 10`). Then
double-click or `cmd /k run_multi_scalper_bybit_loop.bat`. The wrapper restarts
the Python runner on crash with a 60 s backoff.

`Ctrl+C` in the cmd window is a clean shutdown — open positions stay open and
will hit their broker-side SL/TP independently.

## CLI flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--symbols X Y …` | every profitable winner | Restrict basket. |
| `--once` | off | One cycle then exit. |
| `--dry-run` | off | Detect + log + telegram, do not place orders. |
| `--testnet` | off | Use `api-testnet.bybit.com`. |
| `--demo` | off | Use `api-demo.bybit.com` (paper). |
| `--no-telegram` | off | Disable Telegram entirely. |
| `--include-unprofitable` | off | Also run `wr_target_only` symbols (BTC / XAU). |
| `--risk-usdt N` | 20 | Per-trade risk in USDT. |
| `--log-level` | INFO | DEBUG / INFO / WARNING / ERROR. |

## Env (`.env`)

```
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
BYBIT_TESTNET=false
BYBIT_DEMO=false
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_PHONE=+989...
RISK_FIXED_USDT=20
```

## Telegram SSL fix

Telethon trusts the system CA store by default, which on un-updated Windows
machines rejects the modern intermediate cert Telegram serves. The bot points
OpenSSL at the certifi bundle *before* importing telethon (see the top of
`run_multi_scalper_bybit.py`) **and** the notifier itself builds an
`ssl.SSLContext` from certifi and passes it via the `ssl=` kwarg to
`TelegramClient`. If you still see `SSLCertVerificationError`:

```
pip install --upgrade certifi
```

then restart the bot.

## Live vs backtest parity

Both paths import the same strategy function from `notebooks/_strategy_lib.py`.
Notebook 29 takes the bot's log lines + the symbol's CSV history + the symbol's
winner config, runs the backtest on the *same time window*, and emits:

- `replay_vs_live_diff_<from>_<to>.csv` — every signal labelled
  `matched / live_only / replay_only`.
- `replay_summary_<from>_<to>.csv` — per-symbol parity scoreboard.

Drilldown section in the notebook shows the live `cycle` event's diagnostics
around the mismatched bar, so the disagreeing gate (RSI / ADX / h1_trend / …)
is visible immediately.
