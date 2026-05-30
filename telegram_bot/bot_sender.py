"""
Bot API sender — POSTs notifications to a Telegram channel using a bot token.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment (load_dotenv
should already have populated them). The class is silent-by-design: every
network failure is logged but never raised so the trading loop is never
interrupted by a flaky Telegram connection.

The sync `send()` matches the urllib/retry/HTML pattern that other parts of
the project use. `send_async()` wraps it in `asyncio.to_thread` so it can
be passed straight to `WsOrderNotifier`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Optional, Protocol

log = logging.getLogger(__name__)

try:
    import certifi
    _SSL_CONTEXT: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CONTEXT = None


class _LoggerLike(Protocol):
    def event(self, name: str, **fields: object) -> None: ...


class BotTelegramSender:
    """HTTP-based Telegram Bot sender.

    Categories are free-form labels (e.g. "ws_order", "ws_position") that
    appear in the diagnostic events so the source of each message is
    traceable in the JSONL logs.
    """

    def __init__(
        self,
        logger: Optional[_LoggerLike] = None,
        timeout: float = 10.0,
        max_retries: int = 2,
        retry_delay_seconds: float = 1.5,
    ) -> None:
        token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID",   "")
        self._url         = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id     = chat_id
        self._enabled     = bool(token and chat_id)
        self._logger      = logger
        self._timeout     = timeout
        self._max_retries = max(0, int(max_retries))
        self._retry_delay = max(0.0, float(retry_delay_seconds))

        if not self._enabled:
            payload = dict(reason="missing_token_or_chat_id",
                            has_token=bool(token), has_chat_id=bool(chat_id))
            if self._logger:
                self._logger.event("telegram_disabled", **payload)
            else:
                log.warning("BotTelegramSender disabled: %s", payload)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(self, text: str, category: str = "generic") -> bool:
        if not self._enabled:
            return False

        payload = json.dumps({
            "chat_id":                  self._chat_id,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }).encode()

        last_failure: Optional[dict] = None

        for attempt in range(1, self._max_retries + 2):
            t0 = time.monotonic()
            try:
                req = urllib.request.Request(
                    self._url, data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self._timeout,
                                             context=_SSL_CONTEXT) as resp:
                    status = resp.status
                    body = resp.read(512).decode("utf-8", errors="replace")

                if 200 <= status < 300:
                    if self._logger:
                        self._logger.event(
                            "telegram_sent",
                            category=category, attempt=attempt, status=status,
                            latency_ms=round((time.monotonic() - t0) * 1000, 1),
                        )
                    return True

                last_failure = {"category": category, "attempt": attempt,
                                "status": status, "body": body[:200]}
                if status != 429 and not (500 <= status < 600):
                    break

            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read(512).decode("utf-8", errors="replace")
                except Exception:
                    pass
                last_failure = {"category": category, "attempt": attempt,
                                "status": exc.code, "body": body[:200],
                                "error_type": "HTTPError"}
                if exc.code != 429 and not (500 <= exc.code < 600):
                    break

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_failure = {"category": category, "attempt": attempt,
                                "error_type": type(exc).__name__,
                                "error_msg":  str(exc)}

            if attempt <= self._max_retries:
                time.sleep(self._retry_delay)

        if last_failure is not None:
            if self._logger:
                self._logger.event("telegram_error", **last_failure)
            else:
                log.warning("telegram_error: %s", last_failure)
        return False

    async def send_async(self, text: str, category: str = "ws_event") -> None:
        """Async adapter — runs the sync send() in a worker thread."""
        await asyncio.to_thread(self.send, text, category)
