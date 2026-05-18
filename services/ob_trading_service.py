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
    OB_WARMUP_BARS,
    OB_EXPIRY_BARS,
    DEFAULT_RR as OB_DEFAULT_RR,
    SL_BUFFER as OB_SL_BUFFER,
)
from strategy.ob_signals import (
    list_ob_signals_enhanced,
    OBSignalConfig,
    get_passed_signals,
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

        self._ob_config   = self._build_ob_config(settings)
        # H1 data is only needed when HTF bias or H1-level BOS is requested.
        self._needs_h1    = settings.ob_require_htf_bias

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

    # ── OB config builder ─────────────────────────────────────────────────────

    @staticmethod
    def _build_ob_config(cfg: "Settings") -> OBSignalConfig:
        return OBSignalConfig(
            rr=cfg.rr,
            session_filter_enabled=cfg.ob_session_filter,
            allow_london=cfg.ob_allow_london,
            allow_new_york=cfg.ob_allow_new_york,
            allow_overlap=cfg.ob_allow_overlap,
            require_bos=cfg.ob_require_bos,
            require_fvg=cfg.ob_require_fvg,
            volume_filter_enabled=cfg.ob_volume_filter,
            regime_filter_enabled=cfg.ob_regime_filter,
            require_htf_bias=cfg.ob_require_htf_bias,
        )

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
            # fee_adjusted_sizing=True: qty and TP are adjusted so that
            # net_loss at SL == risk_usdt and net_win at TP == risk_usdt * rr,
            # both after entry + exit fees. This matches the fee deduction
            # model used in notebooks_ob/03, 11, 12, 13 backtests.
            fee_adjusted_sizing=True,
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

        add_symbol_file_handler(cfg.symbol, log_dir=cfg.event_log_dir, json_output=cfg.log_json)
        await self._reconciler.reconcile_on_startup()

        oc = self._ob_config
        self._log.info(
            "Initialized OB bot | magic=%d pip_size=%.6f expiry_min=%d "
            "rr=%.1f risk=%.2f USDT sl_buffer=%.4f fee_adj=True atr_floor=False",
            cfg.effective_magic(),
            cfg.effective_pip_size(),
            cfg.pending_expiry_min,
            cfg.rr,
            cfg.risk_fixed_usdt,
            OB_SL_BUFFER,
        )
        self._log.info(
            "OB filters | session=%s bos=%s fvg=%s volume=%s regime=%s htf_bias=%s",
            oc.session_filter_enabled,
            oc.require_bos,
            oc.require_fvg,
            oc.volume_filter_enabled,
            oc.regime_filter_enabled,
            oc.require_htf_bias,
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
            "  SL/TP    | fee_adj=True  fee_tighten=False  atr_floor=False"
            "  sl_buffer=%.4f  entry_fee=%.4f%%  exit_fee=%.4f%%",
            OB_SL_BUFFER, cfg.entry_fee_rate * 100, cfg.exit_fee_rate * 100,
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

        oc = self._ob_config
        self._log.info(
            "  Filters  | session=%s bos=%s fvg=%s vol=%s regime=%s htf=%s",
            oc.session_filter_enabled, oc.require_bos, oc.require_fvg,
            oc.volume_filter_enabled, oc.regime_filter_enabled, oc.require_htf_bias,
        )
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
            ob_filters={
                "session": oc.session_filter_enabled,
                "bos":     oc.require_bos,
                "fvg":     oc.require_fvg,
                "volume":  oc.volume_filter_enabled,
                "regime":  oc.regime_filter_enabled,
                "htf_bias": oc.require_htf_bias,
            },
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

        # ── 1. Fetch price data ──────────────────────────────────────────────
        h1: Optional[pd.DataFrame] = None
        if self._needs_h1:
            m5, h1 = await self._fetcher.fetch_closed_frames()
        else:
            m5 = await self._fetcher.fetch_m5_frame()

        self._log.info(
            "M5 bars: %d  current_bar=%s%s",
            len(m5), m5.index[-1],
            f"  H1 bars: {len(h1)}" if h1 is not None else "",
        )
        self._journal.log(
            "data_fetched",
            cycle=cycle_num,
            m5_bars=len(m5),
            h1_bars=len(h1) if h1 is not None else 0,
            current_bar=str(m5.index[-1]),
        )

        # ── 2. Generate OB signals ───────────────────────────────────────────
        all_signals = list_ob_signals_enhanced(
            m5,
            h1=h1,
            risk_cash=cfg.risk_fixed_usdt,
            config=self._ob_config,
        )
        signals = get_passed_signals(all_signals)

        n_total    = len(all_signals)
        n_passed   = len(signals)
        n_rejected = n_total - n_passed
        self._log.info(
            "OB Signals: %d total | %d passed filters | %d rejected",
            n_total, n_passed, n_rejected,
        )

        # Log a sample of rejection reasons when filters are active
        if n_rejected > 0 and (
            self._ob_config.session_filter_enabled
            or self._ob_config.require_bos
            or self._ob_config.require_fvg
            or self._ob_config.volume_filter_enabled
            or self._ob_config.regime_filter_enabled
        ):
            reasons = (
                all_signals[~all_signals["passed_all_filters"]]["filter_reason"]
                .value_counts()
                .head(3)
            )
            for reason, count in reasons.items():
                self._log.info("  rejected ×%d: %s", count, reason)

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
