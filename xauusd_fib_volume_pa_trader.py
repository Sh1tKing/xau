import argparse
import atexit
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple
from urllib import error, request

import MetaTrader5 as mt5
import pandas as pd


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
LOG_DIR = os.path.join(BASE_DIR, "xau_fib_pa_logs")


def load_env_file(path=ENV_FILE):
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def require_env(name):
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"缺少必要环境变量: {name}，请检查 .env 文件")
    return value


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class DurableFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        try:
            if self.stream and not self.stream.closed:
                self.flush()
                os.fsync(self.stream.fileno())
        except Exception:
            self.handleError(record)


def setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    log_filename = os.path.join(LOG_DIR, f"Fib_PA_{datetime.now().strftime('%Y-%m-%d')}.log")
    logger = logging.getLogger("xauusd_fib_volume_pa")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)

        file_handler = DurableFileHandler(log_filename, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)

        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    return logger


load_env_file()
logger = setup_logger()
atexit.register(logging.shutdown)


MT5_LOGIN = int(require_env("MT5_LOGIN"))
MT5_PASSWORD = require_env("MT5_PASSWORD")
MT5_SERVER = require_env("MT5_SERVER")

SYMBOL = os.getenv("FIB_PA_SYMBOL", "XAUUSD")
LOT_SIZE = env_float("FIB_PA_LOT_SIZE", 0.01)
MAX_POSITIONS = env_int("FIB_PA_MAX_POSITIONS", 1)
TIMEFRAME_NAME = os.getenv("FIB_PA_TIMEFRAME", "M5").strip().upper()
CONFIDENCE_THRESHOLD = env_int("FIB_PA_CONFIDENCE_THRESHOLD", 80)
MAGIC = env_int("FIB_PA_MAGIC", 240610)
ORDER_TAG = os.getenv("FIB_PA_ORDER_TAG", "FIB_PA")
TITLE_TAG = os.getenv("FIB_PA_TITLE_TAG", "三策略")
ENABLE_TRADING = env_bool("FIB_PA_ENABLE_TRADING", True)
POLL_SECONDS = max(0.2, env_float("FIB_PA_POLL_SECONDS", 0.5))
DEVIATION = env_int("FIB_PA_DEVIATION", 30)
GUARDIAN_ENABLED = env_bool("FIB_PA_GUARDIAN_ENABLED", True)
BREAKEVEN_TRIGGER_R = env_float("FIB_PA_BREAKEVEN_TRIGGER_R", 0.8)
BREAKEVEN_TRIGGER_ATR = env_float("FIB_PA_BREAKEVEN_TRIGGER_ATR", 1.0)
BREAKEVEN_BUFFER_ATR = env_float("FIB_PA_BREAKEVEN_BUFFER_ATR", 0.1)
VIRTUAL_CONFIRM_SECONDS = env_float("FIB_PA_VIRTUAL_CONFIRM_SECONDS", 2.0)
STRUCTURE_EXIT_VOLUME_RATIO = env_float("FIB_PA_STRUCTURE_EXIT_VOLUME_RATIO", 1.2)
REAL_SL_DISASTER_ATR = env_float("FIB_PA_REAL_SL_DISASTER_ATR", 0.35)
REAL_SL_DISASTER_MIN = env_float("FIB_PA_REAL_SL_DISASTER_MIN", 1.0)
MIN_SL_MODIFY_ATR = env_float("FIB_PA_MIN_SL_MODIFY_ATR", 0.1)
MIN_SL_MODIFY_ABS = env_float("FIB_PA_MIN_SL_MODIFY_ABS", 0.5)
MIN_REWARD_RISK = env_float("FIB_PA_MIN_RR", 1.5)
MAX_SL_ATR = env_float("FIB_PA_MAX_SL_ATR", 2.0)
MAX_SL_ABS = env_float("FIB_PA_MAX_SL_ABS", 8.0)
ENTRY_MIN_SL_ATR = env_float("FIB_PA_ENTRY_MIN_SL_ATR", 0.6)
ENTRY_MIN_SL_ABS = env_float("FIB_PA_ENTRY_MIN_SL_ABS", 0.8)
H4_HARD_FILTER = env_bool("FIB_PA_H4_HARD_FILTER", False)
ALIGN_HARD_FILTER = True
CHASE_MAX_ATR = 1.5
REGIME_LOOKBACK = 12
TIGHT_BOX_ATR = 1.8
TIGHT_ADX = 15.0
WIDE_BOX_ATR = 2.6
WIDE_EDGE = 0.35
ENTRY_BAR_TIMEFRAME = mt5.TIMEFRAME_M15

FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
FEISHU_WEBHOOK_SECRET = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip()
FEISHU_NOTIFY_ENABLED = env_bool("FEISHU_NOTIFY_ENABLED", True)

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
}
TIMEFRAME = TIMEFRAME_MAP.get(TIMEFRAME_NAME, mt5.TIMEFRAME_M5)


@dataclass
class FibLevels:
    direction: str
    swing_low: float
    swing_high: float
    levels: Dict[str, float]


@dataclass
class StrategyResult:
    action: str
    confidence: int
    close_all: bool
    reasoning: str
    stop_loss: float
    take_profit: float
    score_buy: int
    score_sell: int
    details: Dict[str, object]

    def to_decision(self):
        return {
            "action": self.action,
            "confidence": self.confidence,
            "close_all": self.close_all,
            "reasoning": self.reasoning,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "details": self.details,
        }


@dataclass
class GuardianState:
    ticket: int
    direction: str
    entry_price: float
    initial_sl: float
    initial_tp: float
    risk: float
    highest_price: float
    lowest_price: float
    virtual_sl: float = 0.0
    breach_started_at: Optional[float] = None
    breakeven_done: bool = False


@dataclass
class PositionSnapshot:
    ticket: int
    identifier: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    open_time: int
    first_seen_at: float
    last_seen_at: float


