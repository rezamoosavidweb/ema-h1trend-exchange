"""
Tracks the cumulative realised PnL across all of the bot's logged trades.

On startup it sums `pnl_usdt` from every `position_closed` event in the
per-symbol JSONL files under the log directory. After that, callers add each
new close's PnL via `add()`. The value is intentionally bot-scoped — it
mirrors what the bot has done, not the wallet balance you'd see on Bybit
(those can diverge if you trade the account manually or if a partial fill
isn't observed by the polling reconciler).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)


class AccountPnLTracker:
    def __init__(self, logs_dir: Path) -> None:
        self._lock = Lock()
        self._total = self._sum_from_logs(Path(logs_dir))
        log.info("AccountPnLTracker initialised from %s: total=%+.4f USDT",
                 logs_dir, self._total)

    @staticmethod
    def _sum_from_logs(logs_dir: Path) -> float:
        if not logs_dir.is_dir():
            return 0.0
        total = 0.0
        for fp in sorted(logs_dir.glob("*.jsonl")):
            if fp.name.startswith("_"):
                continue
            try:
                with fp.open("r", encoding="utf-8") as f:
                    for line in f:
                        if '"position_closed"' not in line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("event") != "position_closed":
                            continue
                        try:
                            total += float(event.get("pnl_usdt") or 0)
                        except (TypeError, ValueError):
                            pass
            except OSError as exc:
                log.warning("AccountPnLTracker: cannot read %s: %s", fp, exc)
        return total

    def add(self, pnl: float) -> float:
        with self._lock:
            self._total += pnl
            return self._total

    @property
    def total(self) -> float:
        with self._lock:
            return self._total
