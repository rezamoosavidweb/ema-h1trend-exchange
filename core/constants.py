"""
Shared constants — strategy-level values that must never change between MT5 and Bybit.
Any value that affects signal math belongs here.
"""

# ── EMA periods (must match MT5 exactly) ─────────────────────────────────────
EMA_FAST: int = 8
EMA_MID: int = 13
EMA_SLOW: int = 21

# ── Warmup requirements ───────────────────────────────────────────────────────
MIN_WARMUP_BARS_M5: int = 200
MIN_WARMUP_BARS_H1: int = 200

# ── Default strategy parameters (overridable via env / config) ───────────────
DEFAULT_LOOKBACK_BARS: int = 5
DEFAULT_PENDING_OFFSET_TICKS: float = 3.0
DEFAULT_PENDING_EXPIRY_MIN: int = 60
DEFAULT_RR: float = 1.0
DEFAULT_RISK_PER_TRADE: float = 0.01
DEFAULT_START_BALANCE: float = 10_000.0

# ── Candle timeframes (Bybit interval strings) ────────────────────────────────
TF_ENTRY_BYBIT: str = "5"    # 5-minute candles
TF_TREND_BYBIT: str = "60"   # 1-hour candles
ENTRY_TF_MINUTES: int = 5

# ── Bars to fetch (generous buffer for EMA warmup + merge) ───────────────────
BARS_ENTRY: int = 600   # M5: 200 warmup + 50 buffer + headroom for merge
BARS_TREND: int = 350   # H1: 200 warmup + 50 buffer

# ── Order identification ──────────────────────────────────────────────────────
ORDER_LINK_ID_PREFIX: str = "ema"
ORDER_COMMENT: str = "ema_trend"

# ── Bybit category ────────────────────────────────────────────────────────────
BYBIT_CATEGORY: str = "linear"
BYBIT_ACCOUNT_TYPE: str = "UNIFIED"
BYBIT_QUOTE_COIN: str = "USDT"

# ── Retry / rate-limit ────────────────────────────────────────────────────────
MAX_RETRIES: int = 5
RETRY_BASE_DELAY: float = 1.0   # seconds
RETRY_MAX_DELAY: float = 60.0
RATE_LIMIT_SLEEP: float = 0.2   # seconds between consecutive API calls
