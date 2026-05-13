"""
Trading Service — the main async event loop.

This is the direct replacement of the MT5 main() production loop from main.py.
The loop structure is IDENTICAL to the MT5 version:

  while True:
      1. Duplicate-candle guard (peek at latest M5 bar time)
      2. Fetch M5 + H1 OHLCV
      3. Build context (add EMAs, merge H1 trend onto M5)
      4. Generate signals (list_setup_signals)
      5. Sync pending orders (create / modify / cancel)
      6. Sleep until next M5 candle boundary

Additional Bybit-specific steps added around the same core:
  - Startup: reconcile orphan orders, set leverage, set position mode
  - Per-cycle: reconcile exchange state (catch external fills/cancels)
  - Graceful shutdown on SIGINT/SIGTERM

MT5 behavior preserved exactly:
  - last_processed_candle guard prevents duplicate processing
  - Signal expiry based on bars_passed (not wall-clock time)
  - Position open → skip pending sync
  - Same price comparison with pip_size tolerance
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from config.settings import Settings
from exchange.bybit_client import BybitClient
from execution.order_manager import OrderManager
from execution.reconciler import OrderReconciler
from market.data_fetcher import DataFetcher
from models.order import InstrumentInfo
from state.bot_state import BotState
from storage.signal_log import SignalLogger
from storage.event_journal import EventJournal
from strategy.crypto_core import add_emas, merge_h1_trend_onto_m5
from strategy.setup import list_setup_signals
from telemetry.logging import SymbolAdapter, add_symbol_file_handler

log = logging.getLogger(__name__)


class TradingService:
    """
    Orchestrates the full strategy execution cycle for one symbol.

    Construction:
        service = TradingService(settings)
        await service.run()

    Graceful shutdown:
        await service.stop()
    """

    def __init__(self, settings: Settings) -> None:
        self._cfg = settings
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._log = SymbolAdapter(log, settings.symbol)

        # ── Infrastructure ────────────────────────────────────────────────────
        self._client = BybitClient(
            api_key=settings.bybit_api_key,
            api_secret=settings.bybit_api_secret,
            testnet=settings.bybit_testnet,
            demo=settings.bybit_demo,
            max_retries=settings.max_retries,
            retry_base_delay=settings.retry_base_delay,
            retry_max_delay=settings.retry_max_delay,
        )

        # ── Per-symbol state ──────────────────────────────────────────────────
        self._state = BotState(symbol=settings.symbol)

        # ── Signal logger ─────────────────────────────────────────────────────
        self._signal_log = SignalLogger(settings.signal_log_csv)

        # ── Event journal (JSONL per symbol) ──────────────────────────────────
        self._journal = EventJournal(settings.symbol, log_dir=settings.event_log_dir)
        if self._journal.enabled:
            self._log.info("Event journal → %s", self._journal.path)

        # These are set up in _initialize()
        self._info: Optional[InstrumentInfo] = None
        self._fetcher: Optional[DataFetcher] = None
        self._order_mgr: Optional[OrderManager] = None
        self._reconciler: Optional[OrderReconciler] = None

    # ── Public properties (for Telegram bot access) ───────────────────────────

    @property
    def symbol(self) -> str:
        return self._cfg.symbol

    @property
    def state(self) -> BotState:
        return self._state

    @property
    def settings(self) -> Settings:
        return self._cfg

    @property
    def client(self) -> BybitClient:
        return self._client

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the trading loop. Blocks until stop() is called or fatal error."""
        self._running = True

        try:
            await self._initialize()
            await self._loop()
        except asyncio.CancelledError:
            self._log.info("TradingService cancelled.")
        except Exception as exc:
            self._log.exception("Fatal error in TradingService: %s", exc)
            raise
        finally:
            await self._teardown()

    async def stop(self) -> None:
        """Signal graceful shutdown."""
        self._log.info("Shutdown requested.")
        self._running = False
        self._shutdown_event.set()

    # ── Initialization ────────────────────────────────────────────────────────

    async def _initialize(self) -> None:
        """One-time setup: instrument info, leverage, position mode, reconcile."""
        cfg = self._cfg
        self._log.info(
            "Initializing | testnet=%s leverage=%dx dry_run=%s",
            cfg.bybit_testnet, cfg.leverage, cfg.dry_run,
        )

        # Fetch instrument info (tick size, qty step, etc.)
        self._info = await self._client.get_instrument_info(cfg.symbol)
        self._log.info(
            "Instrument | tickSize=%s qtyStep=%s minQty=%s maxQty=%s",
            self._info.tick_size,
            self._info.qty_step,
            self._info.min_qty,
            self._info.max_qty,
        )

        # Set leverage and position mode (idempotent — ignores "not modified")
        if not cfg.dry_run:
            await self._client.set_leverage(cfg.symbol, cfg.leverage)
            mode_int = 0 if cfg.position_mode == "one_way" else 3
            await self._client.set_position_mode(cfg.symbol, mode_int)

        # Build sub-components
        self._fetcher = DataFetcher(
            client=self._client,
            symbol=cfg.symbol,
            tf_entry=cfg.tf_entry,
            tf_trend=cfg.tf_trend,
            bars_entry=cfg.bars_entry,
            bars_trend=cfg.bars_trend,
        )

        self._order_mgr = OrderManager(
            client=self._client,
            info=self._info,
            state=self._state,
            magic=cfg.effective_magic(),
            symbol=cfg.symbol,
            position_idx=cfg.position_idx,
            leverage=cfg.leverage,
            dry_run=cfg.dry_run,
            journal=self._journal,
        )

        self._reconciler = OrderReconciler(
            client=self._client,
            state=self._state,
            magic=cfg.effective_magic(),
            symbol=cfg.symbol,
            journal=self._journal,
        )

        # ── Per-symbol log file ───────────────────────────────────────────────
        add_symbol_file_handler(cfg.symbol, log_dir="logs", json_output=cfg.log_json)

        # ── Startup recovery: adopt orphan orders from previous session ───────
        await self._reconciler.reconcile_on_startup()

        self._log.info(
            "Initialized | magic=%d pip_size=%.6f lookback=%d "
            "offset_ticks=%.1f expiry_min=%d rr=%.1f risk=%.4f",
            cfg.effective_magic(),
            cfg.effective_pip_size(),
            cfg.lookback_bars,
            cfg.pending_offset_ticks,
            cfg.pending_expiry_min,
            cfg.rr,
            cfg.risk_per_trade,
        )

        await self._log_account_summary()

    # ── Account summary ───────────────────────────────────────────────────────

    async def _log_account_summary(self) -> None:
        """Log a full account snapshot to console and journal at startup."""
        cfg = self._cfg
        sep = "─" * 55
        mode = "DEMO" if cfg.bybit_demo else ("TESTNET" if cfg.bybit_testnet else "LIVE")

        self._log.info(sep)
        self._log.info("ACCOUNT SNAPSHOT | %s", mode)

        # ── Wallet ────────────────────────────────────────────────────────────
        try:
            wallet = await self._client.get_balance()
            self._log.info(
                "  Wallet   | equity=%.2f  available=%.2f  margin=%.2f USDT",
                wallet.total_equity, wallet.available_balance, wallet.used_margin,
            )
        except Exception as exc:
            self._log.warning("  Wallet   | could not fetch: %s", exc)
            wallet = None

        # ── Bot config ────────────────────────────────────────────────────────
        self._log.info(
            "  Config   | risk=%.1f%%  lev=%dx  RR=%.1f  pip=%.6f  expiry=%dmin  lookback=%d",
            cfg.risk_per_trade * 100, cfg.leverage, cfg.rr, cfg.effective_pip_size(),
            cfg.pending_expiry_min, cfg.lookback_bars,
        )
        if wallet is not None and wallet.total_equity > 0:
            risk_cash = wallet.total_equity * cfg.risk_per_trade
            self._log.info(
                "  Per trade | %.2f USDT risk  (%.1f%% of %.2f equity)",
                risk_cash, cfg.risk_per_trade * 100, wallet.total_equity,
            )
        self._log.info(
            "  Mode     | dry_run=%s  replace_pending=%s  position_mode=%s",
            cfg.dry_run, cfg.replace_pending, cfg.position_mode,
        )

        # ── Open positions ────────────────────────────────────────────────────
        positions = []
        try:
            positions = await self._client.get_positions(cfg.symbol)
            if positions:
                self._log.info("  Positions | %d open:", len(positions))
                for p in positions:
                    self._log.info(
                        "    → %s  size=%s  entry=%.2f  uPnL=%.2f USDT  lev=%sx",
                        p.side.value.upper(), p.size, p.entry_price,
                        p.unrealized_pnl, int(p.leverage),
                    )
            else:
                self._log.info("  Positions | none open")
        except Exception as exc:
            self._log.warning("  Positions | could not fetch: %s", exc)

        # ── Pending orders ────────────────────────────────────────────────────
        our_orders = []
        try:
            raw_orders = await self._client.get_open_stop_orders(cfg.symbol)
            our_orders = [o for o in raw_orders
                          if o.get("orderLinkId", "").startswith("ema-")]
            if our_orders:
                self._log.info("  Pending   | %d order(s):", len(our_orders))
                for o in our_orders:
                    self._log.info(
                        "    → %s  side=%s  trigger=%s  sl=%s  tp=%s  qty=%s  status=%s",
                        o.get("orderLinkId", "?"), o.get("side", "?"),
                        o.get("triggerPrice", "?"), o.get("stopLoss", "?"),
                        o.get("takeProfit", "?"), o.get("qty", "?"),
                        o.get("orderStatus", "?"),
                    )
            else:
                self._log.info("  Pending   | none")
        except Exception as exc:
            self._log.warning("  Pending   | could not fetch: %s", exc)

        self._log.info(sep)

        # ── Journal: bot_start + account_snapshot ─────────────────────────────
        self._journal.log(
            "bot_start",
            mode=mode,
            dry_run=cfg.dry_run,
            symbol=cfg.symbol,
            risk_pct=cfg.risk_per_trade * 100,
            leverage=cfg.leverage,
            rr=cfg.rr,
            pip_size=cfg.effective_pip_size(),
            expiry_min=cfg.pending_expiry_min,
            expiry_bars=cfg.expiry_bars,
            lookback_bars=cfg.lookback_bars,
            position_mode=cfg.position_mode,
            magic=cfg.effective_magic(),
            journal_path=str(self._journal.path),
        )
        self._journal.log(
            "account_snapshot",
            equity=wallet.total_equity if wallet else None,
            available=wallet.available_balance if wallet else None,
            used_margin=wallet.used_margin if wallet else None,
            open_positions=[
                {"side": p.side.value, "size": p.size, "entry": p.entry_price,
                 "upnl": p.unrealized_pnl}
                for p in positions
            ],
            pending_orders=[
                {"link_id": o.get("orderLinkId"), "side": o.get("side"),
                 "trigger": o.get("triggerPrice"), "status": o.get("orderStatus")}
                for o in our_orders
            ],
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """
        Production trading loop — mirrors the MT5 while True: block exactly.

        Loop fires immediately on start, then sleeps until the next M5 candle boundary.
        The duplicate-candle guard prevents double-processing on restart.
        """
        self._log.info("━" * 55)
        self._log.info("TRADING LOOP STARTED")
        self._log.info("━" * 55)

        while self._running:
            # ── Duplicate-candle guard (peek cheaply) ─────────────────────────
            current_candle = await self._fetcher.peek_latest_candle_time()

            if self._state.is_duplicate_candle(current_candle):
                self._log.info("Candle %s already processed — waiting for next.", current_candle)
                await self._sleep_until_next_candle()
                continue

            # ── Run full strategy cycle ───────────────────────────────────────
            self._state.cycles_total += 1
            try:
                await self._run_cycle()
                self._state.mark_candle_processed(current_candle)
            except Exception as exc:
                self._state.cycles_error += 1
                self._log.exception("ERROR in strategy cycle #%d: %s", self._state.cycles_total, exc)
                self._journal.log("cycle_error", cycle=self._state.cycles_total, error=str(exc))

            # ── Sleep until next M5 boundary ──────────────────────────────────
            if self._running:
                await self._sleep_until_next_candle()

    # ── Single strategy cycle ─────────────────────────────────────────────────

    async def _run_cycle(self) -> None:
        """
        One complete strategy cycle:
            fetch → context → signals → reconcile → sync orders

        Mirrors the MT5 run_cycle() function exactly.
        """
        cfg = self._cfg
        cycle_num = self._state.cycles_total
        self._log.info("── CYCLE #%d ──────────────────────────────────────", cycle_num)

        self._journal.log("cycle_start", cycle=cycle_num)

        # ── 1. Fetch OHLCV ────────────────────────────────────────────────────
        m5, h1 = await self._fetcher.fetch_closed_frames()

        # ── 2. Build context ──────────────────────────────────────────────────
        m5_ctx = _build_context(m5, h1)

        h1_trend_tail = {str(k): v for k, v in m5_ctx["trend"].tail(3).to_dict().items()}
        self._log.info("H1 trend (last 3): %s", h1_trend_tail)
        self._log.info("M5 trend dist: %s", m5_ctx["trend"].value_counts(dropna=False).to_dict())

        self._journal.log(
            "data_fetched",
            cycle=cycle_num,
            m5_bars=len(m5),
            h1_bars=len(h1),
            current_bar=str(m5_ctx.index[-1]),
            h1_trend=h1_trend_tail,
        )

        # ── 3. Generate signals ───────────────────────────────────────────────
        signals = list_setup_signals(
            m5_ctx,
            start_balance=cfg.start_balance,
            lookback_bars=cfg.lookback_bars,
            pending_offset_ticks=cfg.pending_offset_ticks,
            pip_size=cfg.effective_pip_size(),
            rr=cfg.rr,
            risk_per_trade=cfg.risk_per_trade,
            leverage=cfg.leverage,
        )

        self._log.info("Signals: %d total", len(signals))
        if not signals.empty:
            last = signals.iloc[-1]
            self._log.info(
                "Last signal | side=%s entry=%.5f sl=%.5f tp=%.5f qty=%.4f bar=%s",
                last["side"], last["entry"], last["sl"], last["tp"],
                last["qty"], last["signal_bar_time"],
            )
            self._log_signal_to_csv(signals.iloc[-1], m5_ctx, action="generated")
            self._journal.log(
                "signal",
                cycle=cycle_num,
                total_signals=len(signals),
                side=str(last["side"]),
                entry=float(last["entry"]),
                sl=float(last["sl"]),
                tp=float(last["tp"]),
                qty=float(last["qty"]),
                signal_bar=str(last["signal_bar_time"]),
                trend=str(last.get("trend", "")),
            )
        else:
            self._journal.log("signal", cycle=cycle_num, total_signals=0)

        # ── 4. Reconcile exchange state ───────────────────────────────────────
        await self._reconciler.reconcile_cycle()

        # ── 5. Get live balance ───────────────────────────────────────────────
        wallet = await self._client.get_balance()
        balance = wallet.total_equity
        self._log.info(
            "Balance: %.2f USDT | available=%.2f | margin=%.2f USDT",
            balance, wallet.available_balance, wallet.used_margin,
        )
        self._journal.log(
            "balance",
            cycle=cycle_num,
            equity=balance,
            available=wallet.available_balance,
            used_margin=wallet.used_margin,
        )

        # ── 6. Check for open position ────────────────────────────────────────
        has_position = await self._client.has_open_position(cfg.symbol)
        self._log.info("Open position: %s", has_position)
        self._journal.log("position_check", cycle=cycle_num, has_position=has_position)

        # ── 7. Sync pending orders (core logic — identical to MT5) ────────────
        await self._order_mgr.sync_pending_orders(
            has_position=has_position,
            signals_df=signals,
            m5_ctx=m5_ctx,
            balance=balance,
            wallet=wallet,
            risk_per_trade=cfg.risk_per_trade,
            pip_size=cfg.effective_pip_size(),
            pending_expiry_min=cfg.pending_expiry_min,
            entry_tf_minutes=cfg.entry_tf_minutes,
        )

        self._journal.log("cycle_complete", cycle=cycle_num)

    # ── Sleep helpers ─────────────────────────────────────────────────────────

    async def _sleep_until_next_candle(self) -> None:
        """
        Sleep until the next M5 candle boundary (UTC) + 1-second buffer.
        Identical logic to MT5 sleep_until_next_candle().
        Exits early if shutdown_event is set.
        """
        tf_minutes = self._cfg.entry_tf_minutes
        now = datetime.now(timezone.utc)
        total_seconds = now.minute * 60 + now.second
        candle_seconds = tf_minutes * 60
        next_close = ((total_seconds // candle_seconds) + 1) * candle_seconds
        wait_seconds = next_close - total_seconds

        if wait_seconds <= 0:
            wait_seconds += candle_seconds

        wait_seconds += 1  # 1s buffer to ensure candle is closed

        from datetime import timedelta
        self._log.info(
            "Sleeping %ds → next %dm candle at %s UTC",
            wait_seconds,
            tf_minutes,
            (now + timedelta(seconds=wait_seconds)).strftime("%H:%M:%S"),
        )

        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(),
                timeout=wait_seconds,
            )
            self._log.info("Shutdown event received during sleep.")
            self._running = False
        except asyncio.TimeoutError:
            pass  # Normal: sleep expired, continue loop

    # ── Teardown ──────────────────────────────────────────────────────────────

    async def _teardown(self) -> None:
        """Graceful shutdown — cancel any pending orders if configured."""
        self._log.info("GRACEFUL SHUTDOWN | state: %s", self._state.summary())

    # ── Signal log helper ─────────────────────────────────────────────────────

    def _log_signal_to_csv(self, signal_row, m5_ctx, action: str = "generated") -> None:
        """Log last signal to audit CSV."""
        if not self._signal_log.is_enabled():
            return
        cfg = self._cfg
        try:
            self._signal_log.log_signal(
                symbol=cfg.symbol,
                magic=cfg.effective_magic(),
                signal_bar_time=signal_row["signal_bar_time"],
                trend=str(signal_row.get("trend", "")),
                side=str(signal_row["side"]),
                model_entry=float(signal_row["entry"]),
                model_sl=float(signal_row["sl"]),
                model_tp=float(signal_row["tp"]),
                setup_qty=float(signal_row["qty"]),
                balance=cfg.start_balance,
                risk_per_trade=cfg.risk_per_trade,
                pip_size=cfg.effective_pip_size(),
                lookback_bars=cfg.lookback_bars,
                rr=cfg.rr,
                pending_offset_ticks=cfg.pending_offset_ticks,
                dry_run=cfg.dry_run,
                action=action,
            )
        except Exception as exc:
            self._log.debug("Signal log error (non-fatal): %s", exc)


# ── Context building (pure — identical to MT5 build_context()) ────────────────

def _build_context(m5: pd.DataFrame, h1: pd.DataFrame) -> pd.DataFrame:
    """
    Add EMAs on both frames, merge H1 trend onto M5.
    Identical to MT5 build_context() — no lookahead bias.
    """
    m5 = add_emas(m5)
    h1 = add_emas(h1)
    return merge_h1_trend_onto_m5(m5, h1)
