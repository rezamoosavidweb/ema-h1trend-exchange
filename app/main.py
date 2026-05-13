"""
Application entry point — wires up settings, logging, signal handlers and starts the service.

Usage:
    python app/main.py
    python app/main.py --symbol ETHUSDT --risk 0.005 --dry-run
    python app/main.py --backtest --bars 500

Env vars override all CLI defaults (see .env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

# ── Make project root importable regardless of CWD ────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.settings import Settings, get_settings
from services.trading_service import TradingService
from telemetry.logging import configure_logging

log = logging.getLogger(__name__)


def _build_telegram_bot(cfg: Settings, services: list) -> "Optional[TelegramBot]":
    """Return a TelegramBot if TELEGRAM_API_ID and TELEGRAM_API_HASH are configured."""
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        return None
    try:
        from telegram_bot.bot import TelegramBot
        return TelegramBot(
            api_id=cfg.telegram_api_id,
            api_hash=cfg.telegram_api_hash,
            phone=cfg.telegram_phone,
            services=services,
            base_settings=cfg,
            session_file=cfg.telegram_session_file or None,
        )
    except ImportError:
        log.warning(
            "telethon not installed — Telegram notifier disabled. "
            "Run: pip install telethon"
        )
        return None


def _build_ws_notifier(cfg: Settings, tg_bot) -> "Optional[object]":
    """
    Return a WsOrderNotifier if WebSocket notifications are enabled and
    a Telegram sender is available.
    """
    if not cfg.ws_notifier_enabled:
        return None
    if tg_bot is None:
        log.info("WsOrderNotifier: no Telegram bot configured — skipping.")
        return None
    try:
        from services.ws_order_notifier import WsOrderNotifier
        notifier = WsOrderNotifier(settings=cfg, send_fn=tg_bot.send)
        log.info("WsOrderNotifier created — will send order alerts to Telegram.")
        return notifier
    except Exception as exc:
        log.warning("WsOrderNotifier could not be created: %s", exc)
        return None


# ── Argument parsing ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="EMA H1 Trend Strategy — Bybit Linear Futures",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Symbol / identity
    p.add_argument("--symbol", default=None,
                   help="Bybit linear symbol (e.g. BTCUSDT). Overrides SYMBOL env var.")
    p.add_argument("--symbols", default=None,
                   help="Comma-separated list of symbols for multi-symbol mode (e.g. BTCUSDT,ETHUSDT).")
    p.add_argument("--magic", type=int, default=None,
                   help="Order isolation magic. Default: auto-derived from symbol.")

    # Strategy parameters
    p.add_argument("--risk", type=float, default=None,
                   help="Fraction of balance risked per trade (e.g. 0.01 = 1%).")
    p.add_argument("--pip-size", type=float, default=None,
                   help="Price increment for entry offset. Default: auto from symbol.")
    p.add_argument("--leverage", type=int, default=None,
                   help="Leverage to set on Bybit (default: 1).")
    p.add_argument("--rr", type=float, default=None,
                   help="Reward:risk ratio (TP / SL distance).")

    # Execution flags
    p.add_argument("--dry-run", action="store_true",
                   help="Generate signals but do not place/modify/cancel orders.")
    p.add_argument("--testnet", action="store_true",
                   help="Use Bybit testnet instead of mainnet.")
    p.add_argument("--replace-pending", action="store_true",
                   help="Cancel existing pending on each cycle before placing new one.")

    # Logging
    p.add_argument("--log-level", default=None,
                   help="Logging level: DEBUG | INFO | WARNING | ERROR.")
    p.add_argument("--log-file", default=None,
                   help="Write logs to this file path in addition to stdout.")
    p.add_argument("--log-json", action="store_true",
                   help="Emit JSON log lines (for log aggregation systems).")
    p.add_argument("--signal-log", default=None,
                   help="CSV file for strategy signal audit log.")

    # Backtest mode
    p.add_argument("--backtest", action="store_true",
                   help="Run walk-forward backtest on Bybit live data then exit.")
    p.add_argument("--backtest-bars", type=int, default=500,
                   help="Number of M5 bars to simulate.")
    p.add_argument("--backtest-out", default="backtest_results",
                   help="Output directory for backtest trades.csv + metrics.csv.")
    p.add_argument("--start-balance", type=float, default=None,
                   help="Starting balance for backtest P&L simulation.")

    return p.parse_args()


# ── Env override helpers ──────────────────────────────────────────────────────

def _apply_overrides(args: argparse.Namespace) -> None:
    """Push CLI flags into env so pydantic_settings picks them up."""
    if args.symbol:
        os.environ["SYMBOL"] = args.symbol
    if args.magic is not None:
        os.environ["MAGIC"] = str(args.magic)
    if args.risk is not None:
        os.environ["RISK_PER_TRADE"] = str(args.risk)
    if args.pip_size is not None:
        os.environ["PIP_SIZE"] = str(args.pip_size)
    if args.leverage is not None:
        os.environ["LEVERAGE"] = str(args.leverage)
    if args.rr is not None:
        os.environ["RR"] = str(args.rr)
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    if args.testnet:
        os.environ["BYBIT_TESTNET"] = "true"
    if args.replace_pending:
        os.environ["REPLACE_PENDING"] = "true"
    if args.log_level:
        os.environ["LOG_LEVEL"] = args.log_level
    if args.log_file:
        os.environ["LOG_FILE"] = args.log_file
    if args.log_json:
        os.environ["LOG_JSON"] = "true"
    if args.signal_log:
        os.environ["SIGNAL_LOG_CSV"] = args.signal_log
    if args.start_balance is not None:
        os.environ["START_BALANCE"] = str(args.start_balance)


# ── Backtest mode ─────────────────────────────────────────────────────────────

async def run_backtest(cfg: Settings, args: argparse.Namespace) -> None:
    """Fetch live Bybit data and run walk-forward backtest."""
    from exchange.bybit_client import BybitClient
    from market.data_fetcher import DataFetcher
    from services.trading_service import _build_context
    from strategy.backtest import run_backtest as _run_backtest

    client = BybitClient(
        api_key=cfg.bybit_api_key,
        api_secret=cfg.bybit_api_secret,
        testnet=cfg.bybit_testnet,
        demo=cfg.bybit_demo,
    )
    fetcher = DataFetcher(
        client=client,
        symbol=cfg.symbol,
        tf_entry=cfg.tf_entry,
        tf_trend=cfg.tf_trend,
        bars_entry=min(args.backtest_bars + 250, 1000),
        bars_trend=cfg.bars_trend,
    )

    log.info("Fetching data for backtest | symbol=%s bars=%d", cfg.symbol, args.backtest_bars)
    m5, h1 = await fetcher.fetch_closed_frames()
    m5_ctx = _build_context(m5, h1)

    total = len(m5_ctx)
    if args.backtest_bars > 0 and total > args.backtest_bars:
        m5_ctx = m5_ctx.iloc[-args.backtest_bars:].copy()

    log.info("Running backtest | bars=%d (fetched=%d)", len(m5_ctx), total)

    trades_df, equity_curve = _run_backtest(
        m5_ctx,
        start_balance=cfg.start_balance,
        lookback_bars=cfg.lookback_bars,
        pending_offset_ticks=cfg.pending_offset_ticks,
        pip_size=cfg.effective_pip_size(),
        rr=cfg.rr,
        risk_per_trade=cfg.risk_per_trade,
        pending_expiry_min=cfg.pending_expiry_min,
        entry_timeframe_minutes=cfg.entry_tf_minutes,
    )

    # Print summary
    import numpy as np
    n = len(trades_df)
    if n > 0:
        wins = (trades_df["pnl"] > 0).sum()
        pf_denom = abs(trades_df[trades_df["pnl"] < 0]["pnl"].sum())
        pf = trades_df[trades_df["pnl"] > 0]["pnl"].sum() / max(pf_denom, 1e-12)
        end_bal = float(trades_df["balance_after"].iloc[-1])
        print(f"\n{'='*55}")
        print(f"Backtest Results | {cfg.symbol}")
        print(f"{'='*55}")
        print(f"Trades:        {n}")
        print(f"Win rate:      {wins/n*100:.1f}%")
        print(f"Net PnL:       {trades_df['pnl'].sum():.2f} USDT")
        print(f"Profit factor: {pf:.3f}")
        print(f"Start balance: {cfg.start_balance:.2f}")
        print(f"End balance:   {end_bal:.2f}")
        print(f"Return:        {(end_bal/cfg.start_balance - 1)*100:.2f}%")
        print(f"{'='*55}\n")
        print(trades_df.to_string(index=False))
    else:
        print("No trades in this window.")

    # Save to files
    out = Path(args.backtest_out)
    out.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(out / "trades.csv", index=False)
    log.info("Saved backtest results to %s/", out)


# ── Signal handlers ───────────────────────────────────────────────────────────

def _install_signal_handlers(service: TradingService, loop: asyncio.AbstractEventLoop, tg_bot=None) -> None:
    """Install SIGINT and SIGTERM handlers for graceful shutdown."""
    def _handle_shutdown(signum, frame):
        log.info("Signal %s received — initiating graceful shutdown.", signum)
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(service.stop(), loop=loop)
        )
        if tg_bot is not None:
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(tg_bot.stop(), loop=loop)
            )

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)


def _install_signal_handlers_async(runner, loop: asyncio.AbstractEventLoop) -> None:
    """Signal handlers for multi-symbol runner."""
    def _handle_shutdown(signum, frame):
        log.info("Signal %s received — stopping all symbols.", signum)
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(runner.stop(), loop=loop)
        )

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()
    _apply_overrides(args)

    # Load config (singleton — reads .env + env vars)
    cfg = get_settings()

    # Configure logging first
    configure_logging(
        level=cfg.log_level,
        log_file=cfg.log_file,
        json_output=cfg.log_json,
    )

    mode = "DEMO" if cfg.bybit_demo else ("TESTNET" if cfg.bybit_testnet else "LIVE")
    log.info(
        "Starting EMA H1 Trend Bot | symbol=%s magic=%d risk=%.4f "
        "leverage=%dx mode=%s dry_run=%s",
        cfg.symbol,
        cfg.effective_magic(),
        cfg.risk_per_trade,
        cfg.leverage,
        mode,
        cfg.dry_run,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        if args.backtest:
            loop.run_until_complete(run_backtest(cfg, args))
            return

        # Multi-symbol mode
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            log.info("Multi-symbol mode | symbols=%s", symbols)
            from services.multi_symbol_runner import MultiSymbolRunner
            tg_bot      = _build_telegram_bot(cfg, [])
            ws_notifier = _build_ws_notifier(cfg, tg_bot)
            runner      = MultiSymbolRunner(
                symbols,
                cfg,
                telegram_bot=tg_bot,
                ws_notifier=ws_notifier,
            )

            async def _run_multi():
                _install_signal_handlers_async(runner, loop)
                if tg_bot is not None:
                    await tg_bot.login()
                await runner.run()

            loop.run_until_complete(_run_multi())
        else:
            service     = TradingService(cfg)
            tg_bot      = _build_telegram_bot(cfg, [service])
            ws_notifier = _build_ws_notifier(cfg, tg_bot)
            _install_signal_handlers(service, loop, tg_bot=tg_bot)

            if tg_bot is not None:
                async def _run_single_with_telegram():
                    await tg_bot.login()
                    tasks = [service.run(), tg_bot.run()]
                    if ws_notifier is not None:
                        tasks.append(ws_notifier.run())
                    await asyncio.gather(*tasks)
                loop.run_until_complete(_run_single_with_telegram())
            else:
                loop.run_until_complete(service.run())

    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — shutting down.")
    finally:
        loop.close()
        log.info("Event loop closed.")


if __name__ == "__main__":
    main()
