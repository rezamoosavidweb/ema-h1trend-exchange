"""
Daily realised-loss circuit breaker.

Once the account's realised PnL for the current UTC day drops to/below
`-limit`, the guard blocks *new* entries for the rest of that day. Existing
positions are left untouched — their SL/TP still run to completion. The guard
resets automatically at UTC midnight (it always sums from today's 00:00).

Why a daily loss-stop (and not an ADX/regime filter)?
-----------------------------------------------------
On the live demo data (2026-05-30 → 06-03) a per-trade ADX filter *increased*
losses, because winners/losers were dominated by the day's regime, not by ADX.
A daily-loss stop, by contrast, caps the bleed on chop days while leaving a
trending day fully intact (e.g. 2026-06-02 ran to +565 USDT and never tripped a
3R / -60 USDT limit). Net over the window improved from -368 to +281 USDT.

PnL source
----------
Today's realised PnL is read from Bybit's closed-PnL endpoint — authoritative
and self-correcting — rather than the per-symbol `position_closed` logs, which
can under-report (NaN / pnl=0) when the polling reconciler misses a close.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Tuple

log = logging.getLogger(__name__)


class DailyLossGuard:
    """Account-wide, shared across all symbols (PnL is portfolio-level)."""

    def __init__(self, client, limit_usdt: float, refresh_s: float = 60.0) -> None:
        self._client = client
        self.limit = abs(float(limit_usdt))     # block when day_pnl <= -limit
        self._refresh_s = refresh_s
        self._cached_pnl = 0.0
        self._cached_at = 0.0                    # monotonic seconds
        self._cached_day = ""                    # UTC YYYY-MM-DD of the cache

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @staticmethod
    def _utc_day_start_ms() -> int:
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp() * 1000)

    async def day_pnl(self) -> float:
        """Today's realised PnL (USDT). Cached for `refresh_s` seconds."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now = time.monotonic()
        if today == self._cached_day and (now - self._cached_at) < self._refresh_s:
            return self._cached_pnl

        total = 0.0
        try:
            records = await self._client.get_closed_pnl(
                start_ms=self._utc_day_start_ms(),
                end_ms=int(time.time() * 1000),
                limit=100,
            )
            for r in records or []:
                try:
                    total += float(r.get("closedPnl", 0) or 0)
                except (TypeError, ValueError):
                    pass
        except Exception as exc:
            # Fail open: a transient API error must not silently halt trading.
            # Reuse today's cached value if we have one, else assume 0.
            log.warning("DailyLossGuard: closed-pnl fetch failed: %s", exc)
            return self._cached_pnl if today == self._cached_day else 0.0

        self._cached_pnl = total
        self._cached_at = now
        self._cached_day = today
        return total

    async def should_block(self) -> Tuple[bool, float]:
        """Return (block_new_entries, todays_realised_pnl)."""
        if not self.enabled:
            return False, 0.0
        pnl = await self.day_pnl()
        return (pnl <= -self.limit), pnl
