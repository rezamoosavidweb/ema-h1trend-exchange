"""
Multi-symbol runner — runs one TradingService per symbol concurrently.

Each symbol gets its own:
  - BotState (candle guard, pending tracking)
  - DataFetcher
  - OrderManager
  - Reconciler

They share the same BybitClient (rate-limit aware) and Settings base.

Usage:
    runner = MultiSymbolRunner(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    await runner.run()

Or from CLI:
    python app/main.py --symbols BTCUSDT,ETHUSDT,SOLUSDT
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, List, Optional

from config.settings import Settings
from services.trading_service import TradingService

if TYPE_CHECKING:
    from telegram_bot.bot import TelegramBot
    from services.ws_order_notifier import WsOrderNotifier

log = logging.getLogger(__name__)


class MultiSymbolRunner:
    """
    Orchestrate multiple TradingService instances in the same event loop.

    Each service runs independently: different candle times, different orders.
    Errors in one symbol do not abort others.
    """

    def __init__(
        self,
        symbols: List[str],
        base_settings: Settings,
        telegram_bot: Optional["TelegramBot"] = None,
        ws_notifier: Optional["WsOrderNotifier"] = None,
    ) -> None:
        self._symbols = symbols
        self._base = base_settings
        self._services: List[TradingService] = []
        self._telegram_bot = telegram_bot
        self._ws_notifier = ws_notifier

    @property
    def services(self) -> List[TradingService]:
        return list(self._services)

    async def run(self) -> None:
        """Start all symbol services and run until all complete (or stop() is called)."""
        log.info("MultiSymbolRunner starting | symbols=%s", self._symbols)

        tasks = []
        for symbol in self._symbols:
            # Clone settings with overridden symbol
            cfg_dict = self._base.model_dump()
            cfg_dict["symbol"] = symbol
            # Magic and pip_size must be per-symbol (auto-derived)
            cfg_dict["magic"] = None
            cfg_dict["pip_size"] = None

            from config.settings import Settings as S
            sym_cfg = S(**cfg_dict)

            svc = TradingService(sym_cfg)
            self._services.append(svc)
            tasks.append(asyncio.create_task(
                svc.run(),
                name=f"trading-{symbol}",
            ))

        # Wire telegram bot services now that TradingService instances are ready
        if self._telegram_bot is not None:
            self._telegram_bot._services = {s.symbol: s for s in self._services}
            tasks.append(asyncio.create_task(
                self._telegram_bot.run(),
                name="telegram-bot",
            ))

        # WebSocket order notifier — one shared instance for all symbols
        if self._ws_notifier is not None:
            tasks.append(asyncio.create_task(
                self._ws_notifier.run(),
                name="ws-order-notifier",
            ))

        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except Exception as exc:
            log.exception("MultiSymbolRunner error: %s", exc)
            await self.stop()
            raise

    async def stop(self) -> None:
        """Request graceful shutdown of all symbol services and telegram bot."""
        for svc in self._services:
            await svc.stop()
        if self._telegram_bot is not None:
            await self._telegram_bot.stop()
        if self._ws_notifier is not None:
            await self._ws_notifier.stop()
        log.info("MultiSymbolRunner stopped all services.")
