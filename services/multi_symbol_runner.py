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
from typing import List

from config.settings import Settings
from services.trading_service import TradingService

log = logging.getLogger(__name__)


class MultiSymbolRunner:
    """
    Orchestrate multiple TradingService instances in the same event loop.

    Each service runs independently: different candle times, different orders.
    Errors in one symbol do not abort others.
    """

    def __init__(self, symbols: List[str], base_settings: Settings) -> None:
        self._symbols = symbols
        self._base = base_settings
        self._services: List[TradingService] = []

    async def run(self) -> None:
        """Start all symbol services and run until all complete (or stop() is called)."""
        log.info("MultiSymbolRunner starting | symbols=%s", self._symbols)

        tasks = []
        for symbol in self._symbols:
            # Clone settings with overridden symbol
            # pydantic models are immutable so we copy + override
            cfg_dict = self._base.model_dump()
            cfg_dict["symbol"] = symbol
            # Magic must be per-symbol (auto-derived from symbol)
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

        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except Exception as exc:
            log.exception("MultiSymbolRunner error: %s", exc)
            await self.stop()
            raise

    async def stop(self) -> None:
        """Request graceful shutdown of all symbol services."""
        for svc in self._services:
            await svc.stop()
        log.info("MultiSymbolRunner stopped all services.")
