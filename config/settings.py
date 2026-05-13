"""
Pydantic-based configuration — all settings come from environment variables or a .env file.

Usage:
    from config.settings import get_settings
    cfg = get_settings()
    cfg.bybit_api_key  # etc.

Keep BYBIT_API_KEY / BYBIT_API_SECRET out of source control — use .env or OS env.
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.constants import (
    BARS_ENTRY,
    BARS_TREND,
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_PENDING_EXPIRY_MIN,
    DEFAULT_PENDING_OFFSET_TICKS,
    DEFAULT_RISK_FIXED_USDT,
    DEFAULT_RR,
    DEFAULT_START_BALANCE,
    ENTRY_TF_MINUTES,
    TF_ENTRY_BYBIT,
    TF_TREND_BYBIT,
)


def _derive_magic(symbol: str) -> int:
    """Stable 8-digit magic from symbol name so parallel instances don't collide."""
    h = int(hashlib.md5(symbol.upper().encode()).hexdigest()[:8], 16)
    return 10_000_000 + (h % 89_999_999)


def _default_crypto_tick(sym: str) -> float:
    """Fallback pip size when not set explicitly — identical to MT5 logic."""
    u = sym.upper().replace("USDT", "").replace("PERP", "")
    if u.startswith("BTC"):
        return 1.0
    if u.startswith("ETH"):
        return 0.01
    if u.startswith(("XRP", "DOGE", "ADA")):
        return 0.0001
    if u.startswith(("BNB", "BCH", "LTC", "SOL")):
        return 0.01
    return 0.01


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Bybit credentials ─────────────────────────────────────────────────────
    bybit_api_key: str = Field(..., description="Bybit API key")
    bybit_api_secret: str = Field(..., description="Bybit API secret")
    bybit_testnet: bool = Field(False, description="Use Bybit testnet")
    bybit_demo: bool = Field(False, description="Use Bybit Demo Trading (api-demo.bybit.com)")

    # ── Symbol ────────────────────────────────────────────────────────────────
    symbol: str = Field("BTCUSDT", description="Bybit linear symbol, e.g. BTCUSDT")

    # ── Strategy parameters (MUST match MT5 values unless intentionally changed) ──
    magic: Optional[int] = Field(
        None,
        description="Order isolation magic. Default: auto-derived from symbol.",
    )
    risk_fixed_usdt: float = Field(
        DEFAULT_RISK_FIXED_USDT,
        gt=0,
        description="Fixed dollar risk per trade in USDT (e.g. 20.0 = $20). "
                    "qty = risk_fixed_usdt / sl_distance. Each trade wins/loses exactly this amount.",
    )
    pip_size: Optional[float] = Field(
        None,
        gt=0,
        description="Price increment for offset. Default: auto-detected from symbol.",
    )
    lookback_bars: int = Field(
        DEFAULT_LOOKBACK_BARS,
        gt=0,
        description="Bars in the swing window (HH/LL lookback).",
    )
    pending_offset_ticks: float = Field(
        DEFAULT_PENDING_OFFSET_TICKS,
        gt=0,
        description="Ticks beyond swing boundary for pending entry price.",
    )
    pending_expiry_min: int = Field(
        DEFAULT_PENDING_EXPIRY_MIN,
        gt=0,
        description="Minutes after signal bar before pending is cancelled.",
    )
    rr: float = Field(
        DEFAULT_RR,
        gt=0,
        description="Reward:risk ratio for TP (TP distance / SL distance).",
    )
    start_balance: float = Field(
        DEFAULT_START_BALANCE,
        gt=0,
        description="Balance used for backtest P&L simulation.",
    )

    # ── Timeframes ────────────────────────────────────────────────────────────
    tf_entry: str = Field(
        TF_ENTRY_BYBIT,
        description="Bybit interval string for entry timeframe (5m = '5').",
    )
    tf_trend: str = Field(
        TF_TREND_BYBIT,
        description="Bybit interval string for trend timeframe (1h = '60').",
    )
    entry_tf_minutes: int = Field(
        ENTRY_TF_MINUTES,
        gt=0,
        description="Entry timeframe in minutes (used for expiry bar count).",
    )

    # ── Data fetch sizes ──────────────────────────────────────────────────────
    bars_entry: int = Field(
        BARS_ENTRY,
        gt=0,
        description="M5 bars to fetch (include warmup buffer).",
    )
    bars_trend: int = Field(
        BARS_TREND,
        gt=0,
        description="H1 bars to fetch (include warmup buffer).",
    )

    # ── Execution ─────────────────────────────────────────────────────────────
    leverage: int = Field(
        1,
        ge=1,
        le=100,
        description="Leverage to set on the symbol at startup.",
    )
    position_mode: str = Field(
        "one_way",
        description="'one_way' (positionIdx=0) or 'hedge' (positionIdx=1/2).",
    )
    dry_run: bool = Field(
        False,
        description="If True, signals are logged but no orders are placed.",
    )
    replace_pending: bool = Field(
        False,
        description="Cancel existing pending before placing new one on each cycle.",
    )

    # ── Retry ─────────────────────────────────────────────────────────────────
    max_retries: int = Field(5, ge=1, description="Max API retries on transient errors.")
    retry_base_delay: float = Field(1.0, gt=0)
    retry_max_delay: float = Field(60.0, gt=0)

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = Field("INFO")
    log_file: Optional[str] = Field(None, description="File path for structured log output.")
    log_json: bool = Field(False, description="Emit JSON-formatted log lines.")
    signal_log_csv: Optional[str] = Field(
        None,
        description="CSV file path for strategy signal audit log. Empty = disabled.",
    )
    event_log_dir: str = Field(
        "logs",
        description="Directory for per-symbol JSONL event journals. Empty string disables.",
    )

    # ── Telegram (Telethon user account) ─────────────────────────────────────
    telegram_api_id: Optional[int] = Field(
        None,
        description="Telegram API ID from https://my.telegram.org/apps",
    )
    telegram_api_hash: Optional[str] = Field(
        None,
        description="Telegram API hash from https://my.telegram.org/apps",
    )
    telegram_phone: Optional[str] = Field(
        None,
        description="Your phone number (e.g. +989123456789). If unset, prompted at startup.",
    )
    telegram_session_file: str = Field(
        "telegram_session",
        description="Path (without .session suffix) where Telethon saves the login session.",
    )
    telegram_listen_chat: Optional[int] = Field(
        None,
        description="Chat ID to listen for slash commands. None = respond to all incoming messages.",
    )

    # ── Telegram (Bot API — for WebSocket order notifications) ────────────────
    telegram_bot_token: Optional[str] = Field(
        None,
        description="Telegram Bot token (from @BotFather). Used by WsOrderNotifier.",
    )
    telegram_chat_id: Optional[int] = Field(
        None,
        description="Target chat/channel ID for bot notifications (e.g. -1002383929199).",
    )

    # ── WebSocket notifier ────────────────────────────────────────────────────
    ws_notifier_enabled: bool = Field(
        True,
        description="Enable Bybit private WebSocket for real-time Telegram order notifications.",
    )

    # ── Derived properties (computed after validation) ────────────────────────
    @model_validator(mode="after")
    def _fill_derived(self) -> "Settings":
        if self.magic is None:
            self.magic = _derive_magic(self.symbol)
        if self.pip_size is None:
            self.pip_size = _default_crypto_tick(self.symbol)
        return self

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("position_mode")
    @classmethod
    def _valid_position_mode(cls, v: str) -> str:
        if v not in ("one_way", "hedge"):
            raise ValueError("position_mode must be 'one_way' or 'hedge'")
        return v

    @property
    def position_idx(self) -> int:
        """Bybit positionIdx: 0 for one-way, 1 for hedge-buy, 2 for hedge-sell."""
        return 0 if self.position_mode == "one_way" else 1

    @property
    def expiry_bars(self) -> int:
        """Number of entry-TF bars after which a pending order expires."""
        return max(1, self.pending_expiry_min // self.entry_tf_minutes)

    def effective_pip_size(self) -> float:
        """pip_size is always set after validation, but typed Optional — unwrap here."""
        return self.pip_size or _default_crypto_tick(self.symbol)

    def effective_magic(self) -> int:
        return self.magic or _derive_magic(self.symbol)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return singleton Settings instance (reads env once)."""
    return Settings()