def send_feishu_notification(title, lines):
    if not FEISHU_NOTIFY_ENABLED:
        return False
    if not FEISHU_WEBHOOK_URL:
        logger.info("飞书通知未配置 FEISHU_WEBHOOK_URL，跳过发送")
        return False

    payload = {
        "msg_type": "text",
        "content": {
            "text": "\n".join([str(title), "", *[str(line) for line in lines if line is not None]])
        },
    }

    if FEISHU_WEBHOOK_SECRET:
        timestamp = str(int(time.time()))
        sign_payload = f"{timestamp}\n{FEISHU_WEBHOOK_SECRET}".encode("utf-8")
        sign = base64.b64encode(
            hmac.new(sign_payload, b"", digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        payload["timestamp"] = timestamp
        payload["sign"] = sign

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        FEISHU_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
        if 200 <= response.status < 300:
            logger.info("✅ 飞书通知发送成功")
            return True
        logger.warning(f"⚠️ 飞书通知发送失败: HTTP {response.status} - {body[:200]}")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.warning(f"⚠️ 飞书通知发送失败: HTTP {exc.code} - {body[:200]}")
    except Exception as exc:
        logger.warning(f"⚠️ 飞书通知异常: {type(exc).__name__}: {exc}")
    return False


def format_signed_money(value) -> str:
    """Format realized P/L with an explicit sign for both profit and loss."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "未知"
    if amount >= 0:
        return "+$%.2f" % amount
    return "-$%.2f" % abs(amount)


def account_balance_lines() -> List[str]:
    """Return a compact, best-effort account balance/equity snapshot for alerts."""
    try:
        info = mt5.account_info()
    except Exception:
        info = None
    if info is None:
        return ["当前余额: 未知", "当前净值: 未知"]
    balance = getattr(info, "balance", None)
    equity = getattr(info, "equity", None)
    try:
        balance_text = "$%.2f" % float(balance)
    except (TypeError, ValueError):
        balance_text = "未知"
    try:
        equity_text = "$%.2f" % float(equity)
    except (TypeError, ValueError):
        equity_text = "未知"
    return [f"当前余额: {balance_text}", f"当前净值: {equity_text}"]


def normalize_rates(rates) -> pd.DataFrame:
    df = pd.DataFrame(rates)
    if df.empty:
        return df
    required = ["time", "open", "high", "low", "close"]
    for column in required:
        if column not in df.columns:
            raise ValueError(f"rates 缺少字段: {column}")
    if "tick_volume" not in df.columns:
        df["tick_volume"] = 0
    df = df.sort_values("time").reset_index(drop=True)
    for column in ["open", "high", "low", "close", "tick_volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
    return df


def calculate_atr(df: pd.DataFrame, period=14) -> float:
    if df is None or len(df) < 2:
        return 0.0
    high_low = df["high"] - df["low"]
    high_prev_close = (df["high"] - df["close"].shift(1)).abs()
    low_prev_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().dropna()
    if not atr.empty and atr.iloc[-1] > 0:
        return float(atr.iloc[-1])
    fallback = high_low.tail(period).mean()
    return float(fallback) if fallback and not math.isnan(fallback) else 0.0


def trend_from_df(df: pd.DataFrame) -> str:
    if df is None or len(df) < 25:
        return "neutral"
    sma5 = df["close"].tail(5).mean()
    sma20 = df["close"].tail(20).mean()
    latest = df["close"].iloc[-1]
    if sma5 > sma20 and latest >= sma20:
        return "bull"
    if sma5 < sma20 and latest <= sma20:
        return "bear"
    return "neutral"

def ema_last(series: pd.Series, period: int) -> Optional[float]:
    if series is None or len(series) < period:
        return None
    value = series.astype(float).ewm(span=period, adjust=False).mean().iloc[-1]
    if pd.isna(value):
        return None
    return float(value)


def trend_from_ema(df: pd.DataFrame, period: int = 50) -> str:
    if df is None or len(df) < period + 2:
        return "neutral"
    ema = ema_last(df["close"], period)
    if ema is None:
        return "neutral"
    close = float(df["close"].iloc[-1])
    if close > ema:
        return "bull"
    if close < ema:
        return "bear"
    return "neutral"


def calculate_adx(df: pd.DataFrame, period: int = 14) -> float:
    if df is None or len(df) < period * 2 + 2:
        return 0.0
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * plus_dm.rolling(period).mean() / atr.replace(0, math.nan)
    minus_di = 100 * minus_dm.rolling(period).mean() / atr.replace(0, math.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, math.nan) * 100
    adx = dx.rolling(period).mean().dropna()
    if adx.empty or pd.isna(adx.iloc[-1]):
        return 0.0
    return float(adx.iloc[-1])


def ema_distance_atr(df: pd.DataFrame, atr: float, period: int = 21) -> float:
    if df is None or atr <= 0:
        return 0.0
    ema = ema_last(df["close"], period)
    if ema is None:
        return 0.0
    return (float(df["close"].iloc[-1]) - ema) / atr


def classify_market_regime(df: pd.DataFrame, atr: float, adx: float, h1_trend: str = "neutral"):
    meta = {"location": 0.5, "box_atr": 0.0, "net_atr": 0.0}
    if df is None or len(df) < REGIME_LOOKBACK or atr <= 0:
        return "tight_range", "数据不足，视为窄震荡", meta
    window = df.tail(REGIME_LOOKBACK)
    box_low = float(window["low"].min())
    box_high = float(window["high"].max())
    box = max(box_high - box_low, 1e-9)
    box_atr = box / atr
    net_atr = abs(float(window["close"].iloc[-1]) - float(window["close"].iloc[0])) / atr
    price = float(df["close"].iloc[-1])
    location = min(1.0, max(0.0, (price - box_low) / box))
    meta = {"location": round(location, 4), "box_atr": round(box_atr, 2), "net_atr": round(net_atr, 2)}
    if box_atr < TIGHT_BOX_ATR or (adx < TIGHT_ADX and box_atr < WIDE_BOX_ATR and net_atr < 1.0):
        text_out = "窄震荡: ADX=%.1f, 12根区间=%.2fATR, 净位移=%.2fATR" % (adx, box_atr, net_atr)
        return "tight_range", text_out, meta
    if h1_trend in ("bull", "bear") and (adx >= 18 or net_atr >= 1.5):
        text_out = "趋势: H1=%s, ADX=%.1f, 净位移=%.2fATR, 区间=%.2fATR" % (h1_trend, adx, net_atr, box_atr)
        return "trend", text_out, meta
    if box_atr >= WIDE_BOX_ATR:
        text_out = "宽震荡: ADX=%.1f, 区间=%.2fATR, H1=%s" % (adx, box_atr, h1_trend)
        return "wide_range", text_out, meta
    text_out = "方向不清: ADX=%.1f, 区间=%.2fATR, 净位移=%.2fATR" % (adx, box_atr, net_atr)
    return "tight_range", text_out, meta


def regime_blocks_entry(action: str, regime: str, regime_text: str, meta: dict, pa_score: int) -> str:
    if regime == "tight_range":
        return regime_text or "窄震荡或方向不清，观望"
    if regime != "wide_range":
        return ""
    location = float((meta or {}).get("location", 0.5))
    if pa_score < 12:
        return "宽震荡缺少边缘拒绝，不追"
    if action == "buy" and location > WIDE_EDGE:
        return "宽震荡不在下沿(location=%.2f)，不追中轴" % location
    if action == "sell" and location < 1.0 - WIDE_EDGE:
        return "宽震荡不在上沿(location=%.2f)，不追中轴" % location
    return ""


def trends_support(action: str, h1_trend: str, m15_trend: str) -> bool:
    desired = "bull" if action == "buy" else "bear"
    return h1_trend == desired


# Beijing time sessions. Every session can open, but each one uses its own setups.
SESSION_SETUPS = {
    "asia": ("pullback", "range_edge"),
    "london": ("breakout", "pullback"),
    "overlap": ("breakout", "pullback"),
    "late_us": ("pullback", "range_edge"),
}
SESSION_TEXT = {
    "asia": "亚盘 07-15",
    "london": "欧盘 15-20",
    "overlap": "欧美重叠 20-01",
    "late_us": "美盘尾段 01-07",
}
SETUP_TEXT = {"pullback": "趋势回撤", "range_edge": "区间边缘", "breakout": "突破回踩"}
RANGE_LOOKBACK = 24
BREAKOUT_BOX = 16


BEIJING_TZ = timezone(timedelta(hours=8))


def trading_session(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(BEIJING_TZ)
    if now.tzinfo is not None:
        now = now.astimezone(BEIJING_TZ)
    hour = now.hour
    if 7 <= hour < 15:
        return "asia"
    if 15 <= hour < 20:
        return "london"
    if hour >= 20 or hour < 1:
        return "overlap"
    return "late_us"


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> float:
    if df is None or len(df) < period + 2:
        return 50.0
    delta = df["close"].astype(float).diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    last_gain = float(gain.iloc[-1])
    last_loss = float(loss.iloc[-1])
    if last_loss <= 1e-12:
        return 100.0 if last_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + last_gain / last_loss)


def macd_histogram(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[float, float]:
    if df is None or len(df) < slow + signal:
        return 0.0, 0.0
    close = df["close"].astype(float)
    macd = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    hist = macd - macd.ewm(span=signal, adjust=False).mean()
    return float(hist.iloc[-1]), float(hist.iloc[-2])


def find_pivots(df: pd.DataFrame, left=2, right=2) -> Tuple[List[int], List[int]]:
    highs = []
    lows = []
    if len(df) < left + right + 1:
        return highs, lows

    for i in range(left, len(df) - right):
        high_window = df["high"].iloc[i - left:i + right + 1]
        low_window = df["low"].iloc[i - left:i + right + 1]
        if df["high"].iloc[i] >= high_window.max():
            highs.append(i)
        if df["low"].iloc[i] <= low_window.min():
            lows.append(i)
    return highs, lows


def fallback_fib_structure(df: pd.DataFrame, lookback=60) -> Optional[FibLevels]:
    window = df.tail(lookback).reset_index(drop=True)
    if len(window) < 20:
        return None
    high_idx = int(window["high"].idxmax())
    low_idx = int(window["low"].idxmin())
    swing_high = float(window["high"].iloc[high_idx])
    swing_low = float(window["low"].iloc[low_idx])
    if swing_high <= swing_low:
        return None
    direction = "bull" if low_idx < high_idx else "bear"
    return build_fib_levels(direction, swing_low, swing_high)


def build_fib_levels(direction: str, swing_low: float, swing_high: float) -> Optional[FibLevels]:
    price_range = swing_high - swing_low
    if price_range <= 0:
        return None

    if direction == "bull":
        levels = {
            "38.2": swing_high - price_range * 0.382,
            "50.0": swing_high - price_range * 0.500,
            "61.8": swing_high - price_range * 0.618,
            "78.6": swing_high - price_range * 0.786,
        }
    else:
        levels = {
            "38.2": swing_low + price_range * 0.382,
            "50.0": swing_low + price_range * 0.500,
            "61.8": swing_low + price_range * 0.618,
            "78.6": swing_low + price_range * 0.786,
        }
    return FibLevels(direction=direction, swing_low=swing_low, swing_high=swing_high, levels=levels)


def detect_swing_structure(df: pd.DataFrame, lookback=80) -> Optional[FibLevels]:
    if df is None or len(df) < 30:
        return None

    work = df.tail(lookback).reset_index(drop=True)
    pivot_highs, pivot_lows = find_pivots(work)

    if pivot_highs and pivot_lows:
        latest_high = pivot_highs[-1]
        prior_lows = [idx for idx in pivot_lows if idx < latest_high]
        latest_low = pivot_lows[-1]
        prior_highs = [idx for idx in pivot_highs if idx < latest_low]

        candidates = []
        if prior_lows:
            low_idx = prior_lows[-1]
            candidates.append(("bull", low_idx, latest_high))
        if prior_highs:
            high_idx = prior_highs[-1]
            candidates.append(("bear", high_idx, latest_low))

        if candidates:
            direction, start_idx, end_idx = max(candidates, key=lambda item: item[2])
            if direction == "bull":
                return build_fib_levels(
                    "bull",
                    float(work["low"].iloc[start_idx]),
                    float(work["high"].iloc[end_idx]),
                )
            return build_fib_levels(
                "bear",
                float(work["low"].iloc[end_idx]),
                float(work["high"].iloc[start_idx]),
            )

    return fallback_fib_structure(work, lookback=min(lookback, len(work)))


def structure_is_broken(fib: Optional[FibLevels], price: float) -> bool:
    if fib is None:
        return True
    if fib.direction == "bull" and price < fib.swing_low:
        return True
    if fib.direction == "bear" and price > fib.swing_high:
        return True
    return False


def impulse_fib(df: pd.DataFrame, direction: str, atr: float, lookback=80) -> Optional[FibLevels]:
    if df is None or len(df) < 20 or direction not in ("bull", "bear"):
        return None
    window = df.tail(lookback).reset_index(drop=True)
    min_leg = max(1.5 * max(float(atr), 0.0), 3.0)
    highs, lows = find_pivots(window)
    if direction == "bear":
        indexes = [idx for idx in highs if idx < len(window) - 1]
        if not indexes:
            indexes = [int(window["high"].to_numpy().argmax())]
        for high_idx in reversed(indexes):
            rel = int(window["low"].iloc[high_idx:].to_numpy().argmin())
            low_idx = high_idx + rel
            swing_high = float(window["high"].iloc[high_idx])
            swing_low = float(window["low"].iloc[low_idx])
            if swing_high - swing_low >= min_leg:
                return build_fib_levels("bear", swing_low, swing_high)
        return None
    indexes = [idx for idx in lows if idx < len(window) - 1]
    if not indexes:
        indexes = [int(window["low"].to_numpy().argmin())]
    for low_idx in reversed(indexes):
        rel = int(window["high"].iloc[low_idx:].to_numpy().argmax())
        high_idx = low_idx + rel
        swing_low = float(window["low"].iloc[low_idx])
        swing_high = float(window["high"].iloc[high_idx])
        if swing_high - swing_low >= min_leg:
            return build_fib_levels("bull", swing_low, swing_high)
    return None


def entry_fib_structure(
    df: pd.DataFrame,
    h1_trend: str,
    atr: float,
    lookback=80,
) -> Optional[FibLevels]:
    confirmed = detect_swing_structure(df, lookback=lookback)
    if df is None or len(df) == 0:
        return confirmed
    price = float(df["close"].iloc[-1])
    confirmed_ok = confirmed is not None and not structure_is_broken(confirmed, price)
    if confirmed_ok and (h1_trend not in ("bull", "bear") or confirmed.direction == h1_trend):
        return confirmed
    fresh = impulse_fib(df, h1_trend, atr, lookback=lookback) if h1_trend in ("bull", "bear") else None
    if fresh is not None and not structure_is_broken(fresh, price):
        return fresh
    if confirmed_ok:
        return confirmed
    return fresh or confirmed


def fib_score_for_action(
    action: str,
    fib: Optional[FibLevels],
    price: float,
    atr: float,
    df: Optional[pd.DataFrame] = None,
) -> Tuple[int, str]:
    if fib is None:
        return 0, "无有效斐波那契结构"
    if action == "buy" and fib.direction != "bull":
        return 0, "当前主结构不是多头回踩"
    if action == "sell" and fib.direction != "bear":
        return 0, "当前主结构不是空头回踩"

    tolerance = max(0.35 * atr, 0.5)
    l38 = fib.levels["38.2"]
    l50 = fib.levels["50.0"]
    l61 = fib.levels["61.8"]
    l78 = fib.levels["78.6"]

    recent_probe = price
    if df is not None and len(df) >= 3:
        if action == "buy":
            recent_probe = float(df["low"].tail(3).min())
        elif action == "sell":
            recent_probe = float(df["high"].tail(3).max())

    confirmation_extension = max(0.8 * atr, 1.2)

    if action == "buy":
        core_high = l38 + tolerance
        core_low = l61 - tolerance
        deep_low = l78 - tolerance
        if core_low <= price <= core_high:
            return 30, f"价格位于多头Fib核心回踩区 38.2-61.8 ({l38:.2f}/{l50:.2f}/{l61:.2f})"
        if core_low <= recent_probe <= core_high and price <= core_high + confirmation_extension:
            return 30, f"最近3根探入多头Fib核心区后确认反弹，probe={recent_probe:.2f}, close={price:.2f}"
        if deep_low <= price < core_low:
            return 18, f"价格位于多头深回踩观察区 61.8-78.6 ({l61:.2f}/{l78:.2f})"
        if deep_low <= recent_probe < core_low and price <= core_high + confirmation_extension:
            return 18, f"最近3根探入多头深回踩区后反弹，probe={recent_probe:.2f}, close={price:.2f}"
        return 0, f"价格未进入多头Fib回踩区，price={price:.2f}"

    core_low = l38 - tolerance
    core_high = l61 + tolerance
    deep_high = l78 + tolerance
    if core_low <= price <= core_high:
        return 30, f"价格位于空头Fib核心反弹区 38.2-61.8 ({l38:.2f}/{l50:.2f}/{l61:.2f})"
    if core_low <= recent_probe <= core_high and price >= core_low - confirmation_extension:
        return 30, f"最近3根探入空头Fib核心区后确认回落，probe={recent_probe:.2f}, close={price:.2f}"
    if core_high < price <= deep_high:
        return 18, f"价格位于空头深反弹观察区 61.8-78.6 ({l61:.2f}/{l78:.2f})"
    if core_high < recent_probe <= deep_high and price >= core_low - confirmation_extension:
        return 18, f"最近3根探入空头深反弹区后回落，probe={recent_probe:.2f}, close={price:.2f}"
    return 0, f"价格未进入空头Fib反弹区，price={price:.2f}"


def classify_volume_state(df: pd.DataFrame) -> Dict[str, object]:
    if df is None or len(df) < 25:
        return {
            "score": 0,
            "summary": "成交量样本不足",
            "confirm_ratio": 0.0,
            "pullback_ratio": 0.0,
        }

    volumes = df["tick_volume"].astype(float)
    vol_ma20 = float(volumes.tail(21).iloc[:-1].mean())
    confirm_vol = float(volumes.iloc[-1])
    pullback_vol = float(volumes.iloc[-5:-1].mean())
    if vol_ma20 <= 0:
        return {
            "score": 0,
            "summary": "tick_volume 无效",
            "confirm_ratio": 0.0,
            "pullback_ratio": 0.0,
        }

    confirm_ratio = confirm_vol / vol_ma20
    pullback_ratio = pullback_vol / vol_ma20
    score = 0
    notes = []

    if pullback_ratio <= 0.95:
        score += 8
        notes.append(f"回踩缩量({pullback_ratio:.2f}x)")
    else:
        notes.append(f"回踩未缩量({pullback_ratio:.2f}x)")

    if confirm_ratio >= 1.25:
        score += 17
        notes.append(f"确认K放量({confirm_ratio:.2f}x)")
    elif confirm_ratio >= 1.10:
        score += 11
        notes.append(f"确认K温和放量({confirm_ratio:.2f}x)")
    elif confirm_ratio < 0.55:
        score -= 15
        notes.append(f"异常低量({confirm_ratio:.2f}x)")
    else:
        notes.append(f"确认量普通({confirm_ratio:.2f}x)")

    return {
        "score": max(-15, min(25, score)),
        "summary": "，".join(notes),
        "confirm_ratio": confirm_ratio,
        "pullback_ratio": pullback_ratio,
    }


def candle_parts(row) -> Dict[str, float]:
    open_p = float(row["open"])
    close_p = float(row["close"])
    high_p = float(row["high"])
    low_p = float(row["low"])
    body = abs(close_p - open_p)
    rng = max(high_p - low_p, 1e-9)
    upper = high_p - max(open_p, close_p)
    lower = min(open_p, close_p) - low_p
    return {
        "open": open_p,
        "close": close_p,
        "high": high_p,
        "low": low_p,
        "body": body,
        "range": rng,
        "upper": upper,
        "lower": lower,
    }


def detect_liquidity_sweep(df: pd.DataFrame, action: str, atr: float) -> Tuple[bool, str]:
    if df is None or len(df) < 20 or atr <= 0:
        return False, ""
    work = df.reset_index(drop=True)
    source = work.iloc[:-3]
    if len(source) < 12:
        return False, ""
    highs, lows = find_pivots(source, left=2, right=2)
    recent = work.iloc[-4:]
    if action == "buy" and lows:
        swing = float(source["low"].iloc[lows[-1]])
        swept = None
        for i in range(len(recent) - 1):
            row = recent.iloc[i]
            if float(row["low"]) <= swing - 0.15 * atr and float(row["close"]) > swing:
                swept = row
                break
        if swept is None:
            return False, ""
        last = candle_parts(work.iloc[-1])
        body_top = max(float(swept["open"]), float(swept["close"]))
        if last["close"] > body_top and last["range"] >= 0.3 * atr:
            return True, "M15扫低点后收回"
    elif action == "sell" and highs:
        swing = float(source["high"].iloc[highs[-1]])
        swept = None
        for i in range(len(recent) - 1):
            row = recent.iloc[i]
            if float(row["high"]) >= swing + 0.15 * atr and float(row["close"]) < swing:
                swept = row
                break
        if swept is None:
            return False, ""
        last = candle_parts(work.iloc[-1])
        body_bottom = min(float(swept["open"]), float(swept["close"]))
        if last["close"] < body_bottom and last["range"] >= 0.3 * atr:
            return True, "M15扫高点后收回"
    return False, ""

def classify_price_action(df: pd.DataFrame, action: str, atr: float) -> Tuple[int, str]:
    if df is None or len(df) < 8:
        return 0, "价格行为样本不足"

    last = candle_parts(df.iloc[-1])
    prev = candle_parts(df.iloc[-2])
    prior = df.iloc[-7:-1]
    score = 0
    notes = []
    min_body = max(atr * 0.12, 0.15)

    if action == "buy":
        bullish = last["close"] > last["open"]
        prev_bearish = prev["close"] < prev["open"]
        engulfing = (
            bullish
            and prev_bearish
            and last["close"] >= prev["open"]
            and last["open"] <= prev["close"]
            and last["body"] >= min_body
        )
        lower_rejection = (
            last["lower"] >= max(last["body"] * 1.6, atr * 0.18)
            and last["close"] >= last["low"] + last["range"] * 0.55
        )
        prior_low = float(prior["low"].min())
        false_break = last["low"] < prior_low and last["close"] > prior_low
        retest_confirm = bullish and last["close"] > prev["high"] and last["body"] >= min_body

        if engulfing:
            score += 24
            notes.append("多头吞没")
        if lower_rejection:
            score += 18
            notes.append("长下影拒跌")
        if false_break:
            score += 20
            notes.append("假跌破收回")
        if retest_confirm:
            score += 12
            notes.append("突破前K高点确认")

    elif action == "sell":
        bearish = last["close"] < last["open"]
        prev_bullish = prev["close"] > prev["open"]
        engulfing = (
            bearish
            and prev_bullish
            and last["close"] <= prev["open"]
            and last["open"] >= prev["close"]
            and last["body"] >= min_body
        )
        upper_rejection = (
            last["upper"] >= max(last["body"] * 1.6, atr * 0.18)
            and last["close"] <= last["high"] - last["range"] * 0.55
        )
        prior_high = float(prior["high"].max())
        false_break = last["high"] > prior_high and last["close"] < prior_high
        retest_confirm = bearish and last["close"] < prev["low"] and last["body"] >= min_body

        if engulfing:
            score += 24
            notes.append("空头吞没")
        if upper_rejection:
            score += 18
            notes.append("长上影拒涨")
        if false_break:
            score += 20
            notes.append("假突破收回")
        if retest_confirm:
            score += 12
            notes.append("跌破前K低点确认")

    swept, sweep_note = detect_liquidity_sweep(df, action, atr)
    if swept:
        score += 22
        notes.append(sweep_note)

    return min(score, 30), "，".join(notes) if notes else "无明确价格行为触发"


def trend_score_for_action(action: str, h1_trend: str, m15_trend: str) -> Tuple[int, str]:
    desired = "bull" if action == "buy" else "bear"
    if h1_trend == desired and m15_trend == desired:
        return 15, f"H1/M15 同向支持 {action}"
    if h1_trend == desired:
        return 8, f"H1支持 {action}，M15={m15_trend} 只是回撤"
    if h1_trend == "neutral" and m15_trend == desired:
        return 4, f"H1中性，M15支持 {action}"
    if h1_trend != desired and h1_trend != "neutral":
        return -15, f"H1 与 {action} 不同向"
    return 0, "H1/M15 中性"

def chase_blocks(df: pd.DataFrame, action: str, atr: float) -> Tuple[bool, str]:
    if df is None or len(df) == 0:
        return False, ""
    return chase_blocks_at(df, float(df["close"].iloc[-1]), action, atr)


def chase_blocks_at(df: pd.DataFrame, price: float, action: str, atr: float) -> Tuple[bool, str]:
    if df is None or atr <= 0:
        return False, ""
    ema = ema_last(df["close"], 21)
    if ema is None:
        return False, ""
    distance = (float(price) - ema) / atr
    if action == "buy" and distance > CHASE_MAX_ATR:
        return True, f"价格离M15 EMA21过远({distance:.2f}ATR)，不追多"
    if action == "sell" and distance < -CHASE_MAX_ATR:
        return True, f"价格离M15 EMA21过远({distance:.2f}ATR)，不追空"
    return False, ""


def m5_rejection_anchor(df_m5: pd.DataFrame, action: str, atr: float):
    if df_m5 is None or len(df_m5) < 3 or action not in ("buy", "sell"):
        return None
    window = df_m5.tail(3)
    entry = float(window["close"].iloc[-1])
    last = window.iloc[-1]
    buffer = min(max(0.6, 0.15 * max(float(atr), 0.01)), 1.0)
    if action == "sell":
        if float(last["close"]) >= float(last["open"]):
            return None
        extreme = float(window["high"].max())
        risk = extreme + buffer - entry
        if risk <= 0 or risk > MAX_SL_ABS:
            return None
        return entry, extreme
    if float(last["close"]) <= float(last["open"]):
        return None
    extreme = float(window["low"].min())
    risk = entry - (extreme - buffer)
    if risk <= 0 or risk > MAX_SL_ABS:
        return None
    return entry, extreme


def m5_blocks_entry(df_m5: pd.DataFrame, action: str) -> Tuple[bool, str]:
    if df_m5 is None or len(df_m5) < 3:
        return False, ""
    atr = calculate_atr(df_m5)
    last = candle_parts(df_m5.iloc[-1])
    strong = last["body"] >= last["range"] * 0.65 and last["range"] >= max(0.45 * atr, 0.35)
    if action == "buy" and last["close"] < last["open"] and strong:
        return True, "M5强反向大阴线，只确认时机，不做这一根"
    if action == "sell" and last["close"] > last["open"] and strong:
        return True, "M5强反向大阳线，只确认时机，不做这一根"
    return False, ""

def reward_risk_ratio(action: str, entry_price: float, stop_loss: float, take_profit: float) -> float:
    if action == "buy":
        risk = entry_price - stop_loss
        reward = take_profit - entry_price
    elif action == "sell":
        risk = stop_loss - entry_price
        reward = entry_price - take_profit
    else:
        return 0.0
    if risk <= 0 or reward <= 0:
        return 0.0
    return reward / risk


def directional_stop_distance(action: str, entry_price: float, stop_loss: float) -> float:
    """Return the live directional distance from execution price to stop."""
    if action == "buy":
        return float(entry_price) - float(stop_loss)
    if action == "sell":
        return float(stop_loss) - float(entry_price)
    return 0.0


def review_entry(
    action: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    h1_trend: str,
    m15_trend: str,
    min_rr: float = 1.5,
    enforce_align: bool = True,
) -> Tuple[bool, str]:
    rr = reward_risk_ratio(action, entry_price, stop_loss, take_profit)
    reasons = []
    if enforce_align and not trends_support(action, h1_trend, m15_trend):
        reasons.append(f"{action.upper()} 与H1不同向(H1={h1_trend}, M15={m15_trend})")
    if rr < min_rr:
        reasons.append(f"盈亏比 {rr:.2f} < {min_rr:.2f}")
    if reasons:
        return False, "二审拒绝: " + "，".join(reasons)
    return True, f"二审通过: RR={rr:.2f}, H1={h1_trend}, M15={m15_trend}"

def sl_change_is_tighter(direction: str, current_sl: float, new_sl: float) -> bool:
    if new_sl <= 0:
        return False
    if current_sl <= 0:
        return True
    if direction == "BUY":
        return new_sl > current_sl
    if direction == "SELL":
        return new_sl < current_sl
    return False


def update_guardian_extremes(state: GuardianState, current_price: float) -> None:
    """Track both favorable and adverse extrema for the observed position path."""
    state.highest_price = max(float(state.highest_price), float(current_price))
    state.lowest_price = min(float(state.lowest_price), float(current_price))


def entry_risk_limits(atr: float) -> Tuple[float, float]:
    atr = max(float(atr), 0.01)
    min_risk = max(ENTRY_MIN_SL_ABS, ENTRY_MIN_SL_ATR * atr)
    max_risk = MAX_SL_ABS
    if min_risk >= max_risk:
        min_risk = max(0.3, max_risk - 0.1)
    return min_risk, max_risk



def stop_buffer(atr: float) -> float:
    return min(max(0.6, 0.15 * max(float(atr), 0.01)), 1.0)


def recent_m5_extreme(df_m5: pd.DataFrame, action: str) -> Optional[float]:
    if df_m5 is None or len(df_m5) < 3 or action not in ("buy", "sell"):
        return None
    window = df_m5.tail(3)
    if action == "sell":
        return float(window["high"].max())
    return float(window["low"].min())


def m5_extreme_on_stop_side(action: str, entry_price: float, extreme: Optional[float]) -> bool:
    if extreme is None:
        return False
    if action == "sell":
        return float(extreme) > float(entry_price)
    if action == "buy":
        return float(extreme) < float(entry_price)
    return False


def m5_extreme_risk(action: str, entry_price: float, extreme: float, atr: float) -> float:
    buffer = stop_buffer(atr)
    if action == "sell":
        return float(extreme) + buffer - float(entry_price)
    return float(entry_price) - (float(extreme) - buffer)


def stop_covers_m5_extreme(action: str, stop_loss: float, extreme: float, atr: float) -> bool:
    if stop_loss <= 0:
        return False
    buffer = stop_buffer(atr)
    if action == "sell":
        return float(stop_loss) + 1e-6 >= float(extreme) + buffer
    if action == "buy":
        return float(stop_loss) - 1e-6 <= float(extreme) - buffer
    return False


def m5_wick_reject_text() -> str:
    return "最近3根M5极值加缓冲超过小资金上限 %.2f，拒绝把止损拉进影线" % MAX_SL_ABS


def guarded_structure_price(action: str, entry_price: float, candidates, protect_extreme: Optional[float], buffer: float, max_risk: float) -> Optional[float]:
    if action == "sell":
        usable = [float(item) for item in candidates if float(item) > entry_price]
        if protect_extreme is not None and protect_extreme > entry_price:
            usable = [item for item in usable if item + 1e-9 >= float(protect_extreme)]
            usable.append(float(protect_extreme))
        fitting = [item for item in usable if 0 < item + buffer - entry_price <= max_risk + 0.001]
        return min(fitting) if fitting else None
    usable = [float(item) for item in candidates if float(item) < entry_price]
    if protect_extreme is not None and protect_extreme < entry_price:
        usable = [item for item in usable if item - 1e-9 <= float(protect_extreme)]
        usable.append(float(protect_extreme))
    fitting = [item for item in usable if 0 < entry_price - (item - buffer) <= max_risk + 0.001]
    return max(fitting) if fitting else None


def reject_inside_m5_wick(action, entry, stop_loss, take_profit, atr, fib, df_m5):
    extreme = recent_m5_extreme(df_m5, action)
    if not m5_extreme_on_stop_side(action, entry, extreme):
        return stop_loss, take_profit, ""
    required = m5_extreme_risk(action, entry, extreme, atr)
    if required <= 0 or required > MAX_SL_ABS + 0.001:
        return 0.0, 0.0, m5_wick_reject_text()
    if stop_covers_m5_extreme(action, stop_loss, extreme, atr):
        return stop_loss, take_profit, ""
    new_sl, new_tp = calculate_targets(action, entry, atr, fib, extreme, extreme)
    if new_sl <= 0 or not stop_covers_m5_extreme(action, new_sl, extreme, atr):
        return 0.0, 0.0, m5_wick_reject_text()
    return new_sl, new_tp, "止损改到最近3根M5极值之外"

def calculate_targets(
    action: str,
    entry_price: float,
    atr: float,
    fib: Optional[FibLevels],
    setup_price: Optional[float] = None,
    protect_extreme: Optional[float] = None,
) -> Tuple[float, float]:
    atr = max(atr, 0.01)
    buffer = stop_buffer(atr)
    min_risk, max_risk = entry_risk_limits(atr)

    if action == "buy":
        lows = []
        if fib and fib.swing_low < entry_price:
            lows.append(float(fib.swing_low))
        if setup_price is not None and setup_price < entry_price:
            lows.append(float(setup_price))
        if protect_extreme is not None:
            structure_low = guarded_structure_price("buy", entry_price, lows, protect_extreme, buffer, max_risk)
            if structure_low is None:
                return 0.0, 0.0
        else:
            structure_low = max(lows) if lows else entry_price - 1.2 * atr
        raw_sl = structure_low - buffer
        risk = entry_price - raw_sl
        if risk > max_risk:
            return 0.0, 0.0
        if risk <= 0 or risk < min_risk:
            raw_sl = entry_price - min_risk
        stop_loss = round(raw_sl, 2)
        risk = entry_price - stop_loss
        if risk <= 0 or risk > max_risk + 0.001:
            return 0.0, 0.0
        rr_target = entry_price + 1.8 * risk
        structure_target = fib.swing_high if fib and fib.swing_high > entry_price else rr_target
        structure_reward = structure_target - entry_price
        if structure_target < rr_target and risk > 0 and structure_reward >= MIN_REWARD_RISK * risk:
            take_profit = structure_target
        else:
            take_profit = rr_target
        return stop_loss, round(take_profit, 2)

    highs = []
    if fib and fib.swing_high > entry_price:
        highs.append(float(fib.swing_high))
    if setup_price is not None and setup_price > entry_price:
        highs.append(float(setup_price))
    if protect_extreme is not None:
        structure_high = guarded_structure_price("sell", entry_price, highs, protect_extreme, buffer, max_risk)
        if structure_high is None:
            return 0.0, 0.0
    else:
        structure_high = min(highs) if highs else entry_price + 1.2 * atr
    raw_sl = structure_high + buffer
    risk = raw_sl - entry_price
    if risk > max_risk:
        return 0.0, 0.0
    if risk <= 0 or risk < min_risk:
        raw_sl = entry_price + min_risk
    stop_loss = round(raw_sl, 2)
    risk = stop_loss - entry_price
    if risk <= 0 or risk > max_risk + 0.001:
        return 0.0, 0.0
    rr_target = entry_price - 1.8 * risk
    structure_target = fib.swing_low if fib and fib.swing_low < entry_price else rr_target
    structure_reward = entry_price - structure_target
    if structure_target > rr_target and risk > 0 and structure_reward >= MIN_REWARD_RISK * risk:
        take_profit = structure_target
    else:
        take_profit = rr_target
    return stop_loss, round(take_profit, 2)

def calculate_sigmoid_buffer(max_profit_atr: float) -> float:
    b_max = 1.5
    b_min = 0.5
    inflection_atr = 1.5
    steepness = 2.5
    exponent = steepness * (max_profit_atr - inflection_atr)
    if exponent > 50:
        sigmoid_term = 0.0
    elif exponent < -50:
        sigmoid_term = 1.0
    else:
        sigmoid_term = 1.0 / (1.0 + math.exp(exponent))
    return b_min + (b_max - b_min) * sigmoid_term


def volume_confirm_ratio(df: pd.DataFrame) -> float:
    if df is None or len(df) < 22:
        return 0.0
    volumes = df["tick_volume"].astype(float)
    base = float(volumes.tail(21).iloc[:-1].mean())
    if base <= 0:
        return 0.0
    return float(volumes.iloc[-1]) / base


def is_structure_failure(df: pd.DataFrame, action: str, state: GuardianState, atr: float) -> Tuple[bool, str]:
    if df is None or len(df) < 10:
        return False, "结构数据不足"

    fib = detect_swing_structure(df)
    latest_close = float(df["close"].iloc[-1])
    pa_score, pa_text = classify_price_action(df, "sell" if action == "buy" else "buy", atr)
    vol_ratio = volume_confirm_ratio(df)

    if action == "buy":
        structure_line = fib.levels["78.6"] if fib and fib.direction == "bull" else state.initial_sl
        hard_break = latest_close < min(structure_line, state.entry_price - max(0.3 * atr, 0.5))
        if hard_break and vol_ratio >= STRUCTURE_EXIT_VOLUME_RATIO and pa_score >= 12:
            return True, f"BUY结构失效: close={latest_close:.2f}, line={structure_line:.2f}, vol={vol_ratio:.2f}x, PA={pa_text}"
    else:
        structure_line = fib.levels["78.6"] if fib and fib.direction == "bear" else state.initial_sl
        hard_break = latest_close > max(structure_line, state.entry_price + max(0.3 * atr, 0.5))
        if hard_break and vol_ratio >= STRUCTURE_EXIT_VOLUME_RATIO and pa_score >= 12:
            return True, f"SELL结构失效: close={latest_close:.2f}, line={structure_line:.2f}, vol={vol_ratio:.2f}x, PA={pa_text}"

    return False, "结构未失效"


def score_candidate(
    action: str,
    df_m15: pd.DataFrame,
    fib: Optional[FibLevels],
    atr: float,
    h1_trend: str,
    m15_trend: str,
    adx: float,
):
    price = float(df_m15["close"].iloc[-1])
    fib_score, fib_text = fib_score_for_action(action, fib, price, atr, df_m15)
    volume = classify_volume_state(df_m15)
    pa_score, pa_text = classify_price_action(df_m15, action, atr)
    trend_score, trend_text = trend_score_for_action(action, h1_trend, m15_trend)
    if adx >= 25:
        trend_text = f"{trend_text}；ADX={adx:.1f}趋势清楚"
    elif adx > 0:
        trend_text = f"{trend_text}；ADX={adx:.1f}"

    core_count = int(fib_score >= 25) + int(volume["score"] >= 15) + int(pa_score >= 18)
    score = fib_score + int(volume["score"]) + pa_score + trend_score
    if core_count == 3:
        score += 5
    elif core_count <= 1:
        score -= 12
    if adx < 14 and core_count < 3:
        score -= 8

    score = max(0, min(100, score))
    return score, {
        "fib": fib_text,
        "volume": str(volume["summary"]),
        "price_action": pa_text,
        "trend": trend_text,
        "core_count": str(core_count),
        "pa_score": str(pa_score),
        "fib_score": str(fib_score),
    }



def momentum_score(action: str, setup: str, rsi: float, hist: float, prev_hist: float) -> Tuple[int, str]:
    score = 0
    notes = []
    sign = 1 if action == "buy" else -1
    rsi_dir = rsi if action == "buy" else 100.0 - rsi
    if rsi_dir >= 75:
        score -= 12
        notes.append("RSI已到极端区，不追")
    elif setup == "range_edge" and rsi_dir <= 40:
        score += 10
        notes.append("RSI在区间边缘偏离")
    elif setup == "pullback" and 35 <= rsi_dir <= 55:
        score += 5
        notes.append("RSI回撤降温")
    if sign * hist > 0 or sign * (hist - prev_hist) > 0:
        score += 5
        notes.append("MACD动能同向")
    elif setup == "breakout":
        score -= 8
        notes.append("MACD没跟上突破")
    text = "RSI=%.1f, MACD柱=%.2f" % (rsi, hist)
    if notes:
        text += "，" + "，".join(notes)
    return score, text


def setup_details(fib_text, volume, pa_score, pa_text, trend_text, edge_score):
    return {
        "fib": fib_text,
        "volume": str(volume["summary"]),
        "price_action": pa_text,
        "trend": trend_text,
        "core_count": str(int(edge_score >= 25) + int(volume["score"] >= 15) + int(pa_score >= 18)),
        "pa_score": str(pa_score),
        "fib_score": str(edge_score),
    }


def score_range_edge(action: str, df_m15: pd.DataFrame, atr: float, h1_trend: str, m15_trend: str):
    window = df_m15.tail(RANGE_LOOKBACK)
    low = float(window["low"].min())
    high = float(window["high"].max())
    box = max(high - low, 1e-9)
    box_atr = box / atr
    price = float(df_m15["close"].iloc[-1])
    location = (price - low) / box
    structure = FibLevels("bull" if action == "buy" else "bear", low, high, {})
    volume = classify_volume_state(df_m15)
    pa_score, pa_text = classify_price_action(df_m15, action, atr)
    trend_score, trend_text = trend_score_for_action(action, h1_trend, m15_trend)
    edge_loc = location if action == "buy" else 1.0 - location
    box_text = "24根M15区间 %.2f-%.2f (%.2fATR), location=%.2f" % (low, high, box_atr, location)
    if box_atr < 2.0:
        return 0, setup_details(box_text + "，区间太窄", volume, pa_score, pa_text, trend_text, 0), structure
    if box_atr > 7.0:
        return 0, setup_details(box_text + "，区间过宽，不按震荡做", volume, pa_score, pa_text, trend_text, 0), structure
    if edge_loc > 0.3:
        return 0, setup_details(box_text + "，不在区间边缘", volume, pa_score, pa_text, trend_text, 0), structure
    edge_score = 30 if edge_loc <= 0.18 else 22
    score = edge_score + pa_score + int(volume["score"]) + trend_score
    if pa_score < 12:
        score = min(score, 50)
        pa_text += "，边缘缺少拒绝K线"
    return max(0, min(100, score)), setup_details(box_text, volume, pa_score, pa_text, trend_text, edge_score), structure


def score_breakout(action: str, df_m15: pd.DataFrame, atr: float, h1_trend: str, m15_trend: str):
    work = df_m15.reset_index(drop=True)
    direction = "bull" if action == "buy" else "bear"
    empty_volume = {"score": 0, "summary": "突破样本不足"}
    if len(work) < BREAKOUT_BOX + 26:
        return 0, setup_details("突破样本不足", empty_volume, 0, "", "", 0), None
    box = work.iloc[-(BREAKOUT_BOX + 4):-4]
    recent = work.iloc[-4:]
    low = float(box["low"].min())
    high = float(box["high"].max())
    box_atr = (high - low) / atr
    structure = FibLevels(direction, low, high, {})
    pa_score, pa_text = classify_price_action(df_m15, action, atr)
    trend_score, trend_text = trend_score_for_action(action, h1_trend, m15_trend)
    box_text = "突破前16根箱体 %.2f-%.2f (%.2fATR)" % (low, high, box_atr)
    vol_ma = float(work["tick_volume"].iloc[-24:-4].mean())
    if box_atr > 3.5 or vol_ma <= 0:
        return 0, setup_details(box_text + "，突破前没有收敛", empty_volume, pa_score, pa_text, trend_text, 0), structure
    edge = high if action == "buy" else low
    sign = 1 if action == "buy" else -1
    pushes = recent.iloc[:-1]
    broke = pushes[sign * (pushes["close"] - edge) > 0.1 * atr]
    last = recent.iloc[-1]
    if broke.empty:
        return 0, setup_details(box_text + "，没有收盘突破", empty_volume, pa_score, pa_text, trend_text, 0), structure
    if sign * (float(last["close"]) - edge) <= 0:
        return 0, setup_details(box_text + "，回踩收回箱体，突破失败", empty_volume, pa_score, pa_text, trend_text, 0), structure
    # The retest must happen after the first breakout close; the breakout bar's own wick is still inside the box.
    after = recent.loc[broke.index[0] + 1:]
    retested = False
    if not after.empty:
        probe = float(after["low"].min()) if action == "buy" else float(after["high"].max())
        retested = sign * (probe - edge) <= 0.35 * atr
    break_ratio = float(broke["tick_volume"].max()) / vol_ma
    break_score = 25 if break_ratio >= 1.3 else 15 if break_ratio >= 1.1 else 5
    volume = {"score": break_score, "summary": "突破K量能 %.2fx" % break_ratio}
    retest_score = 30 if retested else 0
    holds = (float(last["close"]) > float(last["open"])) if action == "buy" else (float(last["close"]) < float(last["open"]))
    if holds and pa_score < 12:
        pa_score = 12
        pa_text = (pa_text + "，回踩后收" + ("阳" if action == "buy" else "阴")).strip("，")
    text = box_text + ("，回踩到箱体边缘后守住" if retested else "，没有回踩，属于追突破")
    score = break_score + retest_score + pa_score + trend_score
    if not retested:
        score = min(score, 45)
    return max(0, min(100, score)), setup_details(text, volume, pa_score, pa_text, trend_text, retest_score), structure


def build_fib_volume_pa_decision(
    df_m15: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_m5: pd.DataFrame,
    threshold=80,
    now: Optional[datetime] = None,
) -> StrategyResult:
    atr = calculate_atr(df_m15)
    if atr <= 0:
        return StrategyResult("no", 0, False, "ATR无效，拒绝交易", 0.0, 0.0, 0, 0, {})

    h1_trend = trend_from_ema(df_h1, 50)
    m15_trend = trend_from_ema(df_m15, 50)
    adx = calculate_adx(df_m15)
    regime, regime_text, regime_meta = classify_market_regime(df_m15, atr, adx, h1_trend)
    fib = entry_fib_structure(df_m15, h1_trend, atr)
    price = float(df_m15["close"].iloc[-1])

    def boost_m5(action: str, score: int, details: dict) -> int:
        if int(details.get("fib_score", 0)) < 18 or df_m5 is None or len(df_m5) < 8:
            return score
        m5_atr = calculate_atr(df_m5)
        m5_pa, m5_text = classify_price_action(df_m5, action, m5_atr)
        old_pa = int(details.get("pa_score", 0))
        if m5_pa < 12 or m5_pa <= old_pa:
            return score
        details["pa_score"] = str(m5_pa)
        details["price_action"] = f"{details.get('price_action', '')}；M5拒绝确认: {m5_text}"
        return max(0, min(100, score + m5_pa - old_pa))

    session = trading_session(now)
    rsi = calculate_rsi(df_m15)
    hist, prev_hist = macd_histogram(df_m15)
    picks = {}
    for side in ("buy", "sell"):
        best = None
        for setup in SESSION_SETUPS[session]:
            if setup == "pullback":
                score, details = score_candidate(side, df_m15, fib, atr, h1_trend, m15_trend, adx)
                structure = fib
            elif setup == "range_edge":
                score, details, structure = score_range_edge(side, df_m15, atr, h1_trend, m15_trend)
            else:
                score, details, structure = score_breakout(side, df_m15, atr, h1_trend, m15_trend)
            bonus, momentum_text = momentum_score(side, setup, rsi, hist, prev_hist)
            if score > 0:
                score = boost_m5(side, score, details)
                score = max(0, min(100, score + bonus))
            details["momentum"] = momentum_text
            details["regime"] = regime
            details["regime_text"] = regime_text
            details["location"] = str(regime_meta.get("location", 0.5))
            details["session"] = session
            details["setup"] = setup
            if best is None or score > best[0]:
                best = (score, details, structure)
        picks[side] = best
    buy_score, buy_details, buy_structure = picks["buy"]
    sell_score, sell_details, sell_structure = picks["sell"]

    def label(details: dict) -> str:
        return "[%s/%s] " % (
            SESSION_TEXT.get(details.get("session", session), session),
            SETUP_TEXT.get(details.get("setup", ""), details.get("setup", "")),
        )

    def finish(action: str, score: int, details: dict, fib: Optional[FibLevels]) -> StrategyResult:
        blocks = []
        if details.get("setup") == "pullback":
            regime_block = regime_blocks_entry(
                action, regime, regime_text, regime_meta, int(details.get("pa_score", 0))
            )
            if regime_block:
                blocks.append(regime_block)
        if ALIGN_HARD_FILTER and not trends_support(action, h1_trend, m15_trend):
            blocks.append(f"{action.upper()} 与H1不同向(H1={h1_trend}, M15={m15_trend})")
        blocked, m5_text = m5_blocks_entry(df_m5, action)
        if blocked:
            blocks.append(m5_text)
        setup_price = float(df_m15["low"].tail(3).min() if action == "buy" else df_m15["high"].tail(3).max())
        # Keep the closed M15 close as the planned entry. Pricing M5 rechecks off the latest
        # M5 close was backtested on 2026-10-06: 175 trades, -70 USD vs 101 trades, -1.65 USD.
        entry_price = price
        extreme = recent_m5_extreme(df_m5, action)
        protect = extreme if m5_extreme_on_stop_side(action, entry_price, extreme) else None
        stop_loss, take_profit = 0.0, 0.0
        if protect is not None and m5_extreme_risk(action, entry_price, protect, atr) > MAX_SL_ABS:
            blocks.append(m5_wick_reject_text())
        else:
            stop_loss, take_profit = calculate_targets(action, entry_price, atr, fib, setup_price, protect)
            if stop_loss <= 0 and not blocked:
                timed = m5_rejection_anchor(df_m5, action, atr)
                if timed is not None:
                    entry_price, setup_price = timed
                    extreme = recent_m5_extreme(df_m5, action)
                    protect = extreme if m5_extreme_on_stop_side(action, entry_price, extreme) else None
                    if protect is not None and m5_extreme_risk(action, entry_price, protect, atr) > MAX_SL_ABS:
                        blocks.append(m5_wick_reject_text())
                        stop_loss, take_profit = 0.0, 0.0
                    else:
                        stop_loss, take_profit = calculate_targets(action, entry_price, atr, fib, setup_price, protect)
                        details["entry_note"] = f"M5极值仍在{MAX_SL_ABS:g}美元内"
            if stop_loss > 0 and protect is not None and not stop_covers_m5_extreme(action, stop_loss, protect, atr):
                guarded_sl, guarded_tp, guard_note = reject_inside_m5_wick(
                    action, entry_price, stop_loss, take_profit, atr, fib, df_m5
                )
                if guarded_sl <= 0:
                    blocks.append(guard_note or m5_wick_reject_text())
                    stop_loss, take_profit = 0.0, 0.0
                else:
                    stop_loss, take_profit = guarded_sl, guarded_tp
                    if guard_note:
                        details["entry_note"] = guard_note
        chased, chase_text = chase_blocks_at(df_m15, entry_price, action, atr)
        if chased:
            blocks.append(chase_text)
        details["entry_price"] = f"{entry_price:.2f}"
        prices_ok = (
            action == "buy" and stop_loss > 0 and stop_loss < entry_price and take_profit > entry_price
        ) or (
            action == "sell" and stop_loss > entry_price and take_profit > 0 and take_profit < entry_price
        )
        if not prices_ok:
            if not any("影线" in item for item in blocks):
                blocks.append(f"结构止损超过小资金上限 {MAX_SL_ABS:.2f}，拒绝把止损拉进噪声")
            stop_loss, take_profit = 0.0, 0.0
        approved, review_text = (False, "")
        if prices_ok and not blocks:
            approved, review_text = review_entry(
                action, entry_price, stop_loss, take_profit, h1_trend, m15_trend, MIN_REWARD_RISK, ALIGN_HARD_FILTER
            )
            if not approved:
                blocks.append(review_text)
        elif prices_ok:
            review_text = "未进入盈亏比审核"
        reasoning = label(details) + (
            f"{action.upper()}三策略共振，score={score}。{regime_text}。"
            f"{details['fib']}；{details['volume']}；"
            f"{details['price_action']}；{details['trend']}；{details.get('momentum', '')}。"
        )
        if blocks:
            reasoning += "拒绝: " + "；".join(blocks)
            return StrategyResult("no", score, False, reasoning, stop_loss, take_profit, buy_score, sell_score, details)
        reasoning += review_text
        return StrategyResult(action, score, False, reasoning, stop_loss, take_profit, buy_score, sell_score, details)

    if buy_score >= threshold and sell_score >= threshold and abs(buy_score - sell_score) <= 5:
        reasoning = f"多空评分接近，拒绝追单。BUY={buy_score}, SELL={sell_score}"
        return StrategyResult("no", max(buy_score, sell_score), False, reasoning, 0.0, 0.0, buy_score, sell_score, {})

    if buy_score >= threshold and buy_score > sell_score:
        return finish("buy", buy_score, buy_details, buy_structure)
    if sell_score >= threshold and sell_score > buy_score:
        return finish("sell", sell_score, sell_details, sell_structure)

    best_score = max(buy_score, sell_score)
    best_side = "BUY" if buy_score >= sell_score else "SELL"
    details = buy_details if buy_score >= sell_score else sell_details
    reasoning = label(details) + (
        f"未达到交易阈值。最佳方向={best_side}, score={best_score}, "
        f"BUY={buy_score}, SELL={sell_score}。"
        f"{regime_text}。H1={h1_trend}, M15={m15_trend}。"
        f"{details.get('fib', '')}；{details.get('volume', '')}；"
        f"{details.get('price_action', '')}；{details.get('trend', '')}"
    )
    return StrategyResult("no", best_score, False, reasoning, 0.0, 0.0, buy_score, sell_score, details)

class FibVolumePATrader:
    def __init__(self):
        self.account_info = None
        self.guardian_states: Dict[int, GuardianState] = {}
        self.position_snapshots: Dict[int, PositionSnapshot] = {}
        self.notified_close_tickets: Set[int] = set()
        # Confidence at entry, keyed by position ticket, so close messages can show it.
        self.entry_confidence: Dict[int, str] = {}
        self.last_order_ticket = 0

    def entry_confidence_line(self, ticket: int) -> str:
        return f"开仓置信度: {self.entry_confidence.get(int(ticket), '未知(非本进程开仓或已重启)')}"

    def connect_mt5(self):
        if mt5.initialize():
            mt5.shutdown()

        mt5_paths = [
            r"C:\Users\10057\AppData\Roaming\MetaTrader 5\terminal64.exe",
        ]

        initialized = False
        for path in mt5_paths:
            if os.path.exists(path):
                logger.info(f"尝试MT5路径: {path}")
                if mt5.initialize(path=path):
                    initialized = True
                    logger.info("✅ MT5通过指定路径初始化成功")
                    break

        if not initialized:
            logger.info("尝试默认MT5初始化...")
            if mt5.initialize():
                initialized = True
                logger.info("✅ MT5默认初始化成功")

        if not initialized:
            logger.error("❌ 无法初始化MT5，请确保MT5终端正在运行")
            return False

        if not mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
            logger.error("❌ MT5账户登录失败")
            mt5.shutdown()
            return False

        self.account_info = mt5.account_info()
        if self.account_info is None:
            logger.error("❌ 无法获取账户信息")
            mt5.shutdown()
            return False

        logger.info("✅ MT5账户连接成功")
        logger.info(f"账户: {self.account_info.login}, 余额: {self.account_info.balance}, 净值: {self.account_info.equity}")
        if not mt5.symbol_select(SYMBOL, True):
            logger.error(f"❌ 无法选择交易品种 {SYMBOL}")
            mt5.shutdown()
            return False
        return True

    def get_rates(self, timeframe, count, shift=1) -> pd.DataFrame:
        rates = mt5.copy_rates_from_pos(SYMBOL, timeframe, shift, count)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()
        return normalize_rates(rates)

    def get_context(self):
        df_m15 = self.get_rates(mt5.TIMEFRAME_M15, 160, shift=1)
        df_h1 = self.get_rates(mt5.TIMEFRAME_H1, 120, shift=1)
        df_m5 = self.get_rates(mt5.TIMEFRAME_M5, 40, shift=1)
        if len(df_m15) < 80 or len(df_h1) < 60 or len(df_m5) < 10:
            raise RuntimeError("K线数据不足，无法计算H1/M15策略")
        return df_m15, df_h1, df_m5

    def current_position_count(self):
        positions = mt5.positions_get(symbol=SYMBOL)
        if positions is None:
            return 0
        return len(positions)

    def get_positions(self):
        positions = mt5.positions_get(symbol=SYMBOL)
        return list(positions) if positions else []

    def create_position_snapshot(self, position) -> PositionSnapshot:
        ticket = int(getattr(position, "ticket", 0) or 0)
        pos_type = getattr(position, "type", None)
        now_ts = time.time()
        existing = self.position_snapshots.get(ticket)
        return PositionSnapshot(
            ticket=ticket,
            identifier=int(getattr(position, "identifier", 0) or 0),
            symbol=getattr(position, "symbol", SYMBOL),
            direction="BUY" if pos_type == mt5.POSITION_TYPE_BUY else "SELL",
            volume=float(getattr(position, "volume", 0.0) or 0.0),
            price_open=float(getattr(position, "price_open", 0.0) or 0.0),
            sl=float(getattr(position, "sl", 0.0) or 0.0),
            tp=float(getattr(position, "tp", 0.0) or 0.0),
            profit=float(getattr(position, "profit", 0.0) or 0.0),
            open_time=int(getattr(position, "time", 0) or 0),
            first_seen_at=existing.first_seen_at if existing else now_ts,
            last_seen_at=now_ts,
        )

    def deal_reason_text(self, reason) -> str:
        reason_map = {
            getattr(mt5, "DEAL_REASON_SL", object()): "止损触发",
            getattr(mt5, "DEAL_REASON_TP", object()): "止盈触发",
            getattr(mt5, "DEAL_REASON_SO", object()): "强平/止损强制平仓",
            getattr(mt5, "DEAL_REASON_EXPERT", object()): "EA/脚本平仓",
            getattr(mt5, "DEAL_REASON_CLIENT", object()): "客户端手动平仓",
            getattr(mt5, "DEAL_REASON_MOBILE", object()): "移动端手动平仓",
            getattr(mt5, "DEAL_REASON_WEB", object()): "网页端手动平仓",
        }
        return reason_map.get(reason, f"外部/未知平仓(reason={reason})")

    def find_recent_close_deal(self, ticket: int, snapshot: PositionSnapshot):
        start_time = datetime.now() - timedelta(days=3)
        if snapshot.open_time > 0:
            start_time = datetime.fromtimestamp(snapshot.open_time) - timedelta(minutes=5)

        deals = mt5.history_deals_get(start_time, datetime.now())
        if not deals:
            return None

        position_ids = {int(ticket)}
        if snapshot.identifier:
            position_ids.add(int(snapshot.identifier))

        out_entries = {
            getattr(mt5, "DEAL_ENTRY_OUT", None),
            getattr(mt5, "DEAL_ENTRY_INOUT", None),
            getattr(mt5, "DEAL_ENTRY_OUT_BY", None),
        }

        candidates = []
        fallback_candidates = []
        for deal in deals:
            if getattr(deal, "symbol", None) != snapshot.symbol:
                continue

            deal_entry = getattr(deal, "entry", None)
            if deal_entry not in out_entries:
                continue

            deal_position_id = int(getattr(deal, "position_id", 0) or 0)
            deal_order = int(getattr(deal, "order", 0) or 0)
            if deal_position_id in position_ids or deal_order == int(ticket):
                candidates.append(deal)
                continue

            deal_time = int(getattr(deal, "time", 0) or 0)
            if deal_time and abs(deal_time - int(snapshot.last_seen_at)) <= 3600:
                fallback_candidates.append(deal)

        matched = candidates or fallback_candidates
        if not matched:
            return None

        return sorted(
            matched,
            key=lambda item: (getattr(item, "time_msc", 0), getattr(item, "time", 0)),
            reverse=True,
        )[0]

    def notify_position_closed(
        self,
        ticket: int,
        snapshot: PositionSnapshot,
        deal=None,
        close_reason: Optional[str] = None,
        title_prefix: str = "✅",
    ):
        balance_lines = account_balance_lines()
        if deal is None:
            send_feishu_notification(
                f"⚠️ {snapshot.symbol} {snapshot.direction}平仓",
                [
                    "【平仓结果】",
                    "本次盈亏: 未知（未查到历史成交）",
                    *balance_lines,
                    "【平仓信息】",
                    f"平仓原因: {close_reason or '持仓已消失'}",
                    f"最后浮动盈亏: {format_signed_money(snapshot.profit)}",
                    "【开仓信息】",
                    self.entry_confidence_line(ticket),
                    f"手数/开仓价: {snapshot.volume} / {snapshot.price_open}",
                    f"原止损/止盈: {snapshot.sl} / {snapshot.tp}",
                    f"ticket: {ticket}",
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                ],
            )
            return

        server_reason = self.deal_reason_text(getattr(deal, "reason", None))
        reason = close_reason or server_reason
        profit = float(getattr(deal, "profit", snapshot.profit) or 0.0)
        commission = float(getattr(deal, "commission", 0.0) or 0.0)
        swap = float(getattr(deal, "swap", 0.0) or 0.0)
        fee = float(getattr(deal, "fee", 0.0) or 0.0)
        net_profit = profit + commission + swap + fee

        send_feishu_notification(
            f"{title_prefix} {snapshot.symbol} {snapshot.direction}平仓",
            [
                "【平仓结果】",
                f"本次盈亏: {format_signed_money(net_profit)}",
                *balance_lines,
                "【平仓信息】",
                f"平仓原因: {reason}",
                f"开仓价 → 平仓价: {snapshot.price_open} → {float(getattr(deal, 'price', 0.0) or 0.0)}",
                f"成交盈亏/费用: {format_signed_money(profit)} / {format_signed_money(commission + swap + fee)}",
                "【开仓信息】",
                self.entry_confidence_line(ticket),
                f"手数: {float(getattr(deal, 'volume', snapshot.volume) or snapshot.volume)}",
                f"原止损/止盈: {snapshot.sl} / {snapshot.tp}",
                f"ticket: {ticket}  成交: {getattr(deal, 'ticket', '')}",
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ],
        )

    def sync_position_close_notifications(self):
        positions = self.get_positions()
        current_tickets = set()

        for position in positions:
            ticket = int(getattr(position, "ticket", 0) or 0)
            if ticket <= 0:
                continue
            current_tickets.add(ticket)
            self.position_snapshots[ticket] = self.create_position_snapshot(position)

        disappeared_tickets = set(self.position_snapshots.keys()) - current_tickets
        for ticket in disappeared_tickets:
            snapshot = self.position_snapshots.pop(ticket, None)
            self.guardian_states.pop(ticket, None)
            if snapshot is None:
                continue

            if ticket in self.notified_close_tickets:
                self.notified_close_tickets.discard(ticket)
                continue

            deal = self.find_recent_close_deal(ticket, snapshot)
            self.notify_position_closed(ticket, snapshot, deal)
            self.notified_close_tickets.add(ticket)
            self.entry_confidence.pop(ticket, None)

    def validate_order_prices(self, action, entry_price, stop_loss, take_profit):
        if action == "buy":
            return stop_loss > 0 and stop_loss < entry_price and take_profit > entry_price
        if action == "sell":
            return stop_loss > entry_price and take_profit > 0 and take_profit < entry_price
        return False

    def modify_position_sl(self, position, new_sl: float, reason: str) -> bool:
        current_sl = float(getattr(position, "sl", 0.0) or 0.0)
        current_tp = float(getattr(position, "tp", 0.0) or 0.0)
        ticket = int(getattr(position, "ticket", 0))
        if ticket <= 0 or new_sl <= 0:
            return False
        direction = "BUY" if getattr(position, "type", None) == mt5.POSITION_TYPE_BUY else "SELL"
        if not sl_change_is_tighter(direction, current_sl, new_sl):
            logger.info(f"护盘拒绝放宽止损 ticket={ticket}: {current_sl} -> {round(new_sl, 2)} ({reason})")
            return False

        request_body = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": SYMBOL,
            "position": ticket,
            "sl": round(new_sl, 2),
            "tp": current_tp,
        }
        if not ENABLE_TRADING:
            logger.info(f"🧪 DRY-RUN 修改SL ticket={ticket}: {current_sl} -> {round(new_sl, 2)} ({reason})")
            return True

        result = mt5.order_send(request_body)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"🛡️ 护盘SL更新 ticket={ticket}: {current_sl} -> {round(new_sl, 2)} ({reason})")
            return True
        retcode = result.retcode if result else "None"
        comment = result.comment if result else "无返回"
        logger.warning(f"⚠️ 护盘SL更新失败 ticket={ticket}, retcode={retcode}, comment={comment}")
        return False

    def close_position_by_ticket(self, position, reason: str) -> bool:
        ticket = int(getattr(position, "ticket", 0))
        if ticket <= 0:
            return False

        snapshot = self.create_position_snapshot(position)

        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            logger.error(f"❌ 护盘平仓失败，无法获取 {SYMBOL} 报价")
            return False

        if position.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = float(tick.bid)
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = float(tick.ask)

        if not ENABLE_TRADING:
            logger.warning(f"🧪 DRY-RUN 护盘平仓 ticket={ticket}, reason={reason}, price={price}")
            return True

        request_body = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": SYMBOL,
            "volume": float(position.volume),
            "type": order_type,
            "position": ticket,
            "price": price,
            "deviation": DEVIATION,
            "magic": MAGIC,
            "comment": f"{ORDER_TAG}_GUARDIAN",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request_body)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.warning(f"✅ 护盘单票平仓成功 ticket={ticket}, reason={reason}")
            self.guardian_states.pop(ticket, None)
            self.notified_close_tickets.add(ticket)
            close_deal = None
            for _ in range(3):
                close_deal = self.find_recent_close_deal(ticket, snapshot)
                if close_deal is not None:
                    break
                time.sleep(0.2)
            self.notify_position_closed(
                ticket,
                snapshot,
                close_deal,
                close_reason=reason,
                title_prefix="🛡️",
            )
            self.entry_confidence.pop(ticket, None)
            return True

        retcode = result.retcode if result else "None"
        comment = result.comment if result else "无返回"
        logger.error(f"❌ 护盘平仓失败 ticket={ticket}, retcode={retcode}, comment={comment}")
        return False

    def get_or_create_guardian_state(self, position, tick) -> GuardianState:
        ticket = int(position.ticket)
        direction = "BUY" if position.type == mt5.POSITION_TYPE_BUY else "SELL"
        entry = float(position.price_open)
        sl = float(position.sl or 0.0)
        tp = float(position.tp or 0.0)

        state = self.guardian_states.get(ticket)
        if state and state.direction == direction:
            return state

        if direction == "BUY":
            initial_risk = entry - sl if sl > 0 and sl < entry else max(1.0, entry * 0.001)
            highest = max(entry, float(tick.bid))
            lowest = entry
        else:
            initial_risk = sl - entry if sl > entry else max(1.0, entry * 0.001)
            highest = entry
            lowest = min(entry, float(tick.ask))

        state = GuardianState(
            ticket=ticket,
            direction=direction,
            entry_price=entry,
            initial_sl=sl,
            initial_tp=tp,
            risk=max(initial_risk, 0.01),
            highest_price=highest,
            lowest_price=lowest,
        )
        self.guardian_states[ticket] = state
        logger.info(f"🛡️ 初始化护盘状态 ticket={ticket}, direction={direction}, risk={state.risk:.2f}")
        return state

    def run_guardian(self):
        if not GUARDIAN_ENABLED:
            return

        positions = self.get_positions()
        active_tickets = {int(pos.ticket) for pos in positions}
        for ticket in list(self.guardian_states.keys()):
            if ticket not in active_tickets:
                self.guardian_states.pop(ticket, None)

        if not positions:
            return

        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            logger.warning("护盘跳过：无法获取当前报价")
            return

        df_m5 = self.get_rates(mt5.TIMEFRAME_M5, 120, shift=1)
        atr = calculate_atr(df_m5) if len(df_m5) >= 20 else 0.0
        if atr <= 0:
            logger.warning("护盘跳过：ATR无效")
            return

        now_ts = time.time()
        disaster_buffer = max(REAL_SL_DISASTER_MIN, REAL_SL_DISASTER_ATR * atr)
        min_modify_step = max(MIN_SL_MODIFY_ABS, MIN_SL_MODIFY_ATR * atr)

        for position in positions:
            state = self.get_or_create_guardian_state(position, tick)
            current_sl = float(position.sl or 0.0)

            if position.type == mt5.POSITION_TYPE_BUY:
                current_price = float(tick.bid)
                update_guardian_extremes(state, current_price)
                max_profit = max(0.0, state.highest_price - state.entry_price)
                max_profit_atr = max_profit / max(atr, 0.01)
                max_profit_r = max_profit / max(state.risk, 0.01)
                buffer_mult = calculate_sigmoid_buffer(max_profit_atr)
                virtual_sl = state.highest_price - buffer_mult * atr
                state.virtual_sl = max(state.virtual_sl, virtual_sl) if state.virtual_sl > 0 else virtual_sl

                if not state.breakeven_done and (
                    max_profit_r >= BREAKEVEN_TRIGGER_R or max_profit >= BREAKEVEN_TRIGGER_ATR * atr
                ):
                    be_sl = round(state.entry_price + BREAKEVEN_BUFFER_ATR * atr, 2)
                    if be_sl < current_price and (current_sl == 0.0 or be_sl > current_sl + min_modify_step):
                        if self.modify_position_sl(position, be_sl, "breakeven"):
                            state.breakeven_done = True

                target_real_sl = round(state.virtual_sl - disaster_buffer, 2)
                if target_real_sl > 0 and target_real_sl < current_price and (
                    current_sl == 0.0 or target_real_sl > current_sl + min_modify_step
                ):
                    self.modify_position_sl(position, target_real_sl, "virtual disaster SL")

                structure_failed, structure_reason = is_structure_failure(df_m5, "buy", state, atr)
                if structure_failed:
                    self.close_position_by_ticket(position, structure_reason)
                    continue

                if state.virtual_sl > 0 and current_price <= state.virtual_sl:
                    if state.breach_started_at is None:
                        state.breach_started_at = now_ts
                        logger.warning(f"⚠️ BUY触碰虚拟护盘线 ticket={state.ticket}, price={current_price:.2f}, virtualSL={state.virtual_sl:.2f}")
                    elif now_ts - state.breach_started_at >= VIRTUAL_CONFIRM_SECONDS:
                        self.close_position_by_ticket(position, f"BUY持续跌破虚拟SL {state.virtual_sl:.2f}")
                        continue
                else:
                    state.breach_started_at = None

            elif position.type == mt5.POSITION_TYPE_SELL:
                current_price = float(tick.ask)
                update_guardian_extremes(state, current_price)
                max_profit = max(0.0, state.entry_price - state.lowest_price)
                max_profit_atr = max_profit / max(atr, 0.01)
                max_profit_r = max_profit / max(state.risk, 0.01)
                buffer_mult = calculate_sigmoid_buffer(max_profit_atr)
                virtual_sl = state.lowest_price + buffer_mult * atr
                state.virtual_sl = min(state.virtual_sl, virtual_sl) if state.virtual_sl > 0 else virtual_sl

                if not state.breakeven_done and (
                    max_profit_r >= BREAKEVEN_TRIGGER_R or max_profit >= BREAKEVEN_TRIGGER_ATR * atr
                ):
                    be_sl = round(state.entry_price - BREAKEVEN_BUFFER_ATR * atr, 2)
                    if be_sl > current_price and (current_sl == 0.0 or be_sl < current_sl - min_modify_step):
                        if self.modify_position_sl(position, be_sl, "breakeven"):
                            state.breakeven_done = True

                target_real_sl = round(state.virtual_sl + disaster_buffer, 2)
                if target_real_sl > current_price and (
                    current_sl == 0.0 or target_real_sl < current_sl - min_modify_step
                ):
                    self.modify_position_sl(position, target_real_sl, "virtual disaster SL")

                structure_failed, structure_reason = is_structure_failure(df_m5, "sell", state, atr)
                if structure_failed:
                    self.close_position_by_ticket(position, structure_reason)
                    continue

                if state.virtual_sl > 0 and current_price >= state.virtual_sl:
                    if state.breach_started_at is None:
                        state.breach_started_at = now_ts
                        logger.warning(f"⚠️ SELL触碰虚拟护盘线 ticket={state.ticket}, price={current_price:.2f}, virtualSL={state.virtual_sl:.2f}")
                    elif now_ts - state.breach_started_at >= VIRTUAL_CONFIRM_SECONDS:
                        self.close_position_by_ticket(position, f"SELL持续突破虚拟SL {state.virtual_sl:.2f}")
                        continue
                else:
                    state.breach_started_at = None

    def place_order(self, result: StrategyResult):
        action = result.action
        if action not in ("buy", "sell"):
            return False

        self.last_order_ticket = 0

        positions_count = self.current_position_count()
        if positions_count >= MAX_POSITIONS:
            logger.info(f"✋ 已有 {positions_count} 个 {SYMBOL} 持仓，达到上限 {MAX_POSITIONS}，不再开仓")
            return False

        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            logger.error(f"❌ 无法获取 {SYMBOL} 当前报价")
            return False

        if action == "buy":
            order_type = mt5.ORDER_TYPE_BUY
            price = float(tick.ask)
            comment = f"{ORDER_TAG}_BUY"
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = float(tick.bid)
            comment = f"{ORDER_TAG}_SELL"

        if not self.validate_order_prices(action, price, result.stop_loss, result.take_profit):
            logger.error(
                f"❌ SL/TP方向非法，拒绝下单: action={action}, price={price}, "
                f"SL={result.stop_loss}, TP={result.take_profit}"
            )
            return False

        live_risk = directional_stop_distance(action, price, result.stop_loss)
        if live_risk <= 0 or live_risk > MAX_SL_ABS + 0.001:
            logger.info(
                f"二审拒绝下单: 实时止损距离 {live_risk:.2f} > {MAX_SL_ABS:.2f}, "
                f"action={action}, price={price}, SL={result.stop_loss}"
            )
            return False

        live_rr = reward_risk_ratio(action, price, result.stop_loss, result.take_profit)
        if live_rr < MIN_REWARD_RISK:
            logger.info(
                f"二审拒绝下单: 实时盈亏比 {live_rr:.2f} < {MIN_REWARD_RISK:.2f}, "
                f"price={price}, SL={result.stop_loss}, TP={result.take_profit}"
            )
            return False

        if not ENABLE_TRADING:
            logger.info(
                f"🧪 DRY-RUN 信号: {action.upper()} {SYMBOL}, price={price}, "
                f"SL={result.stop_loss}, TP={result.take_profit}, confidence={result.confidence}"
            )
            send_feishu_notification(
                f"🧪 {SYMBOL} {TITLE_TAG}信号(DRY-RUN)",
                [
                    f"方向: {action.upper()}",
                    f"手数: {LOT_SIZE}",
                    f"参考价: {price}",
                    f"止损: {result.stop_loss}",
                    f"止盈: {result.take_profit}",
                    f"置信度: {result.confidence}",
                    f"理由: {result.reasoning}",
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                ],
            )
            return True

        request_body = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": SYMBOL,
            "volume": LOT_SIZE,
            "type": order_type,
            "price": price,
            "sl": result.stop_loss,
            "tp": result.take_profit,
            "deviation": DEVIATION,
            "magic": MAGIC,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        order_result = mt5.order_send(request_body)
        if order_result is None:
            logger.error("❌ 订单发送失败: 返回 None")
            send_feishu_notification(
                f"❌ {SYMBOL} {TITLE_TAG}下单失败",
                [
                    f"方向: {action.upper()}",
                    "原因: MT5 order_send 返回 None",
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                ],
            )
            return False

        if order_result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"❌ 订单发送失败: retcode={order_result.retcode}, comment={order_result.comment}")
            send_feishu_notification(
                f"❌ {SYMBOL} {TITLE_TAG}下单失败",
                [
                    f"方向: {action.upper()}",
                    f"手数: {LOT_SIZE}",
                    f"价格: {price}",
                    f"止损: {result.stop_loss}",
                    f"止盈: {result.take_profit}",
                    f"retcode: {order_result.retcode}",
                    f"comment: {order_result.comment}",
                    f"理由: {result.reasoning}",
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                ],
            )
            return False

        logger.info(f"✅ 下单成功: {action.upper()} {LOT_SIZE} {SYMBOL}, price={price}, SL={result.stop_loss}, TP={result.take_profit}")
        # On netting/hedging MT5 the new position ticket equals the opening order ticket.
        order_ticket = int(getattr(order_result, "order", 0) or 0)
        self.last_order_ticket = order_ticket
        if order_ticket > 0:
            self.entry_confidence[order_ticket] = (
                f"{result.confidence} (BUY={result.score_buy} SELL={result.score_sell})"
            )
        try:
            self.sync_position_close_notifications()
        except Exception as exc:
            logger.warning(f"下单后同步持仓快照失败: {type(exc).__name__}: {exc}")
        send_feishu_notification(
            f"✅ {SYMBOL} {TITLE_TAG}开仓成功",
            [
                f"方向: {action.upper()}",
                f"手数: {LOT_SIZE}",
                f"成交价: {price}",
                f"止损: {result.stop_loss}",
                f"止盈: {result.take_profit}",
                    f"盈亏比: {live_rr:.2f}",
                f"置信度: {result.confidence}",
                f"BUY评分: {result.score_buy}",
                f"SELL评分: {result.score_sell}",
                f"理由: {result.reasoning}",
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ],
        )
        return True

    def evaluate_once(self):
        df_m15, df_h1, df_m5 = self.get_context()
        result = build_fib_volume_pa_decision(df_m15, df_h1, df_m5, CONFIDENCE_THRESHOLD)
        logger.info(
            f"📊 三策略评分: action={result.action}, confidence={result.confidence}, "
            f"BUY={result.score_buy}, SELL={result.score_sell}, SL={result.stop_loss}, TP={result.take_profit}"
        )
        logger.info(f"🧠 策略理由: {result.reasoning}")
        if result.action in ("buy", "sell") and result.confidence >= CONFIDENCE_THRESHOLD:
            self.place_order(result)
        return result

    def run(self, once=False):
        if not self.connect_mt5():
            return 1

        logger.info(
            f"🚀 XAUUSD 三策略交易脚本启动: symbol={SYMBOL}, entry=M15, direction=H1, "
            f"lot={LOT_SIZE}, max_positions={MAX_POSITIONS}, threshold={CONFIDENCE_THRESHOLD}, "
            f"min_rr={MIN_REWARD_RISK}, align={'ON' if ALIGN_HARD_FILTER else 'OFF'}, "
            f"max_sl={MAX_SL_ABS}, chase={CHASE_MAX_ATR}ATR, trading={'ON' if ENABLE_TRADING else 'DRY-RUN'}"
        )

        last_bar_time = 0
        last_m5_time = 0
        if ENABLE_TRADING:
            logger.info("下单已开启，启动后立即评估最新已收盘M15")
        else:
            logger.info("下单关闭，仅评估不下单")
        try:
            while True:
                try:
                    self.run_guardian()
                except Exception as exc:
                    logger.error(f"护盘异常: {type(exc).__name__}: {exc}")

                try:
                    self.sync_position_close_notifications()
                except Exception as exc:
                    logger.error(f"持仓平仓监听异常: {type(exc).__name__}: {exc}")

                rates = mt5.copy_rates_from_pos(SYMBOL, ENTRY_BAR_TIMEFRAME, 0, 1)
                m5_rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 1)
                if rates is None or len(rates) == 0:
                    logger.warning("无法获取最新K线，等待重试")
                    time.sleep(POLL_SECONDS)
                    continue

                current_bar_time = int(rates[0]["time"])
                current_m5_time = int(m5_rates[0]["time"]) if m5_rates is not None and len(m5_rates) else 0
                if current_bar_time != last_bar_time or current_m5_time != last_m5_time:
                    last_bar_time = current_bar_time
                    last_m5_time = current_m5_time
                    try:
                        self.evaluate_once()
                    except Exception as exc:
                        logger.error(f"策略评估异常: {type(exc).__name__}: {exc}")

                    if once:
                        return 0

                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            logger.info("收到人工停止信号")
            return 0
        finally:
            mt5.shutdown()
            logger.info("MT5 已关闭")

def make_test_df(prices: List[float], volumes: List[float]) -> pd.DataFrame:
    rows = []
    for i, price in enumerate(prices):
        prev = prices[i - 1] if i > 0 else price
        open_p = prev
        close_p = price
        high_p = max(open_p, close_p) + 0.25
        low_p = min(open_p, close_p) - 0.25
        rows.append({
            "time": i,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "tick_volume": volumes[i],
        })
    return pd.DataFrame(rows)


def run_self_test():
    def ohlc_frame(rows, volumes):
        return pd.DataFrame([
            {
                "time": i,
                "open": row[0],
                "high": row[1],
                "low": row[2],
                "close": row[3],
                "tick_volume": volumes[i],
            }
            for i, row in enumerate(rows)
        ])

    def with_volume_spike(rows):
        volumes = [100] * len(rows)
        for i in range(len(rows) - 5, len(rows) - 1):
            volumes[i] = 70
        volumes[-1] = 160
        return volumes

    def tight_buy_df():
        rows = []
        price = 100.0
        for _ in range(68):
            opened = price
            closed = round(price + 0.06, 4)
            rows.append((opened, closed + 0.05, opened - 0.05, closed))
            price = closed
        rows.extend([
            (104.08, 104.20, 103.70, 103.85),
            (103.85, 103.95, 103.20, 103.35),
            (103.35, 103.50, 102.70, 102.90),
            (102.90, 103.40, 102.85, 103.30),
            (103.30, 104.10, 103.20, 104.00),
            (104.00, 105.00, 103.90, 104.85),
            (104.85, 106.10, 104.75, 105.90),
            (105.90, 106.05, 105.40, 105.55),
            (105.55, 105.70, 104.90, 105.05),
            (105.05, 105.15, 104.20, 104.55),
            (104.55, 105.20, 104.15, 105.05),
        ])
        return ohlc_frame(rows, with_volume_spike(rows))

    def tight_sell_df():
        rows = []
        price = 120.0
        for _ in range(68):
            opened = price
            closed = round(price - 0.06, 4)
            rows.append((opened, opened + 0.05, closed - 0.05, closed))
            price = closed
        rows.extend([
            (price, price + 0.15, price - 0.1, price + 0.05),
            (price + 0.05, price + 0.7, price - 0.05, price + 0.55),
            (price + 0.55, price + 1.2, price + 0.45, price + 1.05),
            (price + 1.05, price + 1.1, price + 0.5, price + 0.6),
            (price + 0.6, price + 0.7, price - 0.2, price - 0.05),
            (price - 0.05, price + 0.1, price - 0.9, price - 0.75),
            (price - 0.75, price - 0.6, price - 1.7, price - 1.55),
            (price - 1.55, price - 1.2, price - 1.65, price - 1.3),
            (price - 1.3, price - 0.9, price - 1.4, price - 1.0),
            (price - 1.0, price - 0.4, price - 1.1, price - 0.5),
            (price - 0.5, price - 0.35, price - 1.15, price - 1.0),
        ])
        return ohlc_frame(rows, with_volume_spike(rows))

    h1_bull = make_test_df([100 + i * 0.15 for i in range(90)], [100] * 90)
    h1_bear = make_test_df([130 - i * 0.15 for i in range(90)], [100] * 90)
    m5_quiet = make_test_df([105.0, 105.05, 105.02, 105.04], [100] * 4)
    buy_df = tight_buy_df()
    sell_df = tight_sell_df()

    result_buy = build_fib_volume_pa_decision(buy_df, h1_bull, m5_quiet, 80)
    assert result_buy.action == "buy", result_buy
    buy_entry = float(buy_df["close"].iloc[-1])
    buy_risk = buy_entry - result_buy.stop_loss
    assert 0 < buy_risk <= MAX_SL_ABS + 0.02, buy_risk
    assert reward_risk_ratio("buy", buy_entry, result_buy.stop_loss, result_buy.take_profit) >= MIN_REWARD_RISK
    assert directional_stop_distance("buy", 108.1, 100.0) > MAX_SL_ABS
    assert 0 < directional_stop_distance("buy", 107.9, 100.0) <= MAX_SL_ABS

    result_sell = build_fib_volume_pa_decision(sell_df, h1_bear, m5_quiet, 80)
    assert result_sell.action == "sell", result_sell
    sell_entry = float(sell_df["close"].iloc[-1])
    sell_risk = result_sell.stop_loss - sell_entry
    assert 0 < sell_risk <= MAX_SL_ABS + 0.02, sell_risk
    assert reward_risk_ratio("sell", sell_entry, result_sell.stop_loss, result_sell.take_profit) >= MIN_REWARD_RISK
    assert directional_stop_distance("sell", 91.9, 100.0) > MAX_SL_ABS
    assert 0 < directional_stop_distance("sell", 92.1, 100.0) <= MAX_SL_ABS

    misaligned = build_fib_volume_pa_decision(buy_df, h1_bear, m5_quiet, 80)
    assert misaligned.action == "no", misaligned
    assert "不同向" in misaligned.reasoning, misaligned.reasoning

    base_up = [100 + i * 0.5 for i in range(40)]
    wide_prices = base_up + [120, 118.5, 117.0, 115.5, 114.0, 112.3, 111.8, 113.4]
    wide_volumes = [100] * len(base_up) + [80, 78, 75, 73, 70, 72, 70, 145]
    wide_df = make_test_df(wide_prices, wide_volumes)
    wide_df.iloc[-1, wide_df.columns.get_loc("open")] = 111.7
    wide_df.iloc[-1, wide_df.columns.get_loc("close")] = 113.7
    wide_df.iloc[-1, wide_df.columns.get_loc("low")] = 111.2
    wide_fib = detect_swing_structure(wide_df)
    wide_sl, wide_tp = calculate_targets("buy", 113.7, calculate_atr(wide_df), wide_fib)
    assert wide_sl == 0.0 and wide_tp == 0.0, (wide_sl, wide_tp)
    buried_sl, buried_tp = calculate_targets("buy", 113.7, calculate_atr(wide_df), wide_fib, 100.0)
    assert buried_sl == 0.0 and buried_tp == 0.0, (buried_sl, buried_tp)
    wide_m5 = ohlc_frame([
        (113.4, 113.8, 113.1, 113.5),
        (113.5, 113.9, 113.2, 113.6),
        (113.6, 113.9, 113.3, 113.7),
    ], [100, 100, 100])
    wide_result = build_fib_volume_pa_decision(wide_df, h1_bull, wide_m5, 80)
    assert wide_result.action == "buy", wide_result
    wide_entry = float(wide_df["close"].iloc[-1])
    wide_risk = wide_entry - wide_result.stop_loss
    assert 0 < wide_risk <= MAX_SL_ABS + 0.02, wide_risk
    assert wide_result.stop_loss > 105.0, wide_result.stop_loss
    assert reward_risk_ratio("buy", wide_entry, wide_result.stop_loss, wide_result.take_profit) >= MIN_REWARD_RISK

    poor_sl, poor_tp = calculate_targets("buy", 100.0, 2.0, FibLevels("bull", 70.0, 101.0, {}))
    assert poor_sl == 0.0 and poor_tp == 0.0, (poor_sl, poor_tp)
    _, max_risk = entry_risk_limits(10.0)
    assert max_risk == MAX_SL_ABS

    m5_bad = make_test_df([105.2, 105.3, 105.1, 104.9, 104.2], [100] * 5)
    m5_bad.iloc[-1, m5_bad.columns.get_loc("open")] = 105.3
    m5_bad.iloc[-1, m5_bad.columns.get_loc("close")] = 103.6
    m5_bad.iloc[-1, m5_bad.columns.get_loc("high")] = 105.4
    m5_bad.iloc[-1, m5_bad.columns.get_loc("low")] = 103.4
    blocked = build_fib_volume_pa_decision(buy_df, h1_bull, m5_bad, 80)
    assert blocked.action == "no" and "M5" in blocked.reasoning, blocked.reasoning

    flat_df = make_test_df([100 + math.sin(i / 3) * 0.2 for i in range(80)], [100] * 80)
    result_no = build_fib_volume_pa_decision(flat_df, h1_bull, m5_quiet, 80)
    assert result_no.action == "no", result_no
    flat_regime, flat_text, _ = classify_market_regime(
        flat_df, calculate_atr(flat_df), calculate_adx(flat_df), "bull"
    )
    assert flat_regime == "tight_range", (flat_regime, flat_text)
    buy_regime, buy_regime_text, _ = classify_market_regime(
        buy_df, calculate_atr(buy_df), calculate_adx(buy_df), "bull"
    )
    assert buy_regime == "trend", (buy_regime, buy_regime_text)

    assert not sl_change_is_tighter("BUY", 100.0, 99.0)
    assert sl_change_is_tighter("BUY", 100.0, 101.0)
    assert not sl_change_is_tighter("SELL", 110.0, 111.0)
    assert sl_change_is_tighter("SELL", 110.0, 109.0)
    assert sl_change_is_tighter("BUY", 0.0, 99.0)
    extreme_state = GuardianState(1, "BUY", 100.0, 96.0, 108.0, 4.0, 100.0, 100.0)
    update_guardian_extremes(extreme_state, 98.5)
    update_guardian_extremes(extreme_state, 103.0)
    assert extreme_state.highest_price == 103.0 and extreme_state.lowest_price == 98.5, extreme_state

    fail_prices = [100 + i * 0.5 for i in range(40)] + [120, 118, 116, 114, 112, 109, 103.8]
    fail_volumes = [100] * 40 + [90, 88, 85, 82, 80, 140, 155]
    fail_df = make_test_df(fail_prices, fail_volumes)
    fail_df.iloc[-1, fail_df.columns.get_loc("open")] = 109.0
    fail_df.iloc[-1, fail_df.columns.get_loc("close")] = 103.8
    fail_df.iloc[-1, fail_df.columns.get_loc("low")] = 103.2
    fail_atr = calculate_atr(fail_df)
    fail_state = GuardianState(
        ticket=1,
        direction="BUY",
        entry_price=113.7,
        initial_sl=98.95,
        initial_tp=120.25,
        risk=14.75,
        highest_price=120.25,
        lowest_price=113.7,
    )
    failed, fail_reason = is_structure_failure(fail_df, "buy", fail_state, fail_atr)
    assert failed, fail_reason

    assert trends_support("sell", "bear", "bull")
    assert not trends_support("buy", "bear", "bull")
    near_sl, near_tp = calculate_targets("sell", 100.0, 6.5, FibLevels("bear", 90.0, 110.0, {}), 101.2)
    assert 0 < near_sl - 100.0 <= MAX_SL_ABS + 0.02, (near_sl, near_tp)
    far_sl, far_tp = calculate_targets("sell", 100.0, 6.5, FibLevels("bear", 90.0, 110.0, {}), 100.0 + MAX_SL_ABS)
    assert far_sl == 0.0 and far_tp == 0.0, (far_sl, far_tp)

    rows = []
    price = 100.0
    for i in range(50):
        nxt = 100 + i * 0.4
        rows.append((price, max(price, nxt) + 0.2, min(price, nxt) - 0.2, nxt))
        price = nxt
    for nxt in (118, 114, 110, 106, 102):
        rows.append((price, price + 0.2, nxt, nxt))
        price = nxt
    rows.append((102, 109.2, 101.5, 108.4))
    dump_df = ohlc_frame(rows, [100] * len(rows))
    dump_atr = max(calculate_atr(dump_df), 0.5)
    stale = detect_swing_structure(dump_df)
    active = entry_fib_structure(dump_df, "bear", dump_atr)
    assert stale is None or stale.direction == "bull", stale
    assert active is not None and active.direction == "bear", active
    assert active.swing_high > 115 and active.swing_low < 104, active
    zone_score, zone_text = fib_score_for_action("sell", active, active.levels["50.0"], dump_atr, None)
    assert zone_score == 30, (zone_score, zone_text, active)
    low_score, low_text = fib_score_for_action("sell", active, active.swing_low + 0.2, dump_atr, None)
    assert low_score == 0, (low_score, low_text, active)

    far_m5 = ohlc_frame([(100, 110, 99, 100), (100, 110.5, 99, 100), (100, 111, 99, 100)], [100, 100, 100])
    assert m5_rejection_anchor(far_m5, "sell", 2.0) is None
    near_m5 = ohlc_frame([(108, 109.2, 107.4, 107.6), (107.6, 109.0, 107.2, 107.4), (107.4, 109.1, 106.8, 107.0)], [100, 100, 100])
    near_anchor = m5_rejection_anchor(near_m5, "sell", 2.0)
    assert near_anchor is not None and near_anchor[1] - near_anchor[0] <= MAX_SL_ABS, near_anchor
    min_risk_hi, _ = entry_risk_limits(10.0)
    assert min_risk_hi < MAX_SL_ABS, min_risk_hi
    near_sl_hi, near_tp_hi = calculate_targets("sell", 100.0, 10.0, None, 102.2)
    assert 0 < near_sl_hi - 100.0 <= MAX_SL_ABS + 0.02, (near_sl_hi, near_tp_hi)
    far_sl_hi, far_tp_hi = calculate_targets("sell", 100.0, 10.0, None, 100.0 + MAX_SL_ABS)
    assert far_sl_hi == 0.0 and far_tp_hi == 0.0, (far_sl_hi, far_tp_hi)

    covered_sl, covered_tp = calculate_targets(
        "sell", 100.0, 2.0, FibLevels("bear", 90.0, 101.2, {}), 101.2, 102.0
    )
    assert covered_sl >= 102.6 - 0.01, (covered_sl, covered_tp)
    inside_sl, inside_tp = calculate_targets(
        "sell", 100.0, 2.0, FibLevels("bear", 90.0, 101.2, {}), 101.2, 100.0 + MAX_SL_ABS + 1.0
    )
    assert inside_sl == 0.0 and inside_tp == 0.0, (inside_sl, inside_tp)
    sell_close = float(sell_df["close"].iloc[-1])
    wick_m5 = ohlc_frame([
        (sell_close, sell_close + MAX_SL_ABS + 1.4, sell_close - 0.3, sell_close - 0.1),
        (sell_close - 0.1, sell_close + MAX_SL_ABS + 1.2, sell_close - 0.4, sell_close - 0.2),
        (sell_close - 0.2, sell_close + MAX_SL_ABS + 1.0, sell_close - 0.5, sell_close - 0.3),
    ], [80, 80, 90])
    wicked = build_fib_volume_pa_decision(sell_df, h1_bear, wick_m5, 0)
    assert wicked.action == "no" and "影线" in wicked.reasoning, wicked

    bj = lambda hour: datetime(2026, 10, 6, hour, 0, tzinfo=BEIJING_TZ)
    assert [trading_session(bj(h)) for h in (9, 16, 21, 0, 3)] == ["asia", "london", "overlap", "overlap", "late_us"]
    brk_rows = []
    level = 100.0
    for _ in range(50):
        nxt = level + 0.2
        brk_rows.append((level, nxt + 0.3, level - 0.3, nxt))
        level = nxt
    for i in range(16):
        o = 110.4 + (0.3 if i % 2 else -0.3)
        c = 110.4 + (-0.3 if i % 2 else 0.3)
        brk_rows.append((o, 111.0, 109.8, c))
    brk_rows += [(110.6, 112.4, 110.5, 112.2), (112.2, 112.6, 111.6, 111.8),
                 (111.8, 112.0, 111.0, 111.3), (111.3, 112.1, 111.05, 111.95)]
    brk_vols = [100] * len(brk_rows)
    brk_vols[-4] = 190
    brk_df = ohlc_frame(brk_rows, brk_vols)
    brk_m5 = ohlc_frame([(111.7, 112.0, 111.5, 111.8), (111.8, 112.0, 111.6, 111.9), (111.9, 112.0, 111.7, 111.95)], [100] * 3)
    london = build_fib_volume_pa_decision(brk_df, h1_bull, brk_m5, 70, now=bj(16))
    assert london.action == "buy" and london.details.get("setup") == "breakout", london
    assert 0 < 111.95 - london.stop_loss <= MAX_SL_ABS + 0.02, london
    asia = build_fib_volume_pa_decision(brk_df, h1_bull, brk_m5, 70, now=bj(9))
    assert asia.details.get("setup") != "breakout", asia
    chase_rows = brk_rows[:-3] + [(112.2, 113.4, 112.1, 113.2), (113.2, 114.1, 113.1, 114.0), (114.0, 114.9, 113.9, 114.8)]
    chase_score, _, _ = score_breakout("buy", ohlc_frame(chase_rows, brk_vols), calculate_atr(brk_df), "bull", "bull")
    assert chase_score <= 45, chase_score
    edge_rows = []
    for i in range(40):
        mid = 102 + 2 * math.sin(i / 2.5)
        edge_rows.append((mid - 0.2, mid + 0.5, mid - 0.5, mid + 0.2))
    edge_rows.append((100.6, 100.9, 99.6, 100.8))
    edge_vols = [100] * len(edge_rows)
    edge_vols[-1] = 150
    for i in range(len(edge_rows) - 5, len(edge_rows) - 1):
        edge_vols[i] = 70
    edge_m5 = ohlc_frame([(100.9, 101.0, 100.6, 100.7), (100.7, 100.9, 100.5, 100.8), (100.8, 101.0, 100.6, 100.85)], [100] * 3)
    edge = build_fib_volume_pa_decision(ohlc_frame(edge_rows, edge_vols), h1_bull, edge_m5, 70, now=bj(9))
    assert edge.action == "buy" and edge.details.get("setup") == "range_edge", edge
    edge_bear = build_fib_volume_pa_decision(ohlc_frame(edge_rows, edge_vols), h1_bear, edge_m5, 70, now=bj(9))
    assert edge_bear.action == "no", edge_bear
    assert calculate_rsi(h1_bull) > 70 and calculate_rsi(h1_bear) < 30

    print("SELF-TEST OK")
    print("BUY", result_buy.action, result_buy.confidence, result_buy.stop_loss, result_buy.take_profit)
    print("SELL", result_sell.action, result_sell.confidence, result_sell.stop_loss, result_sell.take_profit)
    print("WIDE", wide_result.action, wide_sl, wide_tp)
    print("MISALIGNED", misaligned.action)
    return 0

def main():
    parser = argparse.ArgumentParser(description="XAUUSD Fib + Volume + Price Action MT5 trader.")
    parser.add_argument("--once", action="store_true", help="只在连接 MT5 后评估一次")
    parser.add_argument("--self-test", action="store_true", help="运行内置策略样例测试，不连接 MT5")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    trader = FibVolumePATrader()
    return trader.run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
