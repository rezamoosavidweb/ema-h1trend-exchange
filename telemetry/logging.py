"""
Structured logging setup — JSON or human-readable, always UTC timestamps.

Mirrors the MT5 logging.basicConfig with UTC timestamps but adds:
  - Optional JSON output (for log aggregation: Loki / ELK / Datadog)
  - File handler support
  - Consistent format with exchange name in every line
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional


# ── JSON formatter ────────────────────────────────────────────────────────────

class _UTCFormatter(logging.Formatter):
    converter = time.gmtime  # force UTC in all timestamps

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return time.strftime(datefmt, ct)
        t = time.strftime("%Y-%m-%dT%H:%M:%S", ct)
        ms = int(record.msecs)
        return f"{t}.{ms:03d}Z"


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line."""
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
        # Include extra fields set by the caller
        for key, val in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "levelname", "levelno",
                "pathname", "filename", "module", "exc_info", "exc_text",
                "stack_info", "lineno", "funcName", "created", "msecs",
                "relativeCreated", "thread", "threadName", "processName",
                "process", "message",
            }:
                obj[key] = val
        return json.dumps(obj, default=str)


# ── Public setup function ─────────────────────────────────────────────────────

def configure_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    json_output: bool = False,
) -> None:
    """
    Configure root logger with UTC timestamps.
    Call once at startup before importing any other module that logs.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear any existing handlers (idempotent on re-call)
    root.handlers.clear()

    if json_output:
        fmt = _JsonFormatter()
    else:
        fmt = _UTCFormatter(
            fmt="%(asctime)s UTC | %(levelname)-8s | %(name)-30s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # Console handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Optional file handler
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Silence noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pybit").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper — same as logging.getLogger(name)."""
    return logging.getLogger(name)
