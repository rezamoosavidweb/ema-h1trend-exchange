"""
Strategy signal audit log — append-only CSV.

Logs every signal the strategy generates (model prices from compute_pending_setup,
NOT broker-adjusted fills). This mirrors MT5's strategy03_signals.csv format
so you can compare signal history between the two systems.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SIGNAL_CSV_FIELDS = (
    "logged_at_utc",
    "symbol",
    "magic",
    "signal_bar_utc",
    "trend",
    "side",
    "model_entry",
    "model_sl",
    "model_tp",
    "setup_qty",
    "adjusted_entry",
    "adjusted_sl",
    "adjusted_tp",
    "normalized_qty",
    "balance",
    "risk_cash",
    "pip_size",
    "lookback_bars",
    "rr",
    "pending_offset_ticks",
    "order_link_id",
    "dry_run",
    "action",          # "created" | "modified" | "cancelled" | "expired" | "skipped"
    "reason",
)


class SignalLogger:
    """
    Append strategy signals to a CSV file for audit and comparison.

    Thread-safe: file is opened + closed on each write (no held handles).
    """

    def __init__(self, path: Optional[str]) -> None:
        self._path: Optional[Path] = Path(path) if path else None

    def is_enabled(self) -> bool:
        return self._path is not None

    def log_signal(
        self,
        *,
        symbol: str,
        magic: int,
        signal_bar_time,
        trend: str,
        side: str,
        model_entry: float,
        model_sl: float,
        model_tp: float,
        setup_qty: float,
        adjusted_entry: Optional[float] = None,
        adjusted_sl: Optional[float] = None,
        adjusted_tp: Optional[float] = None,
        normalized_qty: Optional[float] = None,
        balance: float = 0.0,
        risk_cash: float = 0.0,
        pip_size: float = 0.0,
        lookback_bars: int = 5,
        rr: float = 1.0,
        pending_offset_ticks: float = 3.0,
        order_link_id: str = "",
        dry_run: bool = False,
        action: str = "created",
        reason: str = "",
    ) -> None:
        if self._path is None:
            return

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not self._path.exists() or self._path.stat().st_size == 0

            bar_iso = (
                signal_bar_time.isoformat()
                if hasattr(signal_bar_time, "isoformat")
                else str(signal_bar_time)
            )
            row = {
                "logged_at_utc": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "magic": magic,
                "signal_bar_utc": bar_iso,
                "trend": trend,
                "side": side,
                "model_entry": f"{model_entry:.5f}",
                "model_sl": f"{model_sl:.5f}",
                "model_tp": f"{model_tp:.5f}",
                "setup_qty": f"{setup_qty:.6f}",
                "adjusted_entry": f"{adjusted_entry:.5f}" if adjusted_entry is not None else "",
                "adjusted_sl": f"{adjusted_sl:.5f}" if adjusted_sl is not None else "",
                "adjusted_tp": f"{adjusted_tp:.5f}" if adjusted_tp is not None else "",
                "normalized_qty": f"{normalized_qty:.6f}" if normalized_qty is not None else "",
                "balance": f"{balance:.2f}",
                "risk_cash": f"{risk_cash:.4f}",
                "pip_size": f"{pip_size:.6f}",
                "lookback_bars": lookback_bars,
                "rr": rr,
                "pending_offset_ticks": pending_offset_ticks,
                "order_link_id": order_link_id,
                "dry_run": dry_run,
                "action": action,
                "reason": reason,
            }

            with self._path.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=SIGNAL_CSV_FIELDS, extrasaction="ignore")
                if write_header:
                    w.writeheader()
                w.writerow({k: row.get(k, "") for k in SIGNAL_CSV_FIELDS})

        except OSError as exc:
            log.warning("Signal log write failed: %s", exc)
