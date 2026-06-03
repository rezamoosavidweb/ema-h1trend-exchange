"""
WebSocket event journal — persists a faithful copy of every Bybit private
WebSocket order/position event that the bot forwards to Telegram.

Motivation
----------
The polling reconciler (`_reconcile_position_close`) looks up a position's
exit price / realised PnL via a *separate* REST call (`get_closed_pnl`) that
sometimes lags or returns empty when several positions close at once. When it
does, the `position_closed` event is written with `exit_price=NaN, pnl=0`,
silently dropping real wins from any later analysis.

The private WebSocket, by contrast, already carries the authoritative
`avgPrice` / `realisedPnl` / `cumExecFee` in real time — but until now that
data only reached Telegram and was never stored.

This journal closes that gap: one durable, append-only JSONL record per WS
message, rotated by UTC date, written alongside the per-symbol logs. It is the
source of truth for realised PnL and win-rate analysis.

File layout (under the same dir as the per-symbol logs):
    logs/bybit_bot/_ws_events-YYYY-MM-DD.jsonl

Each line is one WS message:
    {"received_ts": "...Z", "topic": "order", "id": "...",
     "creationTime": "1717...", "data": [ {raw order/position record}, ... ]}

JSONL is append-safe: a crash mid-write corrupts at most one trailing line.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

log = logging.getLogger(__name__)


class WsEventJournal:
    """Append-only JSONL log of raw WebSocket order/position events."""

    def __init__(self, log_dir: Optional[str | Path], prefix: str = "_ws_events") -> None:
        self._enabled = bool(log_dir)
        self._prefix = prefix
        self._lock = Lock()
        if self._enabled:
            self._dir = Path(log_dir)
            self._dir.mkdir(parents=True, exist_ok=True)
        else:
            self._dir = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _current_path(self) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._dir / f"{self._prefix}-{today}.jsonl"

    def log_event(self, event: dict) -> None:
        """
        Append one raw WS message to the journal. Never raises — a logging
        failure must not interrupt the live notifier.
        """
        if not self._enabled:
            return
        entry = {
            "received_ts": datetime.now(timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "topic":        event.get("topic", ""),
            "id":           event.get("id"),
            "creationTime": event.get("creationTime") or event.get("ts"),
            "data":         event.get("data", []),
        }
        try:
            line = json.dumps(entry, default=str, ensure_ascii=False)
            with self._lock:
                with self._current_path().open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 — logging must never crash the notifier
            log.warning("WsEventJournal write failed: %s", exc)
