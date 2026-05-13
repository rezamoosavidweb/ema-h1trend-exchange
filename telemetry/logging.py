"""
Structured logging setup — JSON or human-readable, always UTC timestamps.

Each trading symbol gets its own log file via add_symbol_file_handler().
Use SymbolAdapter to tag all log records from a symbol's components.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Optional


# ── Formatters ────────────────────────────────────────────────────────────────

class _UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return time.strftime(datefmt, ct)
        t = time.strftime("%Y-%m-%dT%H:%M:%S", ct)
        return f"{t}.{int(record.msecs):03d}Z"


class _JsonFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        ct = time.gmtime(record.created)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", ct) + f".{int(record.msecs):03d}Z"
        obj = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        _SKIP = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message",
        }
        for key, val in record.__dict__.items():
            if key not in _SKIP:
                obj[key] = val
        return json.dumps(obj, default=str)


# ── Symbol-aware adapter ──────────────────────────────────────────────────────

class SymbolAdapter(logging.LoggerAdapter):
    """
    Wraps any logger and prepends [SYMBOL] to every message.

    Usage:
        log = logging.getLogger(__name__)
        self._log = SymbolAdapter(log, symbol="BTCUSDT")
        self._log.info("Order placed | qty=0.01")
        # → "[BTCUSDT] Order placed | qty=0.01"
    """

    def __init__(self, logger: logging.Logger, symbol: str) -> None:
        super().__init__(logger, {"symbol": symbol})
        self._symbol = symbol

    def process(self, msg, kwargs):
        return f"[{self._symbol}] {msg}", kwargs


# ── Per-symbol file handler ───────────────────────────────────────────────────

class _SymbolFilter(logging.Filter):
    """Passes only records whose message contains [SYMBOL]."""

    def __init__(self, symbol: str) -> None:
        super().__init__()
        self._tag = f"[{symbol}]"

    def filter(self, record: logging.LogRecord) -> bool:
        return self._tag in record.getMessage()


def add_symbol_file_handler(
    symbol: str,
    log_dir: str = "logs",
    level: str = "INFO",
    json_output: bool = False,
) -> None:
    """
    Attach a per-symbol FileHandler to the root logger.

    Only records tagged with [SYMBOL] (via SymbolAdapter) are written to the file.
    Safe to call multiple times — duplicate handlers for the same path are skipped.
    """
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{symbol}.log")

    root = logging.getLogger()
    # Skip if handler for this path already registered
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None):
            if os.path.abspath(h.baseFilename) == os.path.abspath(path):
                return

    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(getattr(logging, level.upper(), logging.INFO))
    fh.addFilter(_SymbolFilter(symbol))

    if json_output:
        fh.setFormatter(_JsonFormatter())
    else:
        fh.setFormatter(_UTCFormatter(
            fmt="%(asctime)s | %(levelname)-5s | %(module)-16s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

    root.addHandler(fh)


# ── Public setup function ─────────────────────────────────────────────────────

def configure_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    json_output: bool = False,
) -> None:
    """Configure root logger. Call once at startup."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    if json_output:
        fmt = _JsonFormatter()
    else:
        fmt = _UTCFormatter(
            fmt="%(asctime)s | %(levelname)-5s | %(module)-16s | %(message)s",
            datefmt="%H:%M:%S",
        )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(_UTCFormatter(
            fmt="%(asctime)s | %(levelname)-5s | %(module)-16s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(fh)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pybit").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
