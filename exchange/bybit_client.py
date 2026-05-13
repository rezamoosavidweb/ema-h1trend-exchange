"""
Bybit exchange client — thin async-friendly wrapper around pybit HTTP.

All public methods are async (they run the synchronous pybit calls in a
thread-pool via asyncio.to_thread so the event loop stays unblocked).

Retry + exponential back-off is applied transparently to all calls.
Rate-limit errors (retCode 10006 / 10016 / 429) trigger a longer sleep.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from pybit.exceptions import FailedRequestError as PybitFailedRequestError
from pybit.exceptions import InvalidRequestError as PybitInvalidRequestError
from pybit.unified_trading import HTTP

from core.constants import (
    BYBIT_ACCOUNT_TYPE,
    BYBIT_CATEGORY,
    BYBIT_QUOTE_COIN,
    RATE_LIMIT_SLEEP,
)
from core.exceptions import (
    ExchangeAuthError,
    ExchangeConnectionError,
    ExchangeMaintenanceError,
    ExchangeRateLimitError,
    InsufficientMarginError,
    OrderNotFoundError,
    ExchangeError,
)
from models.order import InstrumentInfo, Position, Side, WalletBalance

log = logging.getLogger(__name__)

# Bybit retCodes that signal transient / rate-limit conditions
_RATE_LIMIT_CODES = {10006, 10016, 10018, 130035}
_MAINTENANCE_CODES = {10005}
_AUTH_CODES = {10003, 10004, 33004}
_MARGIN_CODES = {110007, 110012, 110014}
_NOT_FOUND_CODES = {20001, 110001}


def _check_response(resp: dict, context: str = "") -> dict:
    """Raise appropriate exception if retCode != 0."""
    code = int(resp.get("retCode", -1))
    msg = resp.get("retMsg", "")
    if code == 0:
        return resp
    if code in _RATE_LIMIT_CODES:
        raise ExchangeRateLimitError(f"{context}: rate limit (code={code} msg={msg})")
    if code in _MAINTENANCE_CODES:
        raise ExchangeMaintenanceError(f"{context}: maintenance (code={code} msg={msg})")
    if code in _AUTH_CODES:
        raise ExchangeAuthError(f"{context}: auth failed (code={code} msg={msg})")
    if code in _MARGIN_CODES:
        raise InsufficientMarginError(f"{context}: margin (code={code} msg={msg})")
    if code in _NOT_FOUND_CODES:
        raise OrderNotFoundError(f"{context}: not found (code={code} msg={msg})")
    raise ExchangeError(f"{context}: code={code} msg={msg}")


class BybitClient:
    """
    Async-friendly Bybit Linear Futures client.

    Construction:
        client = BybitClient(api_key="...", api_secret="...", testnet=False)

    All public methods are coroutines:
        info = await client.get_instrument_info("BTCUSDT")
        bal  = await client.get_balance()
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        testnet: bool = False,
        demo: bool = False,
        max_retries: int = 5,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 60.0,
    ) -> None:
        self._session = HTTP(
            testnet=testnet,
            demo=demo,
            api_key=api_key,
            api_secret=api_secret,
        )
        self._max_retries = max_retries
        self._retry_base = retry_base_delay
        self._retry_max = retry_max_delay
        self._last_call_ts: float = 0.0

    # ── Internal: retry wrapper ───────────────────────────────────────────────

    async def _call(self, fn_name: str, **kwargs: Any) -> dict:
        """
        Call a pybit session method by name with exponential back-off retry.
        Runs the sync call in a thread so the async event loop stays free.
        """
        fn = getattr(self._session, fn_name)
        delay = self._retry_base

        for attempt in range(1, self._max_retries + 1):
            # Soft rate-limit guard: never fire more than 5 calls/sec globally
            now = time.monotonic()
            gap = RATE_LIMIT_SLEEP - (now - self._last_call_ts)
            if gap > 0:
                await asyncio.sleep(gap)

            try:
                resp = await asyncio.to_thread(fn, **kwargs)
                self._last_call_ts = time.monotonic()
                return _check_response(resp, context=fn_name)

            except ExchangeRateLimitError as exc:
                log.warning("[%d/%d] Rate limit: %s — sleeping %.1fs", attempt, self._max_retries, exc, delay * 5)
                await asyncio.sleep(min(delay * 5, self._retry_max))
                delay = min(delay * 2, self._retry_max)

            except ExchangeMaintenanceError:
                log.warning("[%d/%d] Bybit maintenance — sleeping 30s", attempt, self._max_retries)
                await asyncio.sleep(30)

            except (ExchangeAuthError, InsufficientMarginError, OrderNotFoundError):
                raise  # non-retryable

            except (ConnectionError, TimeoutError, OSError) as exc:
                log.warning("[%d/%d] Network error: %s — retrying in %.1fs", attempt, self._max_retries, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_max)
                if attempt == self._max_retries:
                    raise ExchangeConnectionError(str(exc)) from exc

            except ExchangeError:
                raise

            except (PybitInvalidRequestError, PybitFailedRequestError) as exc:
                raise ExchangeError(str(exc)) from exc

            if attempt < self._max_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_max)

        raise ExchangeError(f"{fn_name}: exhausted {self._max_retries} retries")

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_instrument_info(self, symbol: str) -> InstrumentInfo:
        """Fetch tick size + lot size constraints for one symbol."""
        resp = await self._call(
            "get_instruments_info",
            category=BYBIT_CATEGORY,
            symbol=symbol,
        )
        items = resp["result"]["list"]
        if not items:
            raise ExchangeError(f"No instrument info for {symbol}")
        return InstrumentInfo.from_bybit(items[0])

    async def get_kline(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[list]:
        """
        Fetch OHLCV candles (newest-first from Bybit, returned as-is).

        Each item: [startTimeMs, open, high, low, close, volume, turnover]
        Caller is responsible for reversing and parsing.
        """
        resp = await self._call(
            "get_kline",
            category=BYBIT_CATEGORY,
            symbol=symbol,
            interval=interval,
            limit=min(limit, 1000),  # Bybit max per call
        )
        return resp["result"]["list"]

    async def get_ticker(self, symbol: str) -> dict:
        """Return the latest ticker (bid, ask, last price) for the symbol."""
        resp = await self._call(
            "get_tickers",
            category=BYBIT_CATEGORY,
            symbol=symbol,
        )
        items = resp["result"]["list"]
        if not items:
            raise ExchangeError(f"No ticker for {symbol}")
        return items[0]

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> WalletBalance:
        """Return USDT unified wallet balance."""
        resp = await self._call(
            "get_wallet_balance",
            accountType=BYBIT_ACCOUNT_TYPE,
            coin=BYBIT_QUOTE_COIN,
        )
        accounts = resp["result"]["list"]
        if not accounts:
            raise ExchangeError("Empty wallet balance response")

        account = accounts[0]
        coins = account.get("coin", [])
        usdt = next(
            (c for c in coins if c["coin"] == BYBIT_QUOTE_COIN),
            None,
        )
        if usdt is None:
            return WalletBalance(
                total_equity=0.0,
                available_balance=0.0,
                used_margin=0.0,
            )

        wallet_bal = float(usdt.get("walletBalance", 0) or 0)
        position_im = float(usdt.get("totalPositionIM", 0) or 0)
        order_im = float(usdt.get("totalOrderIM", 0) or 0)
        # availableToWithdraw is empty string on Demo — compute directly from USDT fields.
        available = max(wallet_bal - position_im - order_im, 0.0)
        return WalletBalance(
            total_equity=float(usdt.get("equity", 0) or 0),
            available_balance=available,
            used_margin=position_im + order_im,
        )

    # ── Positions ─────────────────────────────────────────────────────────────

    async def get_positions(self, symbol: str) -> list[Position]:
        """Return open positions for the symbol (size > 0)."""
        resp = await self._call(
            "get_positions",
            category=BYBIT_CATEGORY,
            symbol=symbol,
        )
        positions: list[Position] = []
        for p in resp["result"]["list"]:
            size = float(p.get("size", 0) or 0)
            if size <= 0:
                continue
            raw_side = p.get("side", "None")
            if raw_side == "None":
                continue
            side = Side.BUY if raw_side == "Buy" else Side.SELL
            positions.append(
                Position(
                    symbol=p["symbol"],
                    side=side,
                    size=size,
                    entry_price=float(p.get("avgPrice", 0) or 0),
                    unrealized_pnl=float(p.get("unrealisedPnl", 0) or 0),
                    leverage=float(p.get("leverage", 1) or 1),
                    position_idx=int(p.get("positionIdx", 0)),
                )
            )
        return positions

    async def has_open_position(self, symbol: str) -> bool:
        positions = await self.get_positions(symbol)
        return len(positions) > 0

    # ── Orders ────────────────────────────────────────────────────────────────

    async def get_open_stop_orders(self, symbol: str) -> list[dict]:
        """Return all open conditional/stop orders for the symbol."""
        resp = await self._call(
            "get_open_orders",
            category=BYBIT_CATEGORY,
            symbol=symbol,
            orderFilter="StopOrder",
        )
        return resp["result"]["list"]

    async def get_order_by_link_id(
        self,
        symbol: str,
        order_link_id: str,
    ) -> Optional[dict]:
        """
        Look up one order by its custom orderLinkId.
        Returns None if not found (404 is treated as None, not an exception).
        """
        try:
            resp = await self._call(
                "get_open_orders",
                category=BYBIT_CATEGORY,
                symbol=symbol,
                orderLinkId=order_link_id,
                orderFilter="StopOrder",
            )
            items = resp["result"]["list"]
            return items[0] if items else None
        except OrderNotFoundError:
            return None

    async def place_conditional_order(
        self,
        *,
        symbol: str,
        side: str,           # "Buy" or "Sell"
        qty: str,
        trigger_price: str,
        limit_price: str,
        sl: str,
        tp: str,
        order_link_id: str,
        trigger_direction: int,   # 1=rises-to, 2=falls-to
        position_idx: int = 0,
        trigger_by: str = "LastPrice",
        sl_trigger_by: str = "LastPrice",
        tp_trigger_by: str = "LastPrice",
        time_in_force: str = "GTC",
    ) -> dict:
        """
        Place a conditional (stop-entry) order on Bybit Linear.

        MT5 mapping:
          BUY_STOP  → side="Buy",  triggerDirection=1
          SELL_STOP → side="Sell", triggerDirection=2

        SL and TP are attached at order creation time; they auto-activate when
        the conditional order fills and a position is opened.
        """
        resp = await self._call(
            "place_order",
            category=BYBIT_CATEGORY,
            symbol=symbol,
            side=side,
            orderType="Limit",
            qty=qty,
            price=limit_price,
            triggerPrice=trigger_price,
            triggerBy=trigger_by,
            triggerDirection=trigger_direction,
            timeInForce=time_in_force,
            orderLinkId=order_link_id,
            stopLoss=sl,
            takeProfit=tp,
            slTriggerBy=sl_trigger_by,
            tpTriggerBy=tp_trigger_by,
            positionIdx=position_idx,
            reduceOnly=False,
            closeOnTrigger=False,
        )
        log.info(
            "Placed conditional order | link=%s side=%s triggerPrice=%s sl=%s tp=%s qty=%s",
            order_link_id, side, trigger_price, sl, tp, qty,
        )
        return resp["result"]

    async def amend_conditional_order(
        self,
        *,
        symbol: str,
        order_link_id: str,
        qty: Optional[str] = None,
        trigger_price: Optional[str] = None,
        limit_price: Optional[str] = None,
        sl: Optional[str] = None,
        tp: Optional[str] = None,
    ) -> dict:
        """Amend price / SL / TP on an existing conditional order."""
        kwargs: dict[str, Any] = dict(
            category=BYBIT_CATEGORY,
            symbol=symbol,
            orderLinkId=order_link_id,
        )
        if qty is not None:
            kwargs["qty"] = qty
        if trigger_price is not None:
            kwargs["triggerPrice"] = trigger_price
        if limit_price is not None:
            kwargs["price"] = limit_price
        if sl is not None:
            kwargs["stopLoss"] = sl
        if tp is not None:
            kwargs["takeProfit"] = tp

        resp = await self._call("amend_order", **kwargs)
        log.info(
            "Amended conditional order | link=%s triggerPrice=%s sl=%s tp=%s",
            order_link_id, trigger_price, sl, tp,
        )
        return resp["result"]

    async def cancel_order(self, symbol: str, order_link_id: str) -> None:
        """Cancel a conditional order by orderLinkId. Silently ignores 'not found'."""
        try:
            await self._call(
                "cancel_order",
                category=BYBIT_CATEGORY,
                symbol=symbol,
                orderLinkId=order_link_id,
            )
            log.info("Cancelled order | link=%s", order_link_id)
        except OrderNotFoundError:
            log.debug("Cancel ignored — order not found | link=%s", order_link_id)

    async def cancel_all_stop_orders(self, symbol: str) -> None:
        """Cancel ALL conditional/stop orders for the symbol."""
        try:
            await self._call(
                "cancel_all_orders",
                category=BYBIT_CATEGORY,
                symbol=symbol,
                orderFilter="StopOrder",
            )
            log.info("Cancelled all stop orders | symbol=%s", symbol)
        except OrderNotFoundError:
            pass

    # ── Account setup ─────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage (buy and sell sides). Ignores 'not modified' error."""
        try:
            await self._call(
                "set_leverage",
                category=BYBIT_CATEGORY,
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
            log.info("Leverage set | symbol=%s leverage=%d", symbol, leverage)
        except ExchangeError as exc:
            if "leverage not modified" in str(exc).lower() or "110043" in str(exc):
                log.debug("Leverage already set to %d for %s", leverage, symbol)
            else:
                raise

    async def set_position_mode(self, symbol: str, mode: int) -> None:
        """
        0 = Merged Single (one-way)
        3 = Both Side (hedge)
        """
        try:
            await self._call(
                "switch_position_mode",
                category=BYBIT_CATEGORY,
                symbol=symbol,
                mode=mode,
            )
            log.info("Position mode set | symbol=%s mode=%d", symbol, mode)
        except ExchangeError as exc:
            if "position mode is not modified" in str(exc).lower():
                log.debug("Position mode already correct for %s", symbol)
            else:
                raise

    # ── Server time ───────────────────────────────────────────────────────────

    async def get_server_time(self) -> datetime:
        resp = await self._call("get_server_time")
        ts_ms = int(resp["result"]["timeSecond"]) * 1000
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

    # ── All positions (account-wide) ──────────────────────────────────────────

    async def get_all_positions(self) -> list[Position]:
        """Return all open positions across all linear USDT symbols."""
        resp = await self._call(
            "get_positions",
            category=BYBIT_CATEGORY,
            settleCoin=BYBIT_QUOTE_COIN,
        )
        positions: list[Position] = []
        for p in resp["result"]["list"]:
            size = float(p.get("size", 0) or 0)
            if size <= 0:
                continue
            raw_side = p.get("side", "None")
            if raw_side == "None":
                continue
            side = Side.BUY if raw_side == "Buy" else Side.SELL
            positions.append(
                Position(
                    symbol=p["symbol"],
                    side=side,
                    size=size,
                    entry_price=float(p.get("avgPrice", 0) or 0),
                    unrealized_pnl=float(p.get("unrealisedPnl", 0) or 0),
                    leverage=float(p.get("leverage", 1) or 1),
                    position_idx=int(p.get("positionIdx", 0)),
                )
            )
        return positions

    # ── Closed P&L ────────────────────────────────────────────────────────────

    async def get_closed_pnl(
        self,
        symbol: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Fetch closed P&L records (completed trades).

        Each record contains closedPnl, symbol, side, avgEntryPrice,
        avgExitPrice, closedSize, orderLinkId, updatedTime, etc.
        """
        kwargs: dict[str, Any] = {
            "category": BYBIT_CATEGORY,
            "limit": min(limit, 100),
        }
        if symbol:
            kwargs["symbol"] = symbol
        if start_ms is not None:
            kwargs["startTime"] = str(start_ms)
        if end_ms is not None:
            kwargs["endTime"] = str(end_ms)
        resp = await self._call("get_closed_pnl", **kwargs)
        return resp["result"]["list"]
