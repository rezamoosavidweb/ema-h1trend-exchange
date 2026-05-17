"""
Order Block Reaction Bot — entry point.

Identical CLI interface to app/main.py but runs the Order Block strategy
(notebooks/08_order_block_reaction_crypto.ipynb) instead of EMA H1 Trend.

Usage:
    python app/ob_main.py
    python app/ob_main.py --symbol BTCUSDT --risk 20 --rr 2.0
    python app/ob_main.py --symbol ETHUSDT --dry-run --log-level DEBUG
    python app/ob_main.py --symbols BTCUSDT,ETHUSDT

Strategy parameters with recommended defaults:
    --rr    2.0   (RR=2 matches notebook: TP = entry ± 2×SL-distance)
    --risk  20.0  (risk $20 per trade)
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

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.settings import Settings, get_settings
from services.ob_trading_service import OBTradingService
from telemetry.logging import configure_logging

log = logging.getLogger(__name__)


# ── Argument parsing ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Order Block Reaction Bot — Bybit Linear Futures",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--symbol",  default=None,
                   help="Bybit linear symbol (e.g. BTCUSDT).")
    p.add_argument("--symbols", default=None,
                   help="Comma-separated symbols for multi-symbol mode.")
    p.add_argument("--magic",   type=int, default=None,
                   help="Order isolation magic. Default: auto from symbol.")
    p.add_argument("--risk",    type=float, default=None,
                   help="Fixed USDT risk per trade (default 20.0).")
    p.add_argument("--rr",      type=float, default=None,
                   help="Reward:risk ratio (recommended 2.0 for OB strategy).")
    p.add_argument("--pip-size", type=float, default=None,
                   help="Price increment. Default: auto from symbol.")
    p.add_argument("--leverage", type=int, default=None,
                   help="Leverage (default 1).")
    p.add_argument("--dry-run", action="store_true",
                   help="Log signals but do not place orders.")
    p.add_argument("--testnet", action="store_true",
                   help="Use Bybit testnet.")
    p.add_argument("--replace-pending", action="store_true",
                   help="Cancel existing pending on each cycle before placing new one.")
    p.add_argument("--log-level", default=None,
                   help="DEBUG | INFO | WARNING | ERROR")
    p.add_argument("--log-file",  default=None)
    p.add_argument("--log-json",  action="store_true")
    return p.parse_args()


def _apply_overrides(args: argparse.Namespace) -> None:
    if args.symbol:         os.environ["SYMBOL"]           = args.symbol
    if args.magic:          os.environ["MAGIC"]            = str(args.magic)
    if args.risk is not None:  os.environ["RISK_FIXED_USDT"] = str(args.risk)
    if args.rr is not None:
        os.environ["RR"] = str(args.rr)
    elif not os.environ.get("RR"):
        # OB notebook default: RISK_REWARD = 2.0
        # TP = entry ± (entry - sl) * 2.0
        os.environ["RR"] = "2.0"
    if args.pip_size:       os.environ["PIP_SIZE"]         = str(args.pip_size)
    if args.leverage:       os.environ["LEVERAGE"]         = str(args.leverage)
    if args.dry_run:        os.environ["DRY_RUN"]          = "true"
    if args.testnet:        os.environ["BYBIT_TESTNET"]    = "true"
    if args.replace_pending: os.environ["REPLACE_PENDING"] = "true"
    if args.log_level:      os.environ["LOG_LEVEL"]        = args.log_level
    if args.log_file:       os.environ["LOG_FILE"]         = args.log_file
    if args.log_json:       os.environ["LOG_JSON"]         = "true"
    # OB bot writes to logs_ob/ so its files don't mix with the EMA bot's logs/
    if not os.environ.get("EVENT_LOG_DIR"):
        os.environ["EVENT_LOG_DIR"] = "logs_ob"


# ── Signal handlers ───────────────────────────────────────────────────────────

def _install_signal_handlers(
    service: OBTradingService,
    loop: asyncio.AbstractEventLoop,
) -> None:
    def _shutdown(signum, frame):
        log.info("Signal %s — graceful shutdown.", signum)
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(service.stop(), loop=loop)
        )
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)


def _install_signal_handlers_multi(runner, loop: asyncio.AbstractEventLoop) -> None:
    def _shutdown(signum, frame):
        log.info("Signal %s — stopping all OB symbols.", signum)
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(runner.stop(), loop=loop)
        )
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()
    _apply_overrides(args)

    cfg = get_settings()

    configure_logging(
        level=cfg.log_level,
        log_file=cfg.log_file,
        json_output=cfg.log_json,
    )

    mode = "DEMO" if cfg.bybit_demo else ("TESTNET" if cfg.bybit_testnet else "LIVE")
    log.info(
        "Starting Order Block Bot | symbol=%s magic=%d risk=%.2f USDT "
        "rr=%.1f leverage=%dx mode=%s dry_run=%s",
        cfg.symbol,
        cfg.effective_magic(),
        cfg.risk_fixed_usdt,
        cfg.rr,
        cfg.leverage,
        mode,
        cfg.dry_run,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            log.info("Multi-symbol OB mode | symbols=%s", symbols)
            from services.multi_symbol_runner import MultiSymbolRunner

            class _OBRunner(MultiSymbolRunner):
                def _make_service(self, sym_cfg: Settings) -> OBTradingService:
                    return OBTradingService(sym_cfg)

            runner = _OBRunner(symbols, cfg)

            async def _run_multi():
                _install_signal_handlers_multi(runner, loop)
                await runner.run()

            loop.run_until_complete(_run_multi())
        else:
            service = OBTradingService(cfg)
            _install_signal_handlers(service, loop)
            loop.run_until_complete(service.run())

    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — shutting down.")
    finally:
        loop.close()
        log.info("Event loop closed.")


if __name__ == "__main__":
    main()
