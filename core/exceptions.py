"""
Custom exception hierarchy for the Bybit trading system.

All exchange-layer errors inherit from ExchangeError so callers can
catch the broad class and decide whether to retry or abort.
"""

from __future__ import annotations


class TradingBotError(Exception):
    """Root exception for all bot errors."""


# ── Exchange layer ────────────────────────────────────────────────────────────

class ExchangeError(TradingBotError):
    """Any error originating from the Bybit API or network layer."""


class ExchangeConnectionError(ExchangeError):
    """Cannot reach Bybit (DNS, TCP, timeout)."""


class ExchangeAuthError(ExchangeError):
    """API key / secret invalid or permissions missing."""


class ExchangeRateLimitError(ExchangeError):
    """429 / rate-limit hit; caller should back off and retry."""


class ExchangeMaintenanceError(ExchangeError):
    """Exchange is in maintenance mode (retCode 10006 / system busy)."""


class OrderError(ExchangeError):
    """Order placement, amendment, or cancellation failed."""


class OrderNotFoundError(OrderError):
    """Order referenced by orderLinkId or orderId does not exist."""


class InsufficientMarginError(OrderError):
    """Not enough available balance to place this order."""


class InvalidPriceError(OrderError):
    """Computed price fails tick-size or geometry validation."""


class InvalidQtyError(OrderError):
    """Computed quantity is below broker minimum or above maximum."""


# ── Data layer ────────────────────────────────────────────────────────────────

class DataError(TradingBotError):
    """OHLCV data fetching or validation failed."""


class InsufficientDataError(DataError):
    """Not enough bars returned to meet EMA warmup requirements."""


class StaleDataError(DataError):
    """Latest candle timestamp is older than expected (clock drift / feed gap)."""


# ── Strategy layer ────────────────────────────────────────────────────────────

class StrategyError(TradingBotError):
    """Strategy computation error (should never occur with valid data)."""


# ── Configuration ─────────────────────────────────────────────────────────────

class ConfigError(TradingBotError):
    """Invalid or missing configuration value."""


# ── State / recovery ─────────────────────────────────────────────────────────

class StateError(TradingBotError):
    """Bot state is inconsistent (e.g. orphan orders found at startup)."""
