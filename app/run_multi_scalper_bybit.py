#!/usr/bin/env python3
"""
Multi-Symbol Crypto Scalper — Live Bybit Linear (USDT-perp)

Lights-up the per-symbol winner strategies discovered by the sweep notebooks
(25 / 26 / 28) on real Bybit market data. One process, all symbols, runs
forever until Ctrl+C.

╔═══════════════════════════════════════════════════════════════════════════╗
║                         ARCHITECTURE OVERVIEW                             ║
╠═══════════════════════════════════════════════════════════════════════════╣
║                                                                           ║
║  Per cycle (anchored to M5 bar close, every 300 s):                       ║
║    1. For each symbol in `notebooks/results/_top_per_symbol/<SYM>/`:      ║
║         a) Fetch fresh OHLCV (M5 + HTF) from Bybit REST                   ║
║         b) Run the symbol's winner strategy fn from _strategy_lib         ║
║         c) Snapshot the last closed bar's signal + every gate value       ║
║         d) Dedup via per-symbol seen_signals.json                         ║
║         e) Sync open Bybit positions; reconcile orders/states             ║
║         f) On NEW signal & no open position: place market order +         ║
║            attach SL/TP, log every step, telegram the entry               ║
║         g) On position close: log close event + telegram                  ║
║    2. Aggregate cycle summary line to logs/bybit_bot/_portfolio.jsonl     ║
║    3. Sleep until just after the next M5 boundary                         ║
║                                                                           ║
║  Per-symbol artefacts (production-aligned with backtest replay):          ║
║      logs/bybit_bot/<SYM>-YYYY-MM-DD.jsonl   one event per line           ║
║      logs/bybit_bot/_seen_signals/<SYM>.json  dedup persistence           ║
║      logs/bybit_bot/_state/<SYM>.json         last-known position state   ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝

Usage:
    python app/run_multi_scalper_bybit.py                    # loop, every symbol
    python app/run_multi_scalper_bybit.py --once             # one cycle then exit
    python app/run_multi_scalper_bybit.py --dry-run          # signals only, no orders
    python app/run_multi_scalper_bybit.py --symbols SOLUSDT XLMUSDT
    python app/run_multi_scalper_bybit.py --testnet          # use Bybit testnet
    python app/run_multi_scalper_bybit.py --no-telegram      # disable telegram
    python app/run_multi_scalper_bybit.py --risk-usdt 10     # override per-trade risk

Env (.env):
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET (true/false)
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE
    RISK_FIXED_USDT (default 20.0)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal as _signal
import ssl
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── Globally trust the certifi bundle so telethon TLS works on Windows.
# Older system stores reject Telegram's modern intermediate CA. Setting this
# BEFORE importing telethon ensures ssl.create_default_context() picks it up.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("SSL_CERT_DIR",  str(Path(certifi.where()).parent))
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Strategy library lives in notebooks/ — we add it to the path for the strategy
# functions referenced in each per-symbol config.json.
_NOTEBOOKS_DIR = _REPO_ROOT / "notebooks"
if str(_NOTEBOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_NOTEBOOKS_DIR))

from _strategy_lib import (
    strategy_trend_pullback, strategy_bb_revert_midline,
    strategy_rsi_extreme_reversal, strategy_donchian_breakout,
    strategy_macd_pullback, strategy_ichimoku, strategy_fib_pullback,
    strategy_sr_zone_bounce, strategy_vwap_reaction, strategy_ema_cross,
)

from exchange.bybit_client import BybitClient
from exchange.precision import normalize_qty, normalize_price, price_to_str
from models.order import InstrumentInfo, Position, Side

log = logging.getLogger("bybit_bot")


# ═══════════════════════════════════════════════════════════════════════════
# PATHS / CONFIG
# ═══════════════════════════════════════════════════════════════════════════
WINNERS_DIR    = _NOTEBOOKS_DIR / "results" / "_top_per_symbol"
LOG_DIR        = _REPO_ROOT / "logs" / "bybit_bot"
SEEN_DIR       = LOG_DIR / "_seen_signals"
STATE_DIR      = LOG_DIR / "_state"
PORTFOLIO_LOG  = LOG_DIR / "_portfolio.jsonl"

for d in (LOG_DIR, SEEN_DIR, STATE_DIR):
    d.mkdir(parents=True, exist_ok=True)

STRATEGY_FN: Dict[str, Callable] = {
    "trend_pullback":  strategy_trend_pullback,
    "bb_revert_mid":   strategy_bb_revert_midline,
    "rsi_extreme":     strategy_rsi_extreme_reversal,
    "donchian_brkout": strategy_donchian_breakout,
    "macd_pullback":   strategy_macd_pullback,
    "ichimoku":        strategy_ichimoku,
    "fib_pullback":    strategy_fib_pullback,
    "sr_zone_bounce":  strategy_sr_zone_bounce,
    "vwap_reaction":   strategy_vwap_reaction,
    "ema_cross":       strategy_ema_cross,
}
# Which strategies need daily-trend frame in addition to HTF
USE_DAILY: Dict[str, bool] = {
    "trend_pullback":  True,
    "bb_revert_mid":   False,
    "rsi_extreme":     False,
    "donchian_brkout": False,
    "macd_pullback":   False,
    "ichimoku":        False,
    "fib_pullback":    False,
    "sr_zone_bounce":  False,
    "vwap_reaction":   False,
    "ema_cross":       False,
}

# Bybit kline interval string for each notebook timeframe label
BYBIT_INTERVAL = {"M1": "1", "M5": "5", "M15": "15", "M30": "30",
                  "H1": "60", "H4": "240", "D1": "D"}
# Bars to fetch per request — enough for warmup of ATR/EMA/Donchian/Ichimoku
BARS_BY_TF = {"M5": 800, "M15": 600, "M30": 600,
              "H1": 500, "H4": 400, "D1": 300}

M5_SECONDS = 300
DEFAULT_RISK_USDT = 20.0
MAX_DATA_AGE_MIN  = 15
# Per-timeframe staleness threshold. The last *closed* bar's start time is
# inherently (interval) minutes old the moment it closes, so a flat 15-min
# threshold makes H1/H4 strategies permanently stale. Allow ~2× the interval
# so a cycle that runs anywhere inside the next bar still passes.
MAX_DATA_AGE_BY_TF = {"M1": 5, "M5": 15, "M15": 35, "M30": 65,
                      "H1": 125, "H4": 485, "D1": 2880}
MAX_HOLD_BARS_BY_TF = {"M5": 96, "M15": 64, "H1": 48, "H4": 24, "D1": 14}


# ═══════════════════════════════════════════════════════════════════════════
# JSONL LOGGING — one file per (symbol, UTC date)
# ═══════════════════════════════════════════════════════════════════════════
class JsonlLogger:
    """Append-only structured logger: one event per line, rotating by UTC date."""

    def __init__(self, name: str, directory: Path, default_fields: dict | None = None) -> None:
        self.name = name
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.default_fields = dict(default_fields or {})

    def _current_path(self) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.directory / f"{self.name}-{today}.jsonl"

    def event(self, event: str, **fields) -> None:
        payload = {
            "ts":    datetime.now(timezone.utc).isoformat(),
            "event": event,
            **self.default_fields,
            **fields,
        }
        line = json.dumps(payload, default=str, ensure_ascii=False)
        with self._current_path().open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        log.debug("[%s] %s %s", self.name, event,
                  " ".join(f"{k}={v}" for k, v in fields.items() if k not in ("diag",)))

    def error(self, event: str, exc: Exception | None = None, **fields) -> None:
        if exc is not None:
            fields["error_class"] = type(exc).__name__
            fields["error_msg"]   = str(exc)
        self.event(event, level="error", **fields)


def write_portfolio_event(event: str, **fields) -> None:
    payload = {
        "ts":    datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with PORTFOLIO_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
    log.info("[portfolio] %s %s", event,
             " ".join(f"{k}={v}" for k, v in fields.items()
                      if k not in ("per_symbol", "diag")))


# ═══════════════════════════════════════════════════════════════════════════
# SEEN-SIGNAL PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════
def _seen_path(symbol: str) -> Path:
    return SEEN_DIR / f"{symbol}.json"


def load_seen(symbol: str) -> set:
    p = _seen_path(symbol)
    if not p.exists():
        return set()
    try:
        return {tuple(item) for item in json.loads(p.read_text(encoding="utf-8")).get("entries", [])}
    except Exception:
        return set()


def save_seen(symbol: str, seen: set) -> None:
    SEEN_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"schema": "v1", "entries": [list(t) for t in seen]}
    _seen_path(symbol).write_text(json.dumps(payload), encoding="utf-8")


# Track last-known open position so we can emit a "position_closed" event
# when it disappears.  Keyed by (symbol, position_id).
def _state_path(symbol: str) -> Path:
    return STATE_DIR / f"{symbol}.json"


def load_state(symbol: str) -> dict:
    p = _state_path(symbol)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(symbol: str, state: dict) -> None:
    _state_path(symbol).write_text(json.dumps(state, default=str, indent=2),
                                    encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# WINNER CONFIG LOADER
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class WinnerConfig:
    symbol:     str
    tag:        str
    strategy:   str
    base_tf:    str         # "M5" | "M15" | "H1"
    htf:        str         # "H1" | "H4"
    rr:         float       # 0.0 means "use tp_price column"
    use_daily:  bool
    params:     dict
    fn:         Callable = field(repr=False)

    @property
    def max_hold(self) -> int:
        return MAX_HOLD_BARS_BY_TF.get(self.base_tf, 96)


def load_winners(symbols_override: list[str] | None = None,
                 skip_unprofitable: bool = True) -> List[WinnerConfig]:
    """Read every results/_top_per_symbol/<SYM>/config.json into a WinnerConfig.

    `skip_unprofitable` drops configs flagged as fee-negative (tag != 'profitable_*').
    """
    if not WINNERS_DIR.exists():
        raise FileNotFoundError(
            f"{WINNERS_DIR} missing — run notebooks/26_top_strategy_per_symbol_bybit.ipynb first")
    winners: List[WinnerConfig] = []
    for sym_dir in sorted(WINNERS_DIR.iterdir()):
        if not sym_dir.is_dir() or sym_dir.name.startswith("_"):
            continue
        cfg_path = sym_dir / "config.json"
        if not cfg_path.exists():
            continue
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        sym = raw["symbol"]
        if symbols_override and sym not in symbols_override:
            continue
        tag = raw.get("tag", "unknown")
        if skip_unprofitable and not tag.startswith("profitable_"):
            log.warning("skip %s — tag=%s (no profitable config in sweep)", sym, tag)
            continue
        sname = raw["strategy"]
        if sname not in STRATEGY_FN:
            log.error("Unknown strategy '%s' for %s — skipping", sname, sym)
            continue
        # Convert session list-of-2 back to tuple if needed (json round-trips it as list)
        params = dict(raw.get("params", {}))
        if "session" in params and isinstance(params["session"], list):
            params["session"] = tuple(params["session"])
        if "confirms" in params and isinstance(params["confirms"], list):
            params["confirms"] = tuple(params["confirms"])
        winners.append(WinnerConfig(
            symbol    = sym,
            tag       = tag,
            strategy  = sname,
            base_tf   = raw["base_tf"],
            htf       = raw.get("htf", "H1"),
            rr        = float(raw.get("rr", 0.0) or 0.0),
            use_daily = bool(raw.get("use_daily", USE_DAILY.get(sname, False))),
            params    = params,
            fn        = STRATEGY_FN[sname],
        ))
    return winners


# ═══════════════════════════════════════════════════════════════════════════
# DATA FETCH (Bybit REST)
# ═══════════════════════════════════════════════════════════════════════════
async def fetch_ohlcv(client: BybitClient, symbol: str, tf: str,
                       n: int | None = None) -> pd.DataFrame:
    """Fetch the latest `n` closed bars and return tz-naive OHLCV frame.

    Bybit returns kline newest-first as
        [startTimeMs, open, high, low, close, volume, turnover]
    The most-recent bar is still forming; we drop it with iloc[:-1].
    """
    n = n or BARS_BY_TF.get(tf, 500)
    interval = BYBIT_INTERVAL.get(tf)
    if interval is None:
        raise ValueError(f"Unknown timeframe {tf!r}")
    raw = await client.get_kline(symbol=symbol, interval=interval, limit=n)
    if not raw:
        raise RuntimeError(f"empty kline {symbol} {tf}")
    df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close",
                                     "volume", "turnover"])
    df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("time").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.reset_index()
    df["time"] = df["time"].dt.tz_convert(None)   # tz-naive UTC, matches the
                                                   # CSV cache convention
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df = df[["time", "open", "high", "low", "close", "volume"]]
    # Drop forming candle so live + backtest see the same closed-bar set.
    return df.iloc[:-1].reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# SIGNAL EXTRACTION — run the strategy fn and pull the last closed bar
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class StrategySignal:
    bar_time: pd.Timestamp
    direction: int                  # +1 long, -1 short
    entry: float                    # signal-bar close (used as entry reference)
    sl: float
    tp: float

    @property
    def side(self) -> Side:
        return Side.BUY if self.direction == 1 else Side.SELL

    def as_dict(self) -> dict:
        return {
            "bar_time":  self.bar_time.isoformat() if hasattr(self.bar_time, "isoformat") else str(self.bar_time),
            "direction": "long" if self.direction == 1 else "short",
            "entry":     self.entry,
            "sl":        self.sl,
            "tp":        self.tp,
        }


def detect_signal(wcfg: WinnerConfig,
                  m5: pd.DataFrame, htf: pd.DataFrame, d1: pd.DataFrame | None,
                  ) -> tuple[Optional[StrategySignal], dict]:
    """Run the strategy fn end-to-end and read off the last closed bar.

    Returns (signal_or_None, diagnostics_dict). The diagnostics include the
    last bar's signal/SL/TP cells, RSI/ADX/ATR readings, and the strategy
    name — invaluable for backtest/live parity audits.
    """
    fn = wcfg.fn
    fn_args = fn.__code__.co_varnames[:fn.__code__.co_argcount]
    if "daily" in fn_args:
        df_sig = fn(m5, htf, d1 if d1 is not None and not d1.empty else None,
                    **wcfg.params)
    else:
        df_sig = fn(m5, htf, **wcfg.params)

    if df_sig.empty:
        return None, {"reason": "empty_signal_df"}

    last = df_sig.iloc[-1]
    sig_val = int(last.get("signal", 0))
    bar_time = pd.Timestamp(last["time"])
    diag = {
        "strategy":   wcfg.strategy,
        "rr_cfg":     wcfg.rr,
        "last_bar":   str(bar_time),
        "signal_raw": sig_val,
        "close":      float(last.get("close", float("nan"))),
        "atr":        float(last.get("atr", float("nan")))   if "atr"  in df_sig else None,
        "rsi":        float(last.get("rsi", float("nan")))   if "rsi"  in df_sig else None,
        "adx":        float(last.get("adx", float("nan")))   if "adx"  in df_sig else None,
        "h1_trend":   int(last.get("h1_trend", 0))           if "h1_trend" in df_sig else None,
    }
    if sig_val == 0:
        return None, diag

    sl_price = float(last.get("sl_price", float("nan")))
    if not np.isfinite(sl_price):
        diag["reject_reason"] = "sl_price_nan"
        return None, diag

    entry = float(last["close"])           # entry reference = signal-bar close
    if wcfg.rr and wcfg.rr > 0:
        risk = abs(entry - sl_price)
        if risk <= 0:
            diag["reject_reason"] = "zero_risk"
            return None, diag
        tp = entry + wcfg.rr * risk * sig_val
    else:
        tp_raw = last.get("tp_price", float("nan"))
        if not np.isfinite(tp_raw):
            diag["reject_reason"] = "tp_price_nan"
            return None, diag
        tp = float(tp_raw)
        # Sanity: TP must be on the correct side of entry
        if (sig_val == 1 and tp <= entry) or (sig_val == -1 and tp >= entry):
            diag["reject_reason"] = "tp_wrong_side"
            return None, diag

    sig = StrategySignal(
        bar_time=bar_time, direction=sig_val,
        entry=entry, sl=sl_price, tp=tp,
    )
    diag["sig"] = sig.as_dict()
    return sig, diag


# ═══════════════════════════════════════════════════════════════════════════
# RISK SIZING
# ═══════════════════════════════════════════════════════════════════════════
def qty_for_risk(entry: float, sl: float, risk_usdt: float,
                  info: InstrumentInfo) -> float:
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return 0.0
    raw = risk_usdt / risk_per_unit
    return normalize_qty(raw, info)


# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM NOTIFIER WRAPPER (with SSL fix already applied above)
# ═══════════════════════════════════════════════════════════════════════════
class TelegramSender:
    def __init__(self, notifier) -> None:
        self._n = notifier   # TelethonNotifier
        self.enabled = notifier is not None

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            await self._n.send(text)
        except Exception as exc:
            log.warning("telegram send failed: %s", exc)

    async def signal(self, sym: str, sig: StrategySignal, strat: str) -> None:
        arrow = "🟢" if sig.direction == 1 else "🔴"
        msg = (
            f"{arrow} *{sym}* — {strat}\n"
            f"side  : {sig.side.value.upper()}\n"
            f"bar   : `{sig.bar_time}`\n"
            f"entry : `{sig.entry:g}`\n"
            f"sl    : `{sig.sl:g}`\n"
            f"tp    : `{sig.tp:g}`"
        )
        await self.send(msg)

    async def entry_placed(self, sym: str, sig: StrategySignal,
                            qty: float, fill: float | None,
                            order_link_id: str) -> None:
        arrow = "🟢" if sig.direction == 1 else "🔴"
        fill_str = f"{fill:g}" if fill is not None else "market"
        msg = (
            f"{arrow} *ENTRY* — {sym}\n"
            f"side   : {sig.side.value.upper()}\n"
            f"qty    : `{qty:g}`\n"
            f"fill   : `{fill_str}`\n"
            f"sl/tp  : `{sig.sl:g} / {sig.tp:g}`\n"
            f"link   : `{order_link_id}`"
        )
        await self.send(msg)

    async def position_closed(self, sym: str, side: Side,
                               entry: float, exit_px: float,
                               qty: float, pnl: float,
                               opened_at: str | None,
                               closed_at: str | None) -> None:
        win = pnl >= 0
        em = "✅" if win else "❌"
        msg = (
            f"{em} *EXIT* — {sym}\n"
            f"side   : {side.value.upper()}\n"
            f"qty    : `{qty:g}`\n"
            f"entry  : `{entry:g}`\n"
            f"exit   : `{exit_px:g}`\n"
            f"pnl    : `{pnl:+.4f}` USDT\n"
            f"opened : `{opened_at or 'n/a'}`\n"
            f"closed : `{closed_at or 'n/a'}`"
        )
        await self.send(msg)


def build_telegram(args) -> TelegramSender:
    if args.no_telegram:
        return TelegramSender(None)
    api_id_str   = os.getenv("TELEGRAM_API_ID")
    api_hash     = os.getenv("TELEGRAM_API_HASH")
    phone        = os.getenv("TELEGRAM_PHONE")
    if not api_id_str or not api_hash:
        log.warning("TELEGRAM_API_ID/HASH missing — telegram disabled")
        return TelegramSender(None)
    try:
        from telegram_bot.telethon_notifier import TelethonNotifier
        notifier = TelethonNotifier(int(api_id_str), api_hash)
        # ssl-context already widened via SSL_CERT_FILE above; no further
        # patch needed here. login() is async — caller awaits it.
        notifier._phone_arg = phone   # type: ignore[attr-defined]
        return TelegramSender(notifier)
    except Exception as exc:
        log.warning("telegram init failed (%s) — disabled", exc)
        return TelegramSender(None)


async def maybe_login_telegram(sender: TelegramSender) -> None:
    if not sender.enabled:
        return
    notifier = sender._n
    phone = getattr(notifier, "_phone_arg", None)
    try:
        await notifier.login(phone=phone)
    except Exception as exc:
        log.warning("telegram login failed: %s — running without notifications", exc)
        sender.enabled = False


# ═══════════════════════════════════════════════════════════════════════════
# PER-SYMBOL CONTEXT
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class SymbolContext:
    cfg:      WinnerConfig
    info:     InstrumentInfo
    logger:   JsonlLogger
    seen:     set
    state:    dict
    risk_usdt: float


async def build_contexts(client: BybitClient, winners: List[WinnerConfig],
                          risk_usdt: float, run_id: str,
                          ) -> List[SymbolContext]:
    contexts: List[SymbolContext] = []
    for wc in winners:
        try:
            info = await client.get_instrument_info(wc.symbol)
        except Exception as exc:
            log.error("instrument info failed for %s: %s — skipped", wc.symbol, exc)
            continue
        lg = JsonlLogger(wc.symbol, LOG_DIR, default_fields={
            "symbol":   wc.symbol,
            "strategy": wc.strategy,
            "base_tf":  wc.base_tf,
            "rr":       wc.rr,
            "tag":      wc.tag,
            "run_id":   run_id,
        })
        lg.event("bot_start",
                 params=wc.params, htf=wc.htf,
                 risk_usdt=risk_usdt,
                 tick_size=info.tick_size, qty_step=info.qty_step,
                 min_qty=info.min_qty)
        ctx = SymbolContext(
            cfg=wc, info=info, logger=lg,
            seen=load_seen(wc.symbol),
            state=load_state(wc.symbol),
            risk_usdt=risk_usdt,
        )
        contexts.append(ctx)
    return contexts


# ═══════════════════════════════════════════════════════════════════════════
# ONE-CYCLE LOGIC
# ═══════════════════════════════════════════════════════════════════════════
async def run_symbol_cycle(ctx: SymbolContext, client: BybitClient,
                            tg: TelegramSender, dry_run: bool) -> dict:
    sym = ctx.cfg.symbol
    log_ = ctx.logger
    summary: dict = {"symbol": sym}

    # 1) Fetch fresh frames
    try:
        m5 = await fetch_ohlcv(client, sym, ctx.cfg.base_tf)
        htf = await fetch_ohlcv(client, sym, ctx.cfg.htf)
        d1  = await fetch_ohlcv(client, sym, "D1") if ctx.cfg.use_daily else None
    except Exception as exc:
        log_.error("data_fetch_error", exc=exc)
        return {**summary, "skipped": "data_fetch_error"}

    if len(m5) < 200 or len(htf) < 100:
        log_.event("skip", reason="too_few_bars",
                   m5_n=len(m5), htf_n=len(htf))
        return {**summary, "skipped": "too_few_bars"}

    last_bar = m5["time"].iloc[-1]
    now_utc = pd.Timestamp.now(tz="UTC").tz_localize(None)
    age_min = (now_utc - last_bar).total_seconds() / 60
    max_age = MAX_DATA_AGE_BY_TF.get(ctx.cfg.base_tf, MAX_DATA_AGE_MIN)
    if age_min > max_age:
        log_.event("skip", reason="stale_data", age_min=round(age_min, 1),
                   last_bar=str(last_bar), max_age_min=max_age)
        return {**summary, "skipped": "stale_data", "age_min": age_min}

    # 2) Detect signal
    try:
        sig, diag = detect_signal(ctx.cfg, m5, htf, d1)
    except Exception as exc:
        log_.error("strategy_exception", exc=exc)
        return {**summary, "skipped": "strategy_exception", "error": str(exc)}

    log_.event("cycle",
               last_bar=str(last_bar),
               m5_bars=len(m5), htf_bars=len(htf),
               age_min=round(age_min, 1),
               signal=sig is not None,
               diag=diag)

    # 3) Reconcile open position (detect close events even when no signal)
    try:
        positions = await client.get_positions(sym)
    except Exception as exc:
        log_.error("position_fetch_error", exc=exc)
        positions = []
    await _reconcile_position_close(ctx, client, tg, positions)

    if sig is None:
        return {**summary, "signal": False}

    sig_key = (str(sig.bar_time), sig.direction)
    if sig_key in ctx.seen:
        log_.event("skip", reason="already_traded",
                   bar_time=str(sig.bar_time), direction=sig.direction)
        return {**summary, "skipped": "already_traded"}

    has_open = len(positions) > 0
    log_.event("signal",
               bar_time=str(sig.bar_time),
               direction="long" if sig.direction == 1 else "short",
               entry=sig.entry, sl=sig.sl, tp=sig.tp,
               has_open_position=has_open)
    if not has_open:
        await tg.signal(sym, sig, ctx.cfg.strategy)

    if dry_run:
        ctx.seen.add(sig_key); save_seen(sym, ctx.seen)
        log_.event("dry_run", msg="no_order_sent")
        return {**summary, "signal": True, "stage": "dry_run"}

    if has_open:
        log_.event("skip", reason="position_open", existing=positions[0].size)
        return {**summary, "signal": True, "stage": "position_open"}

    # 4) Size + place market order
    qty = qty_for_risk(sig.entry, sig.sl, ctx.risk_usdt, ctx.info)
    if qty <= 0:
        log_.event("skip", reason="qty_zero",
                   risk_usdt=ctx.risk_usdt,
                   risk_per_unit=abs(sig.entry - sig.sl),
                   min_qty=ctx.info.min_qty)
        return {**summary, "signal": True, "stage": "qty_zero"}

    link_id = f"bb-{sym}-{uuid.uuid4().hex[:10]}"
    sl_str  = price_to_str(normalize_price(sig.sl, ctx.info.tick_size,
                                            mode="floor" if sig.direction == 1 else "ceil"),
                            ctx.info.tick_size)
    tp_str  = price_to_str(normalize_price(sig.tp, ctx.info.tick_size,
                                            mode="ceil"  if sig.direction == 1 else "floor"),
                            ctx.info.tick_size)
    qty_str = format(qty, "f").rstrip("0").rstrip(".")
    if "." not in qty_str: qty_str = qty_str

    try:
        # Market order with SL/TP attached. Bybit unified `place_order` is
        # exposed via the BybitClient._call wrapper.
        resp = await client._call(
            "place_order",
            category="linear",
            symbol=sym,
            side="Buy" if sig.direction == 1 else "Sell",
            orderType="Market",
            qty=qty_str,
            timeInForce="IOC",
            orderLinkId=link_id,
            stopLoss=sl_str,
            takeProfit=tp_str,
            slTriggerBy="LastPrice",
            tpTriggerBy="LastPrice",
            positionIdx=0,
            reduceOnly=False,
            closeOnTrigger=False,
        )
        order_id = resp.get("result", {}).get("orderId")
    except Exception as exc:
        log_.error("order_place_failed", exc=exc, link=link_id,
                   sl=sl_str, tp=tp_str, qty=qty_str)
        return {**summary, "signal": True, "stage": "order_failed", "error": str(exc)}

    ctx.seen.add(sig_key); save_seen(sym, ctx.seen)
    log_.event("market_order_placed",
               link=link_id, order_id=order_id,
               side=sig.side.value, qty=qty,
               sl=sig.sl, tp=sig.tp,
               signal_entry=sig.entry, bar_time=str(sig.bar_time))

    # Pull the resulting fill price from the position snapshot (best effort)
    fill_price: float | None = None
    try:
        pos_now = await client.get_positions(sym)
        if pos_now:
            fill_price = pos_now[0].entry_price
            ctx.state.update({
                "open_position": {
                    "side":         pos_now[0].side.value,
                    "size":         pos_now[0].size,
                    "entry_price":  pos_now[0].entry_price,
                    "sl":           sig.sl,
                    "tp":           sig.tp,
                    "link":         link_id,
                    "opened_at":    datetime.now(timezone.utc).isoformat(),
                    "signal_bar":   str(sig.bar_time),
                }
            })
            save_state(sym, ctx.state)
    except Exception as exc:
        log_.error("post_place_position_check_failed", exc=exc)

    await tg.entry_placed(sym, sig, qty, fill_price, link_id)
    return {**summary, "signal": True, "stage": "placed", "ticket": order_id, "qty": qty}


async def _reconcile_position_close(ctx: SymbolContext,
                                     client: BybitClient,
                                     tg: TelegramSender,
                                     positions: list[Position]) -> None:
    """If we were tracking an open position and it's gone, log + notify the close."""
    last_open = (ctx.state or {}).get("open_position")
    if not last_open:
        if positions:
            # Fresh position not tracked yet — start tracking it
            p = positions[0]
            ctx.state["open_position"] = {
                "side":         p.side.value,
                "size":         p.size,
                "entry_price":  p.entry_price,
                "opened_at":    datetime.now(timezone.utc).isoformat(),
            }
            save_state(ctx.cfg.symbol, ctx.state)
        return
    if positions:
        return   # still open — nothing to do

    # Position is gone — look up closed P&L for the most recent close
    closed_at = datetime.now(timezone.utc).isoformat()
    pnl = 0.0; exit_px = float("nan")
    try:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - 6 * 60 * 60 * 1000   # last 6 h is plenty
        pnls = await client.get_closed_pnl(symbol=ctx.cfg.symbol,
                                            start_ms=start_ms, end_ms=end_ms,
                                            limit=20)
        if pnls:
            best = pnls[0]
            pnl     = float(best.get("closedPnl", 0) or 0)
            exit_px = float(best.get("avgExitPrice", 0) or 0) or float("nan")
            closed_at = datetime.fromtimestamp(int(best["updatedTime"]) / 1000,
                                                tz=timezone.utc).isoformat()
    except Exception as exc:
        ctx.logger.error("closed_pnl_fetch_failed", exc=exc)

    side = Side.BUY if last_open["side"].lower() == "buy" else Side.SELL
    ctx.logger.event("position_closed",
                     side=side.value, size=last_open.get("size"),
                     entry=last_open.get("entry_price"),
                     exit_price=exit_px, pnl_usdt=pnl,
                     opened_at=last_open.get("opened_at"),
                     closed_at=closed_at)
    await tg.position_closed(
        sym=ctx.cfg.symbol, side=side,
        entry=float(last_open.get("entry_price") or 0.0),
        exit_px=exit_px, qty=float(last_open.get("size") or 0.0), pnl=pnl,
        opened_at=last_open.get("opened_at"), closed_at=closed_at,
    )
    ctx.state.pop("open_position", None)
    save_state(ctx.cfg.symbol, ctx.state)


