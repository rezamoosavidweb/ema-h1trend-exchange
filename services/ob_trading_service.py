"""
Order Block Reaction Trading Service.

Drop-in replacement for TradingService that runs the Order Block strategy
(from notebooks/08_order_block_reaction_crypto.ipynb) instead of EMA trend.

Key differences from TradingService:
  - Only M5 data is fetched (no H1 trend needed)
  - Signals from list_ob_signals() instead of list_setup_signals()
  - Order link IDs use "ob-" prefix for isolation
  - No H1 trend tracking
"""

from __future__ import annotations

import asyncio
import logging
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
from strategy.ob_core import (
    list_ob_signals,
    OB_WARMUP_BARS,
    OB_EXPIRY_BARS,
    DEFAULT_RR as OB_DEFAULT_RR,
    SL_BUFFER as OB_SL_BUFFER,
)
from telemetry.logging import SymbolAdapter, add_symbol_file_handler

log = logging.getLogger(__name__)

OB_LINK_PREFIX = "ob"


class OBTradingService:
    """
    Orchestrates the Order Block Reaction strategy for one symbol.

    Construction:
        service = OBTradingService(settings)
        await service.run()

    Graceful shutdown:
        await service.stop()
    """

    def __init__(self, settings: Settings) -> None:
        self._cfg = settings
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._log = SymbolAdapter(log, settings.symbol)

        self._client = BybitClient(
            api_key=settings.bybit_api_key,
            api_secret=settings.bybit_api_secret,
            testnet=settings.bybit_testnet,
            demo=settings.bybit_demo,
            max_retries=settings.max_retries,
            retry_base_delay=settings.retry_base_delay,
            retry_max_delay=settings.retry_max_delay,
        )

        self._state       = BotState(symbol=settings.symbol)
        self._signal_log  = SignalLogger(settings.signal_log_csv)
        self._journal     = EventJournal(settings.symbol, log_dir=settings.event_log_dir)
        if self._journal.enabled:
            self._log.info("Event journal → %s", self._journal.path)

        self._info:       Optional[InstrumentInfo] = None
        self._fetcher:    Optional[DataFetcher]    = None
        self._order_mgr:  Optional[OrderManager]   = None
        self._reconciler: Optional[OrderReconciler] = None

    # ── Public properties ─────────────────────────────────────────────────────

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
        self._running = True
        try:
            await self._initialize()
            await self._loop()
        except asyncio.CancelledError:
            self._log.info("OBTradingService cancelled.")
        except Exception as exc:
            self._log.exception("Fatal error in OBTradingService: %s", exc)
            raise
        finally:
            await self._teardown()

    async def stop(self) -> None:
        self._log.info("Shutdown requested.")
        self._running = False
        self._shutdown_event.set()

    # ── Initialization ────────────────────────────────────────────────────────

    async def _initialize(self) -> None:
        cfg = self._cfg
        self._log.info(
            "Initializing OB bot | testnet=%s leverage=%dx dry_run=%s rr=%.1f",
            cfg.bybit_testnet, cfg.leverage, cfg.dry_run, cfg.rr,
        )

        self._info = await self._client.get_instrument_info(cfg.symbol)
        self._log.info(
            "Instrument | tickSize=%s qtyStep=%s minQty=%s maxQty=%s",
            self._info.tick_size, self._info.qty_step,
            self._info.min_qty, self._info.max_qty,
        )

        if not cfg.dry_run:
            await self._client.set_leverage(cfg.symbol, cfg.leverage)
            mode_int = 0 if cfg.position_mode == "one_way" else 3
            await self._client.set_position_mode(cfg.symbol, mode_int)

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
            entry_fee_rate=cfg.entry_fee_rate,
            exit_fee_rate=cfg.exit_fee_rate,
            # Notebook (08_order_block_reaction_crypto.ipynb) has no fee-based SL/TP
            # adjustments or ATR floor — keep False so bot output matches notebook exactly.
            fee_adjusted_sizing=False,
            fee_tighten_sl=False,
            atr_min_sl_enabled=False,
            atr_period=cfg.atr_period,
            atr_min_sl_multiplier=cfg.atr_min_sl_multiplier,
            rr=cfg.rr,
            link_prefix=OB_LINK_PREFIX,
        )

        self._reconciler = OrderReconciler(
            client=self._client,
            state=self._state,
            magic=cfg.effective_magic(),
            symbol=cfg.symbol,
            journal=self._journal,
            link_prefix=OB_LINK_PREFIX,
        )

        add_symbol_file_handler(cfg.symbol, log_dir="logs", json_output=cfg.log_json)
        await self._reconciler.reconcile_on_startup()

        self._log.info(
            "Initialized OB bot | magic=%d pip_size=%.6f expiry_min=%d "
            "rr=%.1f risk=%.2f USDT sl_buffer=%.4f fee_adj=False atr_floor=False",
            cfg.effective_magic(),
            cfg.effective_pip_size(),
            cfg.pending_expiry_min,
            cfg.rr,
            cfg.risk_fixed_usdt,
            OB_SL_BUFFER,
        )

        await self._log_account_summary()

    # ── Account summary ───────────────────────────────────────────────────────

    async def _log_account_summary(self) -> None:
        cfg  = self._cfg
        sep  = "─" * 55
        mode = "DEMO" if cfg.bybit_demo else ("TESTNET" if cfg.bybit_testnet else "LIVE")

        self._log.info(sep)
        self._log.info("ACCOUNT SNAPSHOT | %s | Order Block Strategy", mode)

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

        # ── Config ────────────────────────────────────────────────────────────
        self._log.info(
            "  Config   | risk=%.2f USDT  lev=%dx  RR=%.1f  pip=%.6f  expiry=%dmin",
            cfg.risk_fixed_usdt, cfg.leverage, cfg.rr,
            cfg.effective_pip_size(), cfg.pending_expiry_min,
        )
        self._log.info(
            "  SL/TP    | fee_tighten=False  atr_floor=False  sl_buffer=%.4f"
            "  (notebook: RR=%.1f  SL_BUFFER=%.1f)",
            OB_SL_BUFFER, OB_DEFAULT_RR, OB_SL_BUFFER,
        )
        if wallet is not None and wallet.total_equity > 0:
            self._log.info(
                "  Per trade | %.2f USDT risk  (%.2f%% of %.2f equity)",
                cfg.risk_fixed_usdt,
                cfg.risk_fixed_usdt / wallet.total_equity * 100,
                wallet.total_equity,
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

        # ── Pending orders (ob- prefix) ───────────────────────────────────────
        our_orders = []
        try:
            raw = await self._client.get_open_stop_orders(cfg.symbol)
            our_orders = [o for o in raw
                          if o.get("orderLinkId", "").startswith(f"{OB_LINK_PREFIX}-")]
            if our_orders:
                self._log.info("  Pending   | %d OB order(s):", len(our_orders))
                for o in our_orders:
                    self._log.info(
                        "    → %s  side=%s  trigger=%s  sl=%s  tp=%s  qty=%s  status=%s",
                        o.get("orderLinkId", "?"), o.get("side", "?"),
                        o.get("triggerPrice", "?"), o.get("stopLoss", "?"),
                        o.get("takeProfit", "?"), o.get("qty", "?"),
                        o.get("orderStatus", "?"),
                    )
            else:
                self._log.info("  Pending   | none (ob- prefix)")
        except Exception as exc:
            self._log.warning("  Pending   | could not fetch: %s", exc)

        self._log.info(sep)

        # ── Journal ───────────────────────────────────────────────────────────
        self._journal.log(
            "bot_start",
            mode=mode,
            strategy="order_block_reaction",
            dry_run=cfg.dry_run,
            symbol=cfg.symbol,
            risk_usdt=cfg.risk_fixed_usdt,
            leverage=cfg.leverage,
            rr=cfg.rr,
            pip_size=cfg.effective_pip_size(),
            expiry_min=cfg.pending_expiry_min,
            expiry_bars=cfg.expiry_bars,
            position_mode=cfg.position_mode,
            magic=cfg.effective_magic(),
            link_prefix=OB_LINK_PREFIX,
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
                 "trigger": o.get("triggerPrice"), "sl": o.get("stopLoss"),
                 "tp": o.get("takeProfit"), "qty": o.get("qty"),
                 "status": o.get("orderStatus")}
                for o in our_orders
            ],
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        self._log.info("━" * 55)
        self._log.info("OB [ORDER BLOCK] TRADING LOOP STARTED")
        self._log.info("━" * 55)

        while self._running:
            current_candle = await self._fetcher.peek_latest_candle_time()

            if self._state.is_duplicate_candle(current_candle):
                self._log.info("Candle %s already processed — waiting for next.", current_candle)
                await self._sleep_until_next_candle()
                continue

            self._state.cycles_total += 1
            try:
                await self._run_cycle()
                self._state.mark_candle_processed(current_candle)
            except Exception as exc:
                self._state.cycles_error += 1
                self._log.exception(
                    "ERROR in OB cycle #%d: %s", self._state.cycles_total, exc
                )
                self._journal.log(
                    "cycle_error", cycle=self._state.cycles_total, error=str(exc)
                )

            if self._running:
                await self._sleep_until_next_candle()

    # ── Single strategy cycle ─────────────────────────────────────────────────

    async def _run_cycle(self) -> None:
        cfg       = self._cfg
        cycle_num = self._state.cycles_total
        self._log.info("── OB CYCLE #%d ──────────────────────────────────────", cycle_num)
        self._journal.log("cycle_start", cycle=cycle_num)

        # ── 1. Fetch M5 only ─────────────────────────────────────────────────
        m5 = await self._fetcher.fetch_m5_frame()

        self._log.info("M5 bars: %d  current_bar=%s", len(m5), m5.index[-1])
        self._journal.log(
            "data_fetched",
            cycle=cycle_num,
            m5_bars=len(m5),
            current_bar=str(m5.index[-1]),
        )

        # ── 2. Generate OB signals ───────────────────────────────────────────
        # Matches notebook run_ob_backtest exactly:
        #   Bullish: entry=ob_high  sl=ob_low-SL_BUFFER   tp=entry+(entry-sl)*rr
        #   Bearish: entry=ob_low   sl=ob_high+SL_BUFFER   tp=entry-(sl-entry)*rr
        signals = list_ob_signals(
            m5,
            risk_cash=cfg.risk_fixed_usdt,
            rr=cfg.rr,
            sl_buffer=OB_SL_BUFFER,
        )

        self._log.info("OB Signals: %d total", len(signals))
        if not signals.empty:
            last = signals.iloc[-1]
            sl_dist = abs(float(last["entry"]) - float(last["sl"]))
            actual_rr = (
                abs(float(last["tp"]) - float(last["entry"])) / sl_dist
                if sl_dist > 0 else 0.0
            )
            self._log.info(
                "Last OB signal | side=%s ob_type=%s entry=%.5f sl=%.5f tp=%.5f "
                "sl_dist=%.5f rr=%.2f qty=%.4f bar=%s",
                last["side"], last.get("ob_type", "?"),
                last["entry"], last["sl"], last["tp"],
                sl_dist, actual_rr,
                last["qty"], last["signal_bar_time"],
            )
            self._log_signal_to_csv(last, action="generated")
            self._journal.log(
                "signal",
                cycle=cycle_num,
                total_signals=len(signals),
                side=str(last["side"]),
                entry=float(last["entry"]),
                sl=float(last["sl"]),
                tp=float(last["tp"]),
                sl_dist=float(sl_dist),
                rr=float(actual_rr),
                qty=float(last["qty"]),
                signal_bar=str(last["signal_bar_time"]),
                ob_type=str(last.get("ob_type", "")),
            )
        else:
            self._log.info("No OB signals on current data.")
            self._journal.log("no_signal", cycle=cycle_num, reason="no_ob_setup")

        # ── 3. Reconcile exchange state ──────────────────────────────────────
        await self._reconciler.reconcile_cycle()

        # ── 4. Live balance ──────────────────────────────────────────────────
        wallet  = await self._client.get_balance()
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

        # ── 5. Position check ────────────────────────────────────────────────
        has_position = await self._client.has_open_position(cfg.symbol)
        self._log.info("Open position: %s", has_position)
        self._journal.log("position_check", cycle=cycle_num, has_position=has_position)

        # ── 6. OB invalidation guard ─────────────────────────────────────────
        # Notebook: OB is cancelled when price closes through ob_low/ob_high.
        # ob_core.py handles this naturally — if the OB zone is broken, that
        # signal no longer appears in the output.  When signals are empty AND
        # we hold a pending, it means the latest OB was invalidated → cancel.
        if signals.empty and self._state.has_pending():
            self._log.info(
                "No active OB setups — OB zone invalidated — cancelling pending order."
            )
            self._journal.log("ob_invalidated", cycle=cycle_num,
                              reason="no_active_ob_signals")
            await self._order_mgr.cancel_pending_order(reason="ob_invalidated")

        # ── 7. Sync orders ───────────────────────────────────────────────────
        # Notebook expiry: OB is valid for OB_EXPIRY_BARS (100) M5 candles
        # from OB formation, not from signal time.  Use OB_EXPIRY_BARS as the
        # pending lifetime so the order survives the full OB window.
        # Natural invalidation (step 6 above) cancels it sooner when needed.
        ob_expiry_min = OB_EXPIRY_BARS * cfg.entry_tf_minutes  # 100 * 5 = 500 min
        await self._order_mgr.sync_pending_orders(
            has_position=has_position,
            signals_df=signals,
            m5_ctx=m5,
            balance=balance,
            wallet=wallet,
            risk_cash=cfg.risk_fixed_usdt,
            pip_size=cfg.effective_pip_size(),
            pending_expiry_min=ob_expiry_min,
            entry_tf_minutes=cfg.entry_tf_minutes,
        )

        self._journal.log("cycle_complete", cycle=cycle_num)

    # ── Sleep helpers ─────────────────────────────────────────────────────────

    async def _sleep_until_next_candle(self) -> None:
        tf_minutes = self._cfg.entry_tf_minutes
        now        = datetime.now(timezone.utc)
        total_s    = now.minute * 60 + now.second
        candle_s   = tf_minutes * 60
        next_close = ((total_s // candle_s) + 1) * candle_s
        wait_s     = next_close - total_s
        if wait_s <= 0:
            wait_s += candle_s
        wait_s += 1  # 1s buffer

        from datetime import timedelta
        self._log.info(
            "Sleeping %ds → next %dm candle at %s UTC",
            wait_s, tf_minutes,
            (now + timedelta(seconds=wait_s)).strftime("%H:%M:%S"),
        )
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=wait_s)
            self._log.info("Shutdown event received during sleep.")
            self._running = False
        except asyncio.TimeoutError:
            pass

    # ── Teardown ──────────────────────────────────────────────────────────────

    async def _teardown(self) -> None:
        self._log.info("GRACEFUL SHUTDOWN | state: %s", self._state.summary())

    # ── Signal log helper ─────────────────────────────────────────────────────

    def _log_signal_to_csv(self, signal_row, action: str = "generated") -> None:
        if not self._signal_log.is_enabled():
            return
        cfg = self._cfg
        try:
            ob_type = str(signal_row.get("ob_type", ""))
            self._signal_log.log_signal(
                symbol=cfg.symbol,
                magic=cfg.effective_magic(),
                signal_bar_time=signal_row["signal_bar_time"],
                trend=ob_type,           # ob_type used in trend field for OB strategy
                side=str(signal_row["side"]),
                model_entry=float(signal_row["entry"]),
                model_sl=float(signal_row["sl"]),
                model_tp=float(signal_row["tp"]),
                setup_qty=float(signal_row["qty"]),
                balance=cfg.start_balance,
                risk_cash=cfg.risk_fixed_usdt,
                pip_size=cfg.effective_pip_size(),
                lookback_bars=0,         # not applicable for OB strategy
                rr=cfg.rr,
                pending_offset_ticks=0.0,  # not applicable for OB strategy
                dry_run=cfg.dry_run,
                action=action,
            )
        except Exception as exc:
            self._log.debug("OB signal log error (non-fatal): %s", exc)
