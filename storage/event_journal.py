"""
Per-symbol append-only event journal — JSONL format (one JSON object per line).

Each bot event (cycle start, signal, order create/modify/cancel, balance, error, ...)
is written as a timestamped JSON entry.  One file per symbol:
    logs/events_BTCUSDT.jsonl

JSONL is append-safe: a crash mid-write corrupts at most one trailing line.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


class EventJournal:
    """
    Append-only JSONL event log for one trading symbol.

    Usage:
        journal = EventJournal("BTCUSDT", log_dir="logs")
        journal.log("order_created", side="sell", entry=80000, qty=0.5)
    """

    def __init__(self, symbol: str, log_dir: str = "logs") -> None:
        self._symbol = symbol
        self._enabled = bool(log_dir)
        if self._enabled:
            path = Path(log_dir)
            path.mkdir(parents=True, exist_ok=True)
            self._path = path / f"events_{symbol}.jsonl"
        else:
            self._path = None

    # ── Public API ────────────────────────────────────────────────────────────

    def log(self, event: str, **data: Any) -> None:
        """Append one event entry to the journal."""
        if not self._enabled:
            return
        entry = {
            "ts": _now_iso(),
            "symbol": self._symbol,
            "event": event,
        }
        entry.update(data)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            log.warning("EventJournal write failed: %s", exc)

    @property
    def path(self) -> Optional[Path]:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._enabled


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