# ═══════════════════════════════════════════════════════════════════════════
# PORTFOLIO CYCLE
# ═══════════════════════════════════════════════════════════════════════════
async def run_portfolio_cycle(contexts: List[SymbolContext],
                                client: BybitClient,
                                tg: TelegramSender,
                                dry_run: bool) -> None:
    started = time.monotonic()
    per_symbol = []
    for ctx in contexts:
        try:
            res = await run_symbol_cycle(ctx, client, tg, dry_run)
        except Exception as exc:
            log.exception("cycle exception for %s", ctx.cfg.symbol)
            ctx.logger.error("cycle_uncaught", exc=exc)
            res = {"symbol": ctx.cfg.symbol, "skipped": "uncaught",
                   "error": str(exc)}
        per_symbol.append(res)
    sigs = sum(1 for r in per_symbol if r.get("signal"))
    placed = sum(1 for r in per_symbol if r.get("stage") == "placed")
    skipped = sum(1 for r in per_symbol if "skipped" in r)
    write_portfolio_event("cycle_complete",
                          symbols=len(contexts),
                          signals=sigs, placed=placed, skipped=skipped,
                          elapsed_s=round(time.monotonic() - started, 2),
                          per_symbol=per_symbol)


def sleep_until_next_m5(extra: float = 1.0) -> None:
    now = time.time()
    delay = max(1.0, (int(now // M5_SECONDS) + 1) * M5_SECONDS - now + extra)
    log.info("sleeping %.0fs until next M5 close", delay)
    time.sleep(delay)


# ═══════════════════════════════════════════════════════════════════════════
# CLI + MAIN
# ═══════════════════════════════════════════════════════════════════════════
def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbols", nargs="+", default=None,
                   help="Explicit symbol list. Default: every profitable symbol.")
    p.add_argument("--once", action="store_true",
                   help="One cycle then exit.")
    p.add_argument("--dry-run", action="store_true",
                   help="Detect signals + log + notify; do not send orders.")
    p.add_argument("--testnet", action="store_true",
                   help="Connect to api-testnet.bybit.com.")
    p.add_argument("--demo", action="store_true",
                   help="Connect to api-demo.bybit.com (paper trading).")
    p.add_argument("--no-telegram", action="store_true",
                   help="Disable Telegram notifications.")
    p.add_argument("--include-unprofitable", action="store_true",
                   help="Include symbols whose winner tag is wr_target_only.")
    p.add_argument("--risk-usdt", type=float, default=None,
                   help="Override per-trade risk in USDT (default: env RISK_FIXED_USDT or 20).")
    p.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


async def amain(args) -> None:
    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    risk_usdt = (args.risk_usdt if args.risk_usdt is not None
                 else float(os.getenv("RISK_FIXED_USDT", DEFAULT_RISK_USDT)))

    # Bybit client
    api_key    = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    testnet    = args.testnet or os.getenv("BYBIT_TESTNET", "false").lower() == "true"
    demo       = args.demo    or os.getenv("BYBIT_DEMO",    "false").lower() == "true"
    if not api_key:
        log.error("BYBIT_API_KEY missing — set in .env")
        return
    client = BybitClient(api_key=api_key, api_secret=api_secret,
                          testnet=testnet, demo=demo)

    # Telegram (optional)
    tg = build_telegram(args)
    await maybe_login_telegram(tg)

    # Winners
    winners = load_winners(symbols_override=args.symbols,
                            skip_unprofitable=not args.include_unprofitable)
    if not winners:
        log.error("no winners loaded — exiting")
        return
    log.info("loaded %d symbol winners: %s",
             len(winners), [w.symbol for w in winners])

    # Contexts
    contexts = await build_contexts(client, winners, risk_usdt, run_id)
    if not contexts:
        log.error("no contexts built — exiting"); return

    write_portfolio_event("bot_run_started",
                          run_id=run_id,
                          symbols=[c.cfg.symbol for c in contexts],
                          dry_run=args.dry_run,
                          testnet=testnet, demo=demo,
                          risk_usdt=risk_usdt,
                          telegram_enabled=tg.enabled)
    if tg.enabled:
        await tg.send(
            f"🤖 *Bybit scalper started*\n"
            f"run\\_id: `{run_id}`\n"
            f"symbols: {', '.join(c.cfg.symbol for c in contexts)}\n"
            f"risk/trade: `{risk_usdt}` USDT\n"
            f"dry\\_run: `{args.dry_run}`  testnet: `{testnet}`  demo: `{demo}`"
        )

    try:
        if args.once:
            await run_portfolio_cycle(contexts, client, tg, args.dry_run)
            return
        log.info("entering main loop — Ctrl+C to stop")
        while True:
            try:
                await run_portfolio_cycle(contexts, client, tg, args.dry_run)
            except Exception as exc:
                write_portfolio_event("portfolio_cycle_exception", error=repr(exc))
                log.exception("portfolio cycle exception")
            sleep_until_next_m5()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("stopped by user")
    finally:
        write_portfolio_event("bot_run_stopped", run_id=run_id)
        if tg.enabled:
            try:
                await tg.send(f"🛑 *Bybit scalper stopped* — run\\_id `{run_id}`")
                await tg._n.stop()
            except Exception:
                pass


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Best-effort: load .env so tokens/keys are available
    try:
        from dotenv import load_dotenv
        load_dotenv(_REPO_ROOT / ".env")
    except ImportError:
        pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop(*_a):
        log.info("signal received — shutting down")
        for t in asyncio.all_tasks(loop):
            t.cancel()
    _signal.signal(_signal.SIGINT,  _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    try:
        loop.run_until_complete(amain(args))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
