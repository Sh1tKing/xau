import copy
import argparse
import atexit
import base64
import hashlib
import hmac
import json
import logging
import math
from numbers import Real
import os
import sys
import time
from types import SimpleNamespace
from crypto_demo.strategies.framework import Context, DEFAULT_REGISTRY, StrategyError, VERSION as STRATEGY_FRAMEWORK_VERSION
from crypto_demo.strategies.lifecycle import EventLedger, execution_token, recovery_event
from crypto_demo.strategies.events import validate_entry_event
from crypto_demo.strategies.adaptive import validate_analysis_checkpoint
from crypto_demo.strategies.regions import region_facts
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Dict, List, Optional, Set, Tuple
from urllib import error, request

import MetaTrader5 as mt5
import pandas as pd
from crypto_demo import risk
from crypto_demo.instance_lock import AccountLock, account_id
from crypto_demo.pending import PendingOrder, shared_account_state


STRATEGIES = DEFAULT_REGISTRY
ENTRY_CLOCK_VERSION = "closed-entry-clock-v1"
ADAPTIVE_SESSION_VERSION = "adaptive-session-v1"


def strategy_api():
    """Math helpers only; plugins receive no trader or MT5 instance."""
    names = ('fib_score_for_action', 'classify_volume_state', 'classify_price_action',
             'trend_score_for_action', 'setup_details', 'FibLevels')
    return SimpleNamespace(**{name: globals()[name] for name in names})


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
ASSET = os.environ.get("CRYPTO_ASSET", "BTC")
MODE = os.environ.get("CRYPTO_MODE", "observe")
LOG_DIR = os.path.join(BASE_DIR, "crypto_logs", account_id(os.environ.get("MT5_LOGIN", "offline"), os.environ.get("MT5_SERVER", "offline")), ASSET)


def strategy_reference(df_m15, df_m5, contract):
    use_m5 = (contract or {}).get("entry_reference") == "closed_m5"
    frame = df_m5 if use_m5 else df_m15
    if frame is None or frame.empty:
        raise StrategyError("策略参考K线缺失")
    return (float(frame["close"].iloc[-1]),
            "confirmed_closed_m5_close" if use_m5 else "previous_closed_m15_close",
            int(frame["time"].iloc[-1]) if "time" in frame else 0)


def opportunity_eligible(opportunity):
    """Structural permission is independent of the locally generated SL/TP."""
    return not opportunity.get("structural_blockers")


def verified_region_evidence(action, df_m15, df_m5, now, offset, evidence, parameters, kind):
    """Reconstruct complete source facts before freezing any region risk limits."""
    actual = region_facts(kind, df_m15, df_m5, now, offset, action, parameters)
    if actual.get("blockers") or not actual.get("closed_trigger", {}).get("confirmed"):
        raise StrategyError("区域源数据没有可验证首次事件")
    for key in ("region", "entry_event", "event_id", "risk_anchor", "target_obstacles",
                "active_window", "protection_bars", "trigger_bar", "breakout_bar", "closed_trigger"):
        if evidence.get(key) != actual.get(key):
            raise StrategyError("区域证据与原始行情不一致: " + key)
    frozen = copy.deepcopy(evidence)
    frozen["host_verified_region"] = {
        "action": action, "risk_anchor": actual["risk_anchor"],
        "target_obstacles": actual["target_obstacles"], "region": actual["region"],
        "active_window": actual["active_window"], "entry_event": actual["entry_event"],
        "protection_bars": actual["protection_bars"], "parameters": copy.deepcopy(parameters)}
    return frozen


def _valid_region_math(action, *values):
    """Accept real numeric inputs, never coercible text/bools or nonfinite prices."""
    try:
        return (action in ("buy", "sell") and all(
            isinstance(value, Real) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0 for value in values))
    except (TypeError, ValueError, OverflowError):
        return False


def required_region_stop(action, entry, atr, df_m5, evidence, parameters=None):
    if not _valid_region_math(action, entry, atr):
        raise StrategyError("区域止损方向或价格指标无效")
    facts = evidence.get("host_verified_region", {})
    if facts.get("action") != action or parameters is not None and facts.get("parameters") != parameters:
        raise StrategyError("区域止损缺少已验证来源")
    anchor = float(facts["risk_anchor"]["price"])
    bars = facts["protection_bars"]
    if df_m5 is not None and df_m5[["time", "open", "high", "low", "close"]].tail(3).to_dict("records") != bars:
        raise StrategyError("区域保护K线与已验证来源不一致")
    extreme = min(b["low"] for b in bars) if action == "buy" else max(b["high"] for b in bars)
    sign = 1 if action == "buy" else -1
    floor, cap = entry_risk_limits(atr, entry)
    raw = min(anchor, extreme)-stop_buffer(atr) if action == "buy" else max(anchor, extreme)+stop_buffer(atr)
    raw = min(raw, entry-floor) if action == "buy" else max(raw, entry+floor)
    sl = round_protective_stop(action, raw)
    risk = sign*(entry-sl)
    if not math.isfinite(risk) or not 0 < risk <= cap:
        raise StrategyError("区域及M5保护止损超过币种动态价格距离上限或无效")
    return sl


def region_target_limit(action, entry, atr, evidence):
    if not _valid_region_math(action, entry, atr):
        raise StrategyError("区域目标方向或价格指标无效")
    facts = evidence.get("host_verified_region", {})
    if facts.get("action") != action:
        raise StrategyError("区域目标缺少已验证来源")
    sign = 1 if action == "buy" else -1
    obstacles = facts["target_obstacles"]
    if not obstacles:
        return None
    first = min(obstacles, key=lambda r: sign*(r["obstacle_price"]-entry))
    return first["obstacle_price"]-sign*stop_buffer(atr)


def region_plan_error(action, entry, sl, tp, atr, evidence):
    if not _valid_region_math(action, entry, sl, tp, atr):
        return "区域计划方向或价格指标无效"
    try:
        needed = required_region_stop(action, entry, atr, None, evidence)
        sign = 1 if action == "buy" else -1
        if sign*(sl-needed) > 0 or not 0 < sign*(entry-sl) <= price_cap(atr, entry):
            return "区域止损未覆盖来源或超过上限"
        limit = region_target_limit(action, entry, atr, evidence)
        if limit is not None and sign*(tp-limit) > 0:
            return "区域目标越过最近反向区域"
        if sign*(tp-entry) > evidence["host_verified_region"].get("target_max_reference_r", evidence["host_verified_region"]["parameters"]["target_r"])*sign*(entry-sl):
            return "区域目标超过契约R上限"
        if reward_risk_ratio(action, entry, sl, tp) < MIN_REWARD_RISK:
            return "区域目标空间不足"
    except (StrategyError, KeyError, TypeError, ValueError):
        return "区域保护来源缺失或无效"
    return ""


def configured_server_offset():
    try:
        offset = float(os.environ.get("CRYPTO_SERVER_UTC_OFFSET_HOURS", "0"))
        if not math.isfinite(offset) or not -12 <= offset <= 14:
            raise ValueError()
        return offset
    except (ValueError, TypeError, OverflowError) as exc:
        raise risk.RiskError("invalid configured server offset") from exc


def crypto_quote_prices(tick, observed_at=None):
    return risk.quote_prices(tick, observed_at, configured_server_offset())


def tick_clock_offset(tick, observed_at=None):
    try:
        crypto_quote_prices(tick, observed_at)
        return configured_server_offset()
    except risk.RiskError:
        return None


def closed_entry_readiness(m15_marker, m5_marker, tick, observed_at):
    """Check raw current-bar boundaries against local UTC without advancing its clock."""
    facts = {"ready": False, "reason": "", "observed_utc": observed_at,
             "server_utc_offset": None, "raw_m15": m15_marker, "raw_m5": m5_marker,
             "m15_boundary_utc": None, "m5_boundary_utc": None}
    try:
        observed = float(observed_at)
        if isinstance(observed_at, bool) or not math.isfinite(observed) or observed <= 0:
            raise ValueError("invalid observed local clock")
    except (TypeError, ValueError, OverflowError):
        facts["reason"] = "invalid_observed_clock"
        return facts
    offset = tick_clock_offset(tick, observed)
    facts["server_utc_offset"] = offset
    if offset is None:
        facts["reason"] = "unknown_or_stale_tick_clock"
        return facts
    try:
        for name, marker in (("m15", m15_marker), ("m5", m5_marker)):
            value = float(marker)
            if isinstance(marker, bool) or not math.isfinite(value) or value <= 0 or value != int(value):
                raise ValueError("invalid raw current-bar marker")
            facts[name + "_boundary_utc"] = value - offset * 3600
    except (TypeError, ValueError, OverflowError):
        facts["reason"] = "invalid_current_bar_marker"
        return facts
    if max(facts["m15_boundary_utc"], facts["m5_boundary_utc"]) > observed:
        facts["reason"] = "local_clock_before_closed_entry_boundary"
        return facts
    facts["ready"] = True
    return facts


def research_execution_error(result, tick):
    """Shared executable-price guard for the demo research policy and replay."""
    details = result.details or {}
    if details.get("source") == "manual" and details.get("strategy_contract", {}).get("manual_entry"):
        try:
            risk.execution_prices(ASSET, result.action, None, None, result.stop_loss, result.take_profit,
                                  tick, contract_spec(), configured_server_offset(), manual=True)
            return ""
        except (risk.RiskError, AttributeError, KeyError, TypeError, ValueError) as exc:
            return "用户订单执行检查失败: " + str(exc)
    if not details.get("strategy_contract", {}).get("demo_only"):
        return ""
    try:
        bid, ask = float(tick.bid), float(tick.ask)
        atr, ema = float(details["entry_guard_atr"]), float(details["entry_guard_ema21"])
        sl, tp = float(result.stop_loss), float(result.take_profit)
        if not all(math.isfinite(x) and x > 0 for x in (bid, ask, atr, ema, sl, tp)) or ask < bid:
            return "研究策略执行行情或保护指标无效"
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return "研究策略执行行情或保护指标缺失"
    action = result.action
    if action not in ("buy", "sell"):
        return "研究策略方向无效"
    entry = ask if action == "buy" else bid
    trigger_price = bid if action == "buy" else ask
    risk_distance = directional_stop_distance(action, entry, sl)
    clearance = directional_stop_distance(action, trigger_price, sl)
    cap_tolerance = 0 if details.get("strategy_contract", {}).get("stop_reference") == "region" else .001
    if risk_distance <= 0 or risk_distance > price_cap(atr, entry) + cap_tolerance:
        return "研究策略实时止损距离超出上限"
    if clearance < 2 * stop_buffer(atr):
        return "研究策略实时止损触发侧余量不足"
    if reward_risk_ratio(action, entry, sl, tp) < MIN_REWARD_RISK:
        return "研究策略实时盈亏比不足"
    if abs(entry - ema) > CHASE_MAX_ATR * atr:
        return "研究策略实时EMA21偏离过大，不追价"
    if details.get("strategy_contract", {}).get("stop_reference") == "region":
        evidence = details.get("strategy_evidence", details.get("strategy_assessments", {})).get(action, {}).get("evidence", {})
        return region_plan_error(action, float(details.get("reference_price", details["entry_price"])), sl, tp, atr, evidence)
    return ""


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


class DailyDurableFileHandler(DurableFileHandler):
    """Keep each record in its local calendar day's file without a restart."""

    def __init__(self, log_dir, prefix, encoding="utf-8"):
        self.log_dir = os.path.abspath(log_dir)
        self.prefix = prefix
        self.log_date = datetime.now().strftime("%Y-%m-%d")
        os.makedirs(self.log_dir, exist_ok=True)
        super().__init__(self.filename_for_date(self.log_date), encoding=encoding)

    def filename_for_date(self, log_date):
        return os.path.join(self.log_dir, "%s%s.log" % (self.prefix, log_date))

    def emit(self, record):
        self.acquire()
        try:
            record_date = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d")
            if record_date != self.log_date:
                if self.stream is not None:
                    self.flush()
                    self.stream.close()
                    self.stream = None
                self.baseFilename = self.filename_for_date(record_date)
                self.log_date = record_date
            super().emit(record)
        except Exception:
            self.handleError(record)
        finally:
            self.release()


def setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("myq_crypto_demo")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)

        file_handler = DailyDurableFileHandler(LOG_DIR, "Fib_PA_", encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)

        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    return logger


if os.environ.get("CRYPTO_RUN_AUTHORIZED") != "1":
    raise RuntimeError("Use crypto_demo/run.py; direct import/start is disabled")
logger = setup_logger()
atexit.register(logging.shutdown)


MT5_LOGIN = int(os.environ.get("MT5_LOGIN") or "0")
MT5_PASSWORD = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER = os.environ.get("MT5_SERVER", "")

SYMBOL = os.getenv("FIB_PA_SYMBOL", "")
LOT_SIZE = None  # Volume always comes from broker cash/margin sizing.
MAX_POSITIONS = 1
TIMEFRAME_NAME = os.getenv("FIB_PA_TIMEFRAME", "M5").strip().upper()
CONFIDENCE_THRESHOLD = env_int("FIB_PA_CONFIDENCE_THRESHOLD", 80)
MAGIC = env_int("FIB_PA_MAGIC", 240610)
ORDER_TAG = os.getenv("FIB_PA_ORDER_TAG", "FIB_PA")
TITLE_TAG = os.getenv("FIB_PA_TITLE_TAG", "三策略")
ENABLE_TRADING = MODE == "demo"
POLL_SECONDS = max(0.2, env_float("FIB_PA_POLL_SECONDS", 0.5))
DEVIATION = env_int("FIB_PA_DEVIATION", 30)
GUARDIAN_ENABLED = env_bool("FIB_PA_GUARDIAN_ENABLED", True)
BREAKEVEN_TRIGGER_R = env_float("FIB_PA_BREAKEVEN_TRIGGER_R", 0.8)
BREAKEVEN_TRIGGER_ATR = env_float("FIB_PA_BREAKEVEN_TRIGGER_ATR", 1.0)
BREAKEVEN_BUFFER_ATR = env_float("FIB_PA_BREAKEVEN_BUFFER_ATR", 0.1)
VIRTUAL_CONFIRM_SECONDS = env_float("FIB_PA_VIRTUAL_CONFIRM_SECONDS", 2.0)
STRUCTURE_EXIT_VOLUME_RATIO = env_float("FIB_PA_STRUCTURE_EXIT_VOLUME_RATIO", 1.2)
REAL_SL_DISASTER_ATR = env_float("FIB_PA_REAL_SL_DISASTER_ATR", 0.35)
REAL_SL_DISASTER_MIN = 0.0
MIN_SL_MODIFY_ATR = env_float("FIB_PA_MIN_SL_MODIFY_ATR", 0.1)
MIN_SL_MODIFY_ABS = 0.0
MIN_REWARD_RISK = 1.5

STOP_PRICE_VERSION = "protective-stop-v1"
H4_HARD_FILTER = False
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
FEISHU_NOTIFY_ENABLED = env_bool("FEISHU_NOTIFY_ENABLED", False)

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
            try:
                result = json.loads(body)
            except (TypeError, ValueError):
                logger.warning("⚠️ 飞书通知返回无效JSON")
                return False
            code = result.get("code", result.get("StatusCode")) if isinstance(result, dict) else None
            if type(code) is int and code == 0:
                logger.info("✅ 飞书通知发送成功")
                return True
            logger.warning("⚠️ 飞书通知被拒绝: code=%s", code if type(code) is int else "未知")
            return False
        logger.warning("⚠️ 飞书通知发送失败: HTTP %s", response.status)
    except error.HTTPError as exc:
        logger.warning("⚠️ 飞书通知发送失败: HTTP %s", exc.code)
    except Exception as exc:
        logger.warning("⚠️ 飞书通知异常: %s", type(exc).__name__)
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

def classify_price_action(df: pd.DataFrame, action: str, atr: float, evidence=None) -> Tuple[int, str]:
    if evidence is not None:
        evidence.clear()
        evidence.update({"available": False, "action": action})
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

    if evidence is not None and action in ("buy", "sell"):
        evidence.update({
            "available": True,
            "atr": float(atr),
            "min_body": float(min_body),
            "prior_extreme": float(prior_low if action == "buy" else prior_high),
            "prior_extreme_field": "low" if action == "buy" else "high",
            "checks": {
                "engulfing": bool(engulfing),
                "wick_rejection": bool(lower_rejection if action == "buy" else upper_rejection),
                "false_break": bool(false_break),
                "previous_bar_break_confirm": bool(retest_confirm),
                "liquidity_sweep": bool(swept),
            },
        })
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
    buffer = stop_buffer(atr)
    if action == "sell":
        if float(last["close"]) >= float(last["open"]):
            return None
        extreme = float(window["high"].max())
        risk = extreme + buffer - entry
        if risk <= 0 or risk > price_cap(atr, entry):
            return None
        return entry, extreme
    if float(last["close"]) <= float(last["open"]):
        return None
    extreme = float(window["low"].min())
    risk = entry - (extreme - buffer)
    if risk <= 0 or risk > price_cap(atr, entry):
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


def contract_spec():
    return mt5.symbol_info(SYMBOL)


def tick_size():
    return risk.spec_values(contract_spec())["trade_tick_size"]


def price_cap(atr, reference):
    return risk.price_cap(ASSET, atr, reference)


def entry_risk_limits(atr, reference):
    return .6 * risk.number(atr), price_cap(atr, reference)


def stop_buffer(atr):
    return risk.buffer(atr, tick_size())


def round_protective_stop(action, price):
    return risk.quantize(price, tick_size(), upward=action == "sell")


def round_target(action, price):
    return risk.quantize(price, tick_size(), upward=action == "sell")


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
    return "最近3根M5极值加缓冲超过币种动态价格距离上限，拒绝把止损拉进影线"


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


def reject_inside_m5_wick(action, entry, stop_loss, take_profit, atr, fib, df_m5, target_r=1.8):
    extreme = recent_m5_extreme(df_m5, action)
    if not m5_extreme_on_stop_side(action, entry, extreme):
        return stop_loss, take_profit, ""
    required = m5_extreme_risk(action, entry, extreme, atr)
    if required <= 0:
        return 0.0, 0.0, "M5保护止损方向或距离无效，拒绝开仓"
    if required > price_cap(atr, entry) + 0.001:
        return 0.0, 0.0, m5_wick_reject_text()
    if stop_covers_m5_extreme(action, stop_loss, extreme, atr):
        return stop_loss, take_profit, ""
    new_sl, new_tp = calculate_targets(action, entry, atr, fib, extreme, extreme, target_r=target_r)
    if new_sl <= 0:
        boundary = extreme + stop_buffer(atr) if action == "sell" else extreme - stop_buffer(atr)
        safe_sl = round_protective_stop(action, boundary)
        safe_risk = directional_stop_distance(action, entry, safe_sl)
        if safe_risk > price_cap(atr, entry) + 0.001:
            return 0.0, 0.0, "M5保护止损按价格精度量化后距离 %.4f 超过上限 %.2f，拒绝开仓" % (safe_risk, price_cap(atr, entry))
        return 0.0, 0.0, "M5保护止损无法按价格精度生成，拒绝开仓"
    if not stop_covers_m5_extreme(action, new_sl, extreme, atr):
        return 0.0, 0.0, "M5保护止损未覆盖最近3根极值及缓冲，拒绝开仓"
    return new_sl, new_tp, "止损改到最近3根M5极值之外"

def calculate_targets(action, entry_price, atr, fib, setup_price=None, protect_extreme=None, target_r=1.8):
    if action not in ("buy", "sell") or not math.isfinite(target_r) or not 1.5 <= target_r <= 6:
        return 0., 0.
    try:
        floor, cap = entry_risk_limits(atr, entry_price)
        buf = stop_buffer(atr)
        candidates = [v for v in (setup_price, protect_extreme) if v is not None]
        if fib is not None:
            candidates.append(fib.swing_low if action == "buy" else fib.swing_high)
        if not candidates:
            return 0., 0.
        anchor = min(candidates) if action == "buy" else max(candidates)
        raw = min(anchor-buf, entry_price-floor) if action == "buy" else max(anchor+buf, entry_price+floor)
        sl = round_protective_stop(action, raw)
        distance = directional_stop_distance(action, entry_price, sl)
        if not 0 < distance <= cap:
            return 0., 0.
        tp = round_target(action, entry_price+(1 if action == "buy" else -1)*target_r*distance)
        return (sl, tp) if reward_risk_ratio(action, entry_price, sl, tp) >= 1.5 else (0., 0.)
    except risk.RiskError:
        return 0., 0.

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
        hard_break = latest_close < min(structure_line, state.entry_price - max(0.3 * atr, 2*tick_size()))
        if hard_break and vol_ratio >= STRUCTURE_EXIT_VOLUME_RATIO and pa_score >= 12:
            return True, f"BUY结构失效: close={latest_close:.2f}, line={structure_line:.2f}, vol={vol_ratio:.2f}x, PA={pa_text}"
    else:
        structure_line = fib.levels["78.6"] if fib and fib.direction == "bear" else state.initial_sl
        hard_break = latest_close > max(structure_line, state.entry_price + max(0.3 * atr, 2*tick_size()))
        if hard_break and vol_ratio >= STRUCTURE_EXIT_VOLUME_RATIO and pa_score >= 12:
            return True, f"SELL结构失效: close={latest_close:.2f}, line={structure_line:.2f}, vol={vol_ratio:.2f}x, PA={pa_text}"

    return False, "结构未失效"


def score_candidate(action, df_m15, fib, atr, h1_trend, m15_trend, adx):
    result = STRATEGIES.snapshot().get("pullback").assess(Context(action, df_m15, None, None, fib, atr, h1_trend, m15_trend, adx, "", strategy_api()))
    return result.score, result.details



def momentum_score(action, setup, rsi, hist, prev_hist):
    strategy = STRATEGIES.snapshot().get(setup)
    return strategy.momentum(action, rsi, hist, prev_hist) if strategy.momentum else (0, "")


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


def score_range_edge(action, df_m15, atr, h1_trend, m15_trend):
    result = STRATEGIES.snapshot().get("range_edge").assess(Context(action, df_m15, None, None, None, atr, h1_trend, m15_trend, 0, "", strategy_api()))
    return result.score, result.details, result.structure


def breakout_retest_facts(recent, broke, edge, atr, action, retest_atr=.35):
    # Generic geometry helper; the strategy supplies its own tolerance.
    """Shared closed-bar retest facts for local scoring and AI evidence."""
    sign = 1 if action == "buy" else -1
    after = recent.loc[broke.index[0] + 1:] if not broke.empty else recent.iloc[:0]
    field = "low" if action == "buy" else "high"
    touches = after[sign * (after[field] - edge) <= retest_atr * atr]
    probe = None if after.empty else float(after[field].min() if action == "buy" else after[field].max())
    holds = bool(not broke.empty and sign * (float(recent.iloc[-1]["close"]) - edge) > 0)
    return {"after": after, "touches": touches, "probe": probe,
            "retested": bool(not touches.empty), "close_holds_edge": holds,
            "retest_holds": bool(not touches.empty and holds)}


def score_breakout(action, df_m15, atr, h1_trend, m15_trend):
    result = STRATEGIES.snapshot().get("breakout").assess(Context(action, df_m15, None, None, None, atr, h1_trend, m15_trend, 0, "", strategy_api()))
    return result.score, result.details, result.structure


def build_fib_volume_pa_decision(
    df_m15: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_m5: pd.DataFrame,
    threshold=80,
    now: Optional[datetime] = None,
    strategy_snapshot=None,
    server_utc_offset=None,
    entry_gate=None,
) -> StrategyResult:
    snapshot = strategy_snapshot if strategy_snapshot is not None else STRATEGIES.snapshot()
    session = trading_session(now)
    decision_clock = now or datetime.now(BEIJING_TZ)
    if decision_clock.tzinfo is None:
        decision_clock = decision_clock.replace(tzinfo=BEIJING_TZ)
    active = snapshot.active(session)
    if snapshot.error or not active:
        reason = snapshot.error or f"时段{session}没有启用策略"
        return StrategyResult("no", 0, False, reason, 0., 0., 0, 0,
                              {"strategy_error": reason, "strategy_fingerprint": snapshot.fingerprint})
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

    rsi = calculate_rsi(df_m15)
    hist, prev_hist = macd_histogram(df_m15)
    picks = {}
    assessments = {}
    candidates = []
    try:
        for side in ("buy", "sell"):
            best = None
            for strategy in active:
                context = Context(side, df_m15, df_h1, df_m5, fib, atr, h1_trend, m15_trend, adx, session, strategy_api(),
                                  decision_clock, server_utc_offset)
                result = strategy.assess(context)
                blockers = list(result.blockers)
                evidence = copy.deepcopy(result.evidence)
                evidence.pop("host_verified_region", None)
                evidence.pop("host_verified_entry_event", None)
                evidence.pop("host_verified_analysis_checkpoint", None)
                if strategy.analysis_mode:
                    error = validate_analysis_checkpoint(evidence, decision_clock, server_utc_offset,
                                                         df_m5, df_m15, strategy.id, side)
                    if error:
                        blockers.append(error)
                    else:
                        evidence["host_verified_analysis_checkpoint"] = {
                            "analysis_checkpoint": copy.deepcopy(evidence["analysis_checkpoint"]),
                            "profile": strategy.id, "action": side, "event_id": evidence["event_id"],
                            "server_offset_hours": server_utc_offset,
                            "h1_bar_time": int(df_h1["time"].iloc[-1]), "h1_trend": h1_trend}

                if evidence.get("closed_trigger", {}).get("confirmed") and strategy.one_entry_per_event:
                    error = validate_entry_event(evidence, decision_clock, server_utc_offset, df_m5, df_m15,
                                                 legacy_session=strategy.id == "session_retest")
                    event = evidence.get("entry_event")
                    if event and event.get("action") != side:
                        error = "策略候选方向与首次事件不一致"
                    if error:
                        blockers.append(error)
                    else:
                        evidence["host_verified_entry_event"] = {
                            "entry_event": copy.deepcopy(evidence.get("entry_event")),
                            "event_id": evidence.get("event_id"), "action": side,
                            "server_offset_hours": server_utc_offset}
                    if strategy.stop_reference == "region":
                        try:
                            kind = strategy.parameters.get("region_event_kind")
                            if kind not in ("retest", "rejection"):
                                raise StrategyError("未知区域事实类型")
                            evidence = verified_region_evidence(side, df_m15, df_m5, decision_clock,
                                      server_utc_offset, evidence, strategy.parameters, kind)
                            evidence["host_verified_region"]["target_max_reference_r"] = strategy.target_r
                        except StrategyError as exc:
                            blockers.append(str(exc))
                if strategy.one_entry_per_event and not strategy.analysis_mode and not evidence.get("closed_trigger", {}).get("confirmed"):
                    blockers.append("策略没有确认首次收盘事件")
                event_id = evidence.get("event_id")
                if strategy.one_entry_per_event and (strategy.analysis_mode or result.evidence.get("closed_trigger", {}).get("confirmed")):
                    if not isinstance(event_id, str) or not event_id.strip():
                        blockers.append("策略事件标识缺失")
                    elif entry_gate is not None:
                        event_error = entry_gate(event_id, decision_clock.timestamp())
                        if event_error:
                            blockers.append(event_error)
                score, details, structure = result.score, dict(result.details), result.structure
                # Keep generic fields available for custom strategies, without fabricating facts.
                for key in ("fib", "volume", "price_action", "trend"):
                    details.setdefault(key, "")
                bonus, momentum_text = strategy.momentum(side, rsi, hist, prev_hist) if strategy.momentum else (0, "")
                if type(bonus) is not int or not isinstance(momentum_text, str):
                    raise StrategyError(f"{strategy.id} momentum返回类型错误")
                if score > 0:
                    if strategy.m5_boost:
                        score = boost_m5(side, score, details)
                    score = max(0, min(100, score + bonus))
                details.update(momentum=momentum_text, regime=regime, regime_text=regime_text,
                               location=str(regime_meta.get("location", .5)), session=session, setup=strategy.id,
                               strategy_contract=strategy.manifest(), strategy_fingerprint=snapshot.fingerprint,
                               strategy_blockers=blockers, strategy_event_id=event_id)
                assessments.setdefault(strategy.id, {})[side] = {
                    "score": score, "evidence": evidence, "blockers": blockers}
                candidates.append((side, score, details, structure))
                if best is None or score > best[0]:
                    best = (score, details, structure)
            picks[side] = best
    except Exception as exc:
        reason = f"策略评估失败: {type(exc).__name__}: {exc}"
        return StrategyResult("no", 0, False, reason, 0., 0., 0, 0,
                              {"strategy_error": reason, "strategy_fingerprint": snapshot.fingerprint})
    for _, _, details, _ in candidates:
        details["strategy_assessments"] = assessments[details["setup"]]
    buy_score, buy_details, buy_structure = picks["buy"]
    sell_score, sell_details, sell_structure = picks["sell"]

    def label(details: dict) -> str:
        return "[%s/%s] " % (
            SESSION_TEXT.get(details.get("session", session), session),
            details.get("strategy_contract", {}).get("name", details.get("setup", "")),
        )

    def finish(action: str, score: int, details: dict, fib: Optional[FibLevels]) -> StrategyResult:
        blocks = list(details.get("strategy_blockers", []))
        target_r = details["strategy_contract"]["target_max_reference_r"]
        if details["strategy_contract"].get("regime_filter") == "pullback":
            regime_block = regime_blocks_entry(
                action, regime, regime_text, regime_meta, int(details.get("pa_score", 0))
            )
            if regime_block:
                blocks.append(regime_block)
        if ALIGN_HARD_FILTER and not trends_support(action, h1_trend, m15_trend):
            blocks.append(f"{action.upper()} 与H1不同向(H1={h1_trend}, M15={m15_trend})")
        blocked, m5_text = m5_blocks_entry(df_m5, action)
        if blocked and not details["strategy_contract"].get("analysis_mode"):
            blocks.append(m5_text)
        details["m5_advisory"] = m5_text if blocked else ""
        structural = list(blocks)
        setup_price = float(df_m15["low"].tail(3).min() if action == "buy" else df_m15["high"].tail(3).max())
        # Legacy definitions retain their M15 reference; only an explicit research
        # contract selects the confirming M5. Do not globally revert the old experiment.
        entry_price, reference_source, reference_time = strategy_reference(df_m15, df_m5, details["strategy_contract"])
        details["reference_price_source"] = reference_source
        details["reference_bar_time"] = reference_time
        details["entry_guard_atr"] = atr
        details["entry_guard_ema21"] = float(ema_last(df_m15["close"], 21))
        extreme = recent_m5_extreme(df_m5, action)
        if details["strategy_contract"].get("stop_reference") == "m5_extreme":
            fib = None
            setup_price = extreme
            if not m5_extreme_on_stop_side(action, entry_price, extreme):
                blocks.append("研究策略缺少参考价外侧的最近三根M5保护极值")
        protect = extreme if m5_extreme_on_stop_side(action, entry_price, extreme) else None
        stop_loss, take_profit = 0.0, 0.0
        if details["strategy_contract"].get("stop_reference") == "region":
            evidence = details["strategy_assessments"][action]["evidence"]
            try:
                stop_loss = required_region_stop(action, entry_price, atr, df_m5, evidence,
                                                details["strategy_contract"]["parameters"])
                risk = abs(entry_price-stop_loss)
                sign = 1 if action == "buy" else -1
                take_profit = entry_price+sign*target_r*risk
                limit = region_target_limit(action, entry_price, atr, evidence)
                if limit is not None:
                    take_profit = min(take_profit, limit) if action == "buy" else max(take_profit, limit)
                # Round toward entry so obstacle and reference R limits are never exceeded.
                take_profit = round_protective_stop(action, take_profit)
                error = region_plan_error(action, entry_price, stop_loss, take_profit, atr, evidence)
                if error:
                    blocks.append(error)
            except StrategyError as exc:
                blocks.append(str(exc))
        elif protect is not None and m5_extreme_risk(action, entry_price, protect, atr) > price_cap(atr, entry_price):
            blocks.append(m5_wick_reject_text())
        else:
            stop_loss, take_profit = calculate_targets(action, entry_price, atr, fib, setup_price, protect, target_r=target_r)
            if stop_loss <= 0 and not blocked and details["strategy_contract"].get("stop_reference") != "m5_extreme":
                timed = m5_rejection_anchor(df_m5, action, atr)
                if timed is not None:
                    entry_price, setup_price = timed
                    details["reference_price_source"] = "confirmed_closed_m5_rejection_close"
                    details["reference_bar_time"] = int(df_m5["time"].iloc[-1]) if "time" in df_m5 else 0
                    extreme = recent_m5_extreme(df_m5, action)
                    protect = extreme if m5_extreme_on_stop_side(action, entry_price, extreme) else None
                    if protect is not None and m5_extreme_risk(action, entry_price, protect, atr) > price_cap(atr, entry_price):
                        blocks.append(m5_wick_reject_text())
                        stop_loss, take_profit = 0.0, 0.0
                    else:
                        stop_loss, take_profit = calculate_targets(action, entry_price, atr, fib, setup_price, protect, target_r=target_r)
                        details["entry_note"] = f"M5极值仍在{price_cap(atr, entry_price):g}美元内"
            if stop_loss > 0 and protect is not None and not stop_covers_m5_extreme(action, stop_loss, protect, atr):
                guarded_sl, guarded_tp, guard_note = reject_inside_m5_wick(
                    action, entry_price, stop_loss, take_profit, atr, fib, df_m5, target_r=target_r
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
        details["structural_blockers"] = structural
        details["reference_price"] = entry_price
        details["entry_price"] = float(entry_price)
        prices_ok = (
            action == "buy" and stop_loss > 0 and stop_loss < entry_price and take_profit > entry_price
        ) or (
            action == "sell" and stop_loss > entry_price and take_profit > 0 and take_profit < entry_price
        )
        if not prices_ok:
            if not any("影线" in item for item in blocks):
                blocks.append("结构止损超过币种动态价格距离上限，拒绝把止损拉进噪声")
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
            f"{action.upper()}策略确认，score={score}。{regime_text}。"
            f"{details['fib']}；{details['volume']}；"
            f"{details['price_action']}；{details['trend']}；{details.get('momentum', '')}。"
        )
        if blocks:
            reasoning += "拒绝: " + "；".join(blocks)
            return StrategyResult("no", score, False, reasoning, stop_loss, take_profit, buy_score, sell_score, details)
        reasoning += review_text
        return StrategyResult(action, score, False, reasoning, stop_loss, take_profit, buy_score, sell_score, details)

    finished = []
    opportunities = []
    for side, score, details, structure in candidates:
        plan = finish(side, score, details, structure)
        finished.append(plan)
        facts = details["strategy_assessments"][side]["evidence"]
        trigger = facts.get("event_id") or facts.get("closed_trigger", {}).get("bar_time") or details["reference_bar_time"]
        identity = f"{details['setup']}/{side}/{trigger}/{snapshot.fingerprint}"
        opportunity = {
            "opportunity_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
            "action": side, "local_score": score, "strategy_contract": copy.deepcopy(details["strategy_contract"]),
            "strategy_fingerprint": snapshot.fingerprint,
            "strategy_evidence": copy.deepcopy(details["strategy_assessments"]),
            "structural_blockers": copy.deepcopy(details["structural_blockers"]),
            "local_plan_status": {"valid": plan.action == side, "reason": plan.reasoning},
            "reference_price": {"price": details["reference_price"], "source": details["reference_price_source"],
                                "bar_time": details["reference_bar_time"]},
            "local_plan": {"action": plan.action, "stop_loss": plan.stop_loss, "take_profit": plan.take_profit,
                           "reasoning": plan.reasoning, "details": copy.deepcopy(plan.details)},
            "event_id": facts.get("event_id"), "entry_event": copy.deepcopy(facts.get("entry_event"))}
        opportunities.append(opportunity)
    executable = [p for p in finished if p.action != "no" and p.confidence >= threshold]
    executable.sort(key=lambda p: -p.confidence)  # Stable: session priority, then BUY before SELL.
    if executable:
        chosen = executable[0]
        opposing = next((p for p in executable if p.action != chosen.action), None)
        if opposing is not None and abs(chosen.confidence-opposing.confidence) <= 5:
            chosen = copy.deepcopy(chosen)
            chosen.action = "no"
            chosen.stop_loss = chosen.take_profit = 0.
            chosen.reasoning = f"多空评分接近，拒绝追单。BUY={buy_score}, SELL={sell_score}"
    else:
        indices = [i for i, o in enumerate(opportunities) if opportunity_eligible(o)]
        pool = indices or list(range(len(finished)))
        chosen = copy.deepcopy(finished[max(pool, key=lambda i: opportunities[i]["local_score"])])
        if chosen.action != "no":
            chosen.action = "no"
            if not chosen.details.get("strategy_contract", {}).get("analysis_mode"):
                chosen.stop_loss = chosen.take_profit = 0.
            chosen.reasoning = label(chosen.details) + f"未达到交易阈值，score={chosen.confidence}"
    if not executable:
        reasons = list(dict.fromkeys(block for o in opportunities for block in o["structural_blockers"]))
        if reasons:
            chosen.reasoning += "；候选硬条件: " + "；".join(reasons)
    if chosen.details.get("strategy_contract", {}).get("analysis_mode"):
        chosen.action = "no"
        chosen.reasoning = label(chosen.details) + "自主分析粗筛，仅供AI；本地分不作AI派发门槛。" + chosen.reasoning
    chosen.details["opportunities"] = copy.deepcopy(opportunities)
    return chosen

class FibVolumePATrader:
    def __init__(self):
        self.account_info = None
        self.pending_order = PendingOrder(shared_account_state(account_id(MT5_LOGIN, MT5_SERVER))/"pending_order.json", account_id(MT5_LOGIN, MT5_SERVER))
        self.guardian_states: Dict[int, GuardianState] = {}
        self.position_snapshots: Dict[int, PositionSnapshot] = {}
        self.notified_close_tickets: Set[int] = set()
        # Confidence at entry, keyed by position ticket, so close messages can show it.
        self.entry_confidence: Dict[int, str] = {}
        self.last_order_ticket = 0
        state_path = os.getenv("FIB_PA_STRATEGY_STATE_FILE", "").strip() or os.path.join(LOG_DIR, "strategy_event_state.json")
        self.strategy_ledger = EventLedger(state_path)
        self.positions_known = None
        self._entry_clock_defer_markers = None
        self._entry_clock_defer_reasons = set()
        self._guardian_data_wait = None
        self._guardian_data_wait_at = 0.0

    def crypto_account_ready(self, empty=False):
        account = mt5.account_info()
        risk.account_ready(mt5, account)
        if str(getattr(account, "login", "")) != str(MT5_LOGIN) or getattr(account, "server", None) != MT5_SERVER:
            raise risk.RiskError("account login/server changed")
        if empty:
            positions = self.query_account_positions()
            if self.pending_order.unresolved(positions):
                raise risk.RiskError("unresolved account-wide order receipt; reconcile exact token before BTC/ETH entry")
            if positions is None or len(positions):
                raise risk.RiskError("account positions unknown or nonempty")
        return account

    def query_account_positions(self, **filters):
        """A native None or identity change is unknown, never an empty account."""
        self.positions_known = False
        self.crypto_account_ready()
        positions = mt5.positions_get(**filters)
        # Recheck after the native query before accepting or reconciling its result.
        self.crypto_account_ready()
        if positions is None:
            raise risk.RiskError("account positions unknown")
        self.positions_known = True
        return positions

    def crypto_position_ready(self, position):
        self.crypto_account_ready()
        if getattr(position, "magic", None) != MAGIC or getattr(position, "symbol", None) != SYMBOL:
            raise risk.RiskError("position not owned by this crypto instance")
        live = self.query_account_positions(ticket=int(getattr(position, "ticket", 0)))
        if live is None or len(live) != 1:
            raise risk.RiskError("owned position missing/unknown")
        current = live[0]
        if (getattr(current, "symbol", None) != SYMBOL or getattr(current, "magic", None) != MAGIC
                or getattr(current, "type", None) != getattr(position, "type", None)):
            raise risk.RiskError("owned ticket identity changed")
        return current

    def crypto_sizing(self, result, tick):
        account = self.crypto_account_ready(empty=True)
        details = result.details or {}
        facts = risk.size_order(mt5, account, contract_spec(), ASSET, SYMBOL, result.action,
            details["entry_guard_atr"], details["entry_guard_ema21"], result.stop_loss, result.take_profit, tick,
            commission_per_lot=env_float("CRYPTO_COMMISSION_PER_LOT", 0),
            slippage_ticks=env_float("CRYPTO_SLIPPAGE_TICKS", 0), commission_bps=env_float("CRYPTO_COMMISSION_BPS", 0), server_offset_hours=configured_server_offset(),
            requested_volume=details.get("manual_volume") if details.get("source") == "manual" else None,
            manual=details.get("source") == "manual" and details.get("strategy_contract", {}).get("manual_entry", False))
        result.details["cash_risk"] = facts
        return facts

    def filling_policy(self):
        spec = contract_spec()
        mode, execution = getattr(spec, "filling_mode", None), getattr(spec, "trade_exemode", None)
        if not isinstance(mode, int) or execution is None:
            raise risk.RiskError("missing filling/execution specification")
        if mode & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
            return mt5.ORDER_FILLING_FOK
        if mode & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
            return mt5.ORDER_FILLING_IOC
        if execution != getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", 2):
            return mt5.ORDER_FILLING_RETURN
        raise risk.RiskError("unsupported broker filling policy")

    def entry_clock_ready(self, m15_marker, m5_marker):
        observed = time.time()
        tick = mt5.symbol_info_tick(SYMBOL)
        facts = closed_entry_readiness(m15_marker, m5_marker, tick, observed)
        markers = (m15_marker, m5_marker)
        if markers != self._entry_clock_defer_markers:
            self._entry_clock_defer_markers = markers
            self._entry_clock_defer_reasons.clear()
        if not facts["ready"] and facts["reason"] not in self._entry_clock_defer_reasons:
            self._entry_clock_defer_reasons.add(facts["reason"])
            logger.info("入场收盘等待=%s reason=%s raw_M15=%s raw_M5=%s boundary_utc_M15=%s "
                        "boundary_utc_M5=%s observed_utc=%s offset=%s", ENTRY_CLOCK_VERSION,
                        facts["reason"], m15_marker, m5_marker, facts["m15_boundary_utc"],
                        facts["m5_boundary_utc"], observed, facts["server_utc_offset"])
        return facts["ready"]

    def evaluate_context(self, df_m15, df_h1, df_m5):
        observed = time.time()
        tick = mt5.symbol_info_tick(SYMBOL)
        return build_fib_volume_pa_decision(
            df_m15, df_h1, df_m5, CONFIDENCE_THRESHOLD,
            now=datetime.fromtimestamp(observed, BEIJING_TZ), server_utc_offset=tick_clock_offset(tick, observed),
            entry_gate=self.strategy_ledger.entry_error)

    def validate_strategy_entry(self, result):
        details = result.details or {}
        contract = details.get("strategy_contract", {})
        if details.get("source") == "manual" or contract.get("manual_entry"):
            from crypto_demo.manual_orders import entry_error
            if (MODE != "demo" or not ENABLE_TRADING or details.get("source") != "manual"
                    or not contract.get("manual_entry") or contract.get("id") != "manual_"+ASSET.lower()):
                return "用户订单来源或模拟运行模式无效"
            return entry_error(sys.modules[__name__], self, result)
        if contract.get("demo_only"):
            account = mt5.account_info()
            if account is None or getattr(account, "trade_mode", None) != mt5.ACCOUNT_TRADE_MODE_DEMO:
                return "研究策略仅允许MT5模拟账户，账户类型未知或实盘时拒绝"
        if contract.get("one_entry_per_event"):
            assessments = details.get("strategy_evidence", details.get("strategy_assessments", {}))
            evidence = assessments.get(result.action, {}).get("evidence", {})
            event_id = details.get("strategy_event_id") or evidence.get("event_id")
            if contract.get("analysis_mode"):
                if details.get("source") != "ai1":
                    return "自主粗筛仅允许来源绑定的AI提案，关闭AI不得自动下单"
                binding = details.get("ai1_source_binding", {})
                original = evidence.get("host_verified_analysis_checkpoint", {})
                if (not original or original.get("analysis_checkpoint") != evidence.get("analysis_checkpoint")
                        or original.get("profile") != contract.get("id") or original.get("action") != result.action
                        or original.get("event_id") != event_id
                        or original.get("server_offset_hours") != evidence.get("server_offset_hours")
                        or original.get("h1_trend") != ("bull" if result.action == "buy" else "bear")
                        or binding.get("checkpoint") != original or not binding.get("snapshot_id")
                        or binding.get("fingerprint") != details.get("strategy_fingerprint")
                        or binding.get("reference_price") != details.get("reference_price_metadata")):
                    return "自主分析检查点缺少原始来源及AI请求绑定"
                observed = time.time()
                tick = mt5.symbol_info_tick(SYMBOL)
                offset = tick_clock_offset(tick, observed)
                error = validate_analysis_checkpoint(evidence, datetime.fromtimestamp(observed, timezone.utc),
                                                      offset, profile=contract.get("id"), action=result.action)
                if error:
                    return "自主分析检查点无效: " + error
                try:
                    m15, h1, m5 = self.get_context()
                    error = validate_analysis_checkpoint(evidence, datetime.fromtimestamp(observed, timezone.utc),
                                                         offset, m5, m15, contract.get("id"), result.action)
                    expected_h1 = int((observed + offset * 3600) // 3600) * 3600 - 3600
                    if (error or int(h1["time"].iloc[-1]) != original.get("h1_bar_time")
                            or int(h1["time"].iloc[-1]) != expected_h1
                            or trend_from_ema(h1, 50) != original.get("h1_trend")):
                        return "自主分析原始收盘来源或H1许可已改变: " + error
                except (RuntimeError, KeyError, TypeError, ValueError, IndexError) as exc:
                    return "自主分析来源复核失败: " + str(exc)
                if assessments.get(result.action, {}).get("blockers"):
                    return "策略硬条件已拒绝，不能提交订单"
                return self.strategy_ledger.entry_error(event_id, observed)
            if not evidence.get("closed_trigger", {}).get("confirmed") or not event_id:
                return "策略缺少真实首次收盘事件，拒绝下单"
            if event_id != evidence.get("event_id"):
                return "策略事件与收盘证据不一致，拒绝下单"
            error = validate_entry_event(evidence, time.time(), evidence.get("server_offset_hours"),
                                         legacy_session=contract.get("id") == "session_retest")
            if error:
                return "策略确认或窗口已过期: " + error if "expired" in error or "outside original" in error else error
            event = evidence.get("entry_event")
            if contract.get("id") != "session_retest":
                original = evidence.get("host_verified_entry_event", {})
                if (original.get("entry_event") != event or original.get("action") != result.action
                        or original.get("event_id") != event_id
                        or original.get("server_offset_hours") != evidence.get("server_offset_hours")):
                    return "策略首次事件缺少原始来源绑定"
            if event and (event.get("action") != result.action or event.get("event_id") != event_id):
                return "策略事件方向或标识不一致"
            if contract.get("stop_reference") == "region":
                verified = evidence.get("host_verified_region", {})
                if (verified.get("entry_event") != event or verified.get("action") != result.action
                        or time.time() >= min(verified.get("active_window", {}).get("end_utc", 0),
                                              verified.get("region", {}).get("expiry_utc", 0))):
                    return "区域首次事件来源或期限无效"
            if assessments.get(result.action, {}).get("blockers"):
                return "策略硬条件已拒绝，不能提交订单"
            return self.strategy_ledger.entry_error(event_id, time.time())
        return ""

    def record_strategy_close(self, ticket):
        saved = self.strategy_ledger.snapshot().get("positions", {})
        if str(int(ticket)) in saved:
            self.strategy_ledger.closed_positions({int(t) for t in saved if int(t) != int(ticket)}, time.time())

    def guard_strategy_lifecycle(self, positions):
        # Public callers cannot reconcile a stale list from another account/query.
        positions = self.get_positions()
        if positions:
            risk.spec_values(contract_spec())
            crypto_quote_prices(mt5.symbol_info_tick(SYMBOL))
        now = time.time()
        saved = self.strategy_ledger.snapshot()
        recovered_due = set()
        # Recover an acknowledged/uncertain research order after a crash between
        # order_send and ticket binding, using persisted pre-order clock/contract.
        for position in positions:
            ticket = int(position.ticket)
            if str(ticket) in saved.get("positions", {}):
                continue
            recovered = recovery_event(position, saved, SYMBOL, MAGIC)
            if recovered:
                event_id, opened, meta = recovered
                if self.strategy_ledger.record_position(ticket, event_id, opened, meta["max_hold_minutes"], meta["cooldown_seconds"]):
                    saved = self.strategy_ledger.snapshot()
                    if now >= opened + meta["max_hold_minutes"] * 60:
                        recovered_due.add(ticket)
        if self.positions_known is True:
            # Allow an opening acknowledgement a short propagation interval.
            active = {int(p.ticket) for p in positions}
            active.update(int(t) for t, p in saved.get("positions", {}).items() if now < p["opened_at_utc"] + 30)
            self.strategy_ledger.closed_positions(active, now)
        due = set(self.strategy_ledger.due_tickets(now)) | recovered_due
        remaining = []
        for position in positions:
            if (int(getattr(position, "magic", 0) or 0) == MAGIC and int(position.ticket) in due
                    and self.close_position_by_ticket(position, "策略最长持有期限到达，Python强制退出")):
                continue
            remaining.append(position)
        return remaining

    def entry_confidence_line(self, ticket: int) -> str:
        return f"开仓置信度: {self.entry_confidence.get(int(ticket), '未知(非本进程开仓或已重启)')}"

    def connect_mt5(self):
        try:
            self._crypto_account_lock = AccountLock(MT5_LOGIN, MT5_SERVER).acquire()
            atexit.register(self._crypto_account_lock.close)
        except RuntimeError as exc:
            logger.error("Crypto instance rejected: %s", exc)
            return False
        terminal = os.environ.get("CRYPTO_MT5_PATH", "").strip()
        initialized = mt5.initialize(path=terminal) if terminal else mt5.initialize()
        if not initialized:
            self._crypto_account_lock.close()
            logger.error("MT5 initialization failed for configured terminal")
            return False

        if not mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
            logger.error("❌ MT5账户登录失败")
            mt5.shutdown()
            self._crypto_account_lock.close()
            return False

        self.account_info = mt5.account_info()
        if self.account_info is None:
            logger.error("❌ 无法获取账户信息")
            mt5.shutdown()
            self._crypto_account_lock.close()
            return False

        try:
            self.crypto_account_ready()
            if not mt5.symbol_select(SYMBOL, True):
                raise risk.RiskError("exact broker symbol unavailable")
            risk.spec_values(contract_spec())
        except (risk.RiskError, RuntimeError) as exc:
            logger.error("Crypto connection rejected: %s", exc)
            mt5.shutdown()
            self._crypto_account_lock.close()
            return False
        logger.info("✅ MT5账户连接成功")
        logger.info(f"账户: {self.account_info.login}, 余额: {self.account_info.balance}, 净值: {self.account_info.equity}")
        if not mt5.symbol_select(SYMBOL, True):
            logger.error(f"❌ 无法选择交易品种 {SYMBOL}")
            mt5.shutdown()
            self._crypto_account_lock.close()
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
        df_m5 = self.get_rates(mt5.TIMEFRAME_M5, 80, shift=1)
        if len(df_m15) < 80 or len(df_h1) < 60 or len(df_m5) < 10:
            raise RuntimeError("K线数据不足，无法计算H1/M15策略")
        return df_m15, df_h1, df_m5

    def current_position_count(self):
        return len(self.query_account_positions())

    def get_positions(self):
        positions = self.query_account_positions(symbol=SYMBOL)
        return [p for p in positions if getattr(p, "magic", None) == MAGIC and getattr(p, "symbol", None) == SYMBOL]

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
        return positions

    def validate_order_prices(self, action, entry_price, stop_loss, take_profit):
        if action == "buy":
            return stop_loss > 0 and stop_loss < entry_price and take_profit > entry_price
        if action == "sell":
            return stop_loss > entry_price and take_profit > 0 and take_profit < entry_price
        return False

    def modify_position_sl(self, position, new_sl: float, reason: str) -> bool:
        if MODE != "demo":
            return False
        try:
            position = self.crypto_position_ready(position)
            action = "buy" if position.type == mt5.POSITION_TYPE_BUY else "sell"
            new_sl = round_protective_stop(action, new_sl)
            bid, ask = crypto_quote_prices(mt5.symbol_info_tick(SYMBOL))
            spec = risk.spec_values(contract_spec())
            minimum = max(spec["trade_tick_size"], max(spec["trade_stops_level"], spec["trade_freeze_level"]) * spec["point"])
            if (bid-new_sl if action == "buy" else new_sl-ask) < minimum:
                return False
        except (risk.RiskError, AttributeError, TypeError, ValueError):
            return False
        current_sl = float(getattr(position, "sl", 0.0) or 0.0)
        current_tp = float(getattr(position, "tp", 0.0) or 0.0)
        ticket = int(getattr(position, "ticket", 0))
        if ticket <= 0 or new_sl <= 0:
            return False
        direction = "BUY" if getattr(position, "type", None) == mt5.POSITION_TYPE_BUY else "SELL"
        if not sl_change_is_tighter(direction, current_sl, new_sl):
            logger.info(f"护盘拒绝放宽止损 ticket={ticket}: {current_sl} -> {new_sl} ({reason})")
            return False

        request_body = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": SYMBOL,
            "position": ticket,
            "sl": new_sl,
            "tp": current_tp,
        }
        if not ENABLE_TRADING:
            logger.info(f"🧪 DRY-RUN 修改SL ticket={ticket}: {current_sl} -> {new_sl} ({reason})")
            return True

        position = self.crypto_position_ready(position)
        if not sl_change_is_tighter(direction, float(position.sl or 0), new_sl):
            return False
        result = mt5.order_send(request_body)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"🛡️ 护盘SL更新 ticket={ticket}: {current_sl} -> {new_sl} ({reason})")
            return True
        retcode = result.retcode if result else "None"
        comment = result.comment if result else "无返回"
        logger.warning(f"⚠️ 护盘SL更新失败 ticket={ticket}, retcode={retcode}, comment={comment}")
        return False

    def close_position_by_ticket(self, position, reason: str) -> bool:
        if MODE != "demo":
            return False
        try:
            position = self.crypto_position_ready(position)
            risk.spec_values(contract_spec())
            crypto_quote_prices(mt5.symbol_info_tick(SYMBOL))
        except risk.RiskError:
            return False
        ticket = int(getattr(position, "ticket", 0))
        if ticket <= 0:
            return False

        snapshot = self.create_position_snapshot(position)

        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            logger.error(f"❌ 护盘平仓失败，无法获取 {SYMBOL} 报价")
            return False

        try:
            crypto_quote_prices(tick)
            risk.spec_values(contract_spec())
        except risk.RiskError:
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
            "type_filling": self.filling_policy(),
        }
        self.crypto_position_ready(position)
        result = mt5.order_send(request_body)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.warning(f"✅ 护盘单票平仓成功 ticket={ticket}, reason={reason}")
            self.record_strategy_close(ticket)
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
            risk=max(initial_risk, tick_size()),
            highest_price=highest,
            lowest_price=lowest,
        )
        self.guardian_states[ticket] = state
        logger.info(f"🛡️ 初始化护盘状态 ticket={ticket}, direction={direction}, risk={state.risk:.2f}")
        return state

    def guardian_data_unavailable(self, exc):
        signature = (type(exc).__name__, getattr(exc, "reason", str(exc)))
        now = time.time()
        if signature != self._guardian_data_wait or now-self._guardian_data_wait_at >= 30:
            logger.warning("护盘等待有效数据：%s；保留原护盘状态，下轮重新采集", exc)
            self._guardian_data_wait_at = now
        self._guardian_data_wait = signature

    def guardian_data_ready(self):
        if self._guardian_data_wait is not None:
            logger.info("护盘行情恢复，继续原持仓护盘")
            self._guardian_data_wait = None

    def run_guardian(self):
        # Validate the observation before lifecycle reconciliation or price-state writes.
        try:
            positions = self.guard_strategy_lifecycle(None)
        except risk.RiskError as exc:
            self.guardian_data_unavailable(exc)
            return
        # Frozen deadlines above remain independent of the optional ATR guardian.
        if not GUARDIAN_ENABLED:
            self.guardian_data_ready()
            return
        if positions:
            try:
                tick = mt5.symbol_info_tick(SYMBOL)
                crypto_quote_prices(tick)
                spec = risk.spec_values(contract_spec())
                df_m5 = self.get_rates(mt5.TIMEFRAME_M5, 120, shift=1)
                atr = risk.number(calculate_atr(df_m5) if len(df_m5) >= 20 else 0.0)
                self.crypto_account_ready()
                # Rates may take time: only the final fresh observation may update extrema/SL.
                tick = mt5.symbol_info_tick(SYMBOL)
                crypto_quote_prices(tick)
                spec = risk.spec_values(contract_spec())
            except risk.RiskError as exc:
                self.guardian_data_unavailable(exc)
                return
        self.guardian_data_ready()
        active_tickets = {int(pos.ticket) for pos in positions}
        for ticket in list(self.guardian_states.keys()):
            if ticket not in active_tickets:
                self.guardian_states.pop(ticket, None)
        if not positions:
            return

        now_ts = time.time()
        disaster_buffer = max(2*spec["trade_tick_size"], REAL_SL_DISASTER_ATR * atr)
        min_modify_step = max(spec["trade_tick_size"], MIN_SL_MODIFY_ATR * atr)

        for position in positions:
            state = self.get_or_create_guardian_state(position, tick)
            current_sl = float(position.sl or 0.0)

            if position.type == mt5.POSITION_TYPE_BUY:
                current_price = float(tick.bid)
                update_guardian_extremes(state, current_price)
                max_profit = max(0.0, state.highest_price - state.entry_price)
                max_profit_atr = max_profit / max(atr, tick_size())
                max_profit_r = max_profit / max(state.risk, tick_size())
                buffer_mult = calculate_sigmoid_buffer(max_profit_atr)
                virtual_sl = state.highest_price - buffer_mult * atr
                state.virtual_sl = max(state.virtual_sl, virtual_sl) if state.virtual_sl > 0 else virtual_sl

                if not state.breakeven_done and (
                    max_profit_r >= BREAKEVEN_TRIGGER_R or max_profit >= BREAKEVEN_TRIGGER_ATR * atr
                ):
                    be_sl = round_protective_stop("buy", state.entry_price + BREAKEVEN_BUFFER_ATR * atr)
                    if be_sl < current_price and (current_sl == 0.0 or be_sl > current_sl + min_modify_step):
                        if self.modify_position_sl(position, be_sl, "breakeven"):
                            state.breakeven_done = True

                target_real_sl = round_protective_stop("buy", state.virtual_sl - disaster_buffer)
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
                max_profit_atr = max_profit / max(atr, tick_size())
                max_profit_r = max_profit / max(state.risk, tick_size())
                buffer_mult = calculate_sigmoid_buffer(max_profit_atr)
                virtual_sl = state.lowest_price + buffer_mult * atr
                state.virtual_sl = min(state.virtual_sl, virtual_sl) if state.virtual_sl > 0 else virtual_sl

                if not state.breakeven_done and (
                    max_profit_r >= BREAKEVEN_TRIGGER_R or max_profit >= BREAKEVEN_TRIGGER_ATR * atr
                ):
                    be_sl = round_protective_stop("sell", state.entry_price - BREAKEVEN_BUFFER_ATR * atr)
                    if be_sl > current_price and (current_sl == 0.0 or be_sl < current_sl - min_modify_step):
                        if self.modify_position_sl(position, be_sl, "breakeven"):
                            state.breakeven_done = True

                target_real_sl = round_protective_stop("sell", state.virtual_sl + disaster_buffer)
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

    def validate_execution_prices(self, result: StrategyResult, tick) -> str:
        """Optional execution checks for subclasses using independently reviewed plans."""
        return research_execution_error(result, tick)

    def entry_submission_metadata(self, result, request_body, event_id, token):
        """Original account binding persisted with event consumption before send."""
        account = self.crypto_account_ready()
        return {"account_identity": {"login": str(account.login), "server": account.server}}

    def place_order(self, result: StrategyResult):
        if MODE == "observe":
            return False
        try:
            self.crypto_account_ready(empty=True)
        except risk.RiskError as exc:
            logger.info("Crypto account rejected: %s", exc)
            return False
        action = result.action
        if action not in ("buy", "sell"):
            return False
        strategy_error = self.validate_strategy_entry(result)
        if strategy_error:
            logger.info("策略执行拒绝: %s", strategy_error)
            return False
        contract = (result.details or {}).get("strategy_contract", {})
        manual = (result.details or {}).get("source") == "manual" and contract.get("manual_entry")
        if not manual and (not contract.get("analysis_mode") or contract.get("id") != ASSET.lower()+"_adaptive"):
            return False
        fingerprint = (result.details or {}).get("strategy_fingerprint")
        if fingerprint:
            try:
                STRATEGIES.require_current(fingerprint)
            except StrategyError as exc:
                logger.warning("拒绝旧策略下单: %s", exc)
                return False

        self.last_order_ticket = 0

        positions_count = self.current_position_count()
        if positions_count >= MAX_POSITIONS:
            logger.info(f"✋ 已有 {positions_count} 个 {SYMBOL} 持仓，达到上限 {MAX_POSITIONS}，不再开仓")
            return False
        if contract.get("one_entry_per_event"):
            self.get_positions()
            if self.positions_known is not True:
                logger.info("持仓查询失败，研究策略停止新下单")
                return False
            all_positions = self.query_account_positions()
            if len(all_positions) >= MAX_POSITIONS:
                logger.info("研究策略持仓状态不明或全品种持仓已达到上限")
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
        if contract.get("one_entry_per_event"):
            comment = f"{ORDER_TAG}_SR_{action.upper()}"

        if not self.validate_order_prices(action, price, result.stop_loss, result.take_profit):
            logger.error(
                f"❌ SL/TP方向非法，拒绝下单: action={action}, price={price}, "
                f"SL={result.stop_loss}, TP={result.take_profit}"
            )
            return False

        execution_error = self.validate_execution_prices(result, tick)
        if execution_error:
            logger.info(f"执行复核拒绝下单: {execution_error}, action={action}, price={price}")
            return False

        live_risk = directional_stop_distance(action, price, result.stop_loss)
        if not manual and (live_risk <= 0 or live_risk > price_cap(float(result.details["entry_guard_atr"]), price) + 0.001):
            logger.info(
                f"二审拒绝下单: 实时止损距离 {live_risk:.2f} > {price_cap(float(result.details["entry_guard_atr"]), price):.2f}, "
                f"action={action}, price={price}, SL={result.stop_loss}"
            )
            return False

        live_rr = reward_risk_ratio(action, price, result.stop_loss, result.take_profit)
        if not manual and live_rr < MIN_REWARD_RISK:
            logger.info(
                f"二审拒绝下单: 实时盈亏比 {live_rr:.2f} < {MIN_REWARD_RISK:.2f}, "
                f"price={price}, SL={result.stop_loss}, TP={result.take_profit}"
            )
            return False

        try:
            sizing = self.crypto_sizing(result, tick)
        except (risk.RiskError, KeyError) as exc:
            logger.info("Crypto cash risk rejected: %s", exc)
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
                    f"手数: {sizing['volume']}",
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
            "volume": sizing["volume"],
            "type": order_type,
            "price": price,
            "sl": result.stop_loss,
            "tp": result.take_profit,
            "deviation": DEVIATION,
            "magic": MAGIC,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.filling_policy(),
        }

        if fingerprint:
            try:
                STRATEGIES.require_current(fingerprint)
            except StrategyError as exc:
                logger.warning("提交前策略已改变: %s", exc)
                return False
        # Recheck the latest quote/spec without changing fixed audited SL/TP.
        try:
            tick = mt5.symbol_info_tick(SYMBOL)
            sizing = self.crypto_sizing(result, tick)
            request_body["price"], request_body["volume"] = sizing["price"], sizing["volume"]
            if self.validate_execution_prices(result, tick):
                return False
            risk.order_check(mt5, request_body)
        except (risk.RiskError, KeyError) as exc:
            logger.info("Crypto pre-submit rejected: %s", exc)
            return False
        event_id = None
        if contract.get("one_entry_per_event"):
            if self.validate_strategy_entry(result):
                return False
            assessments = result.details.get("strategy_evidence", result.details.get("strategy_assessments", {}))
            event_id = result.details.get("strategy_event_id") or assessments.get(action, {}).get("evidence", {}).get("event_id")
            observed = time.time()
            offset = tick_clock_offset(tick, observed)
            token = execution_token(event_id, SYMBOL, MAGIC, action)
            original_evidence = assessments.get(action, {}).get("evidence", {})
            try:
                entry_metadata = self.entry_submission_metadata(result, request_body, event_id, token)
            except (risk.RiskError, TypeError, ValueError, OSError) as exc:
                logger.info("原入场上下文无法冻结，停止提交订单: %s", exc)
                return False
            if offset is None or offset != original_evidence.get("server_offset_hours") or not self.strategy_ledger.reserve(event_id, observed, {
                    **entry_metadata,
                    "execution_token": token, "symbol": SYMBOL, "magic": MAGIC, "action": action,
                    "event_id": event_id, "evidence": copy.deepcopy(original_evidence),
                    "server_utc_offset": offset, "contract": contract,
                    "max_hold_minutes": contract.get("max_hold_minutes", 0),
                    "cooldown_seconds": contract.get("cooldown_seconds", 0)}):
                logger.info("策略事件无法持久保留或行情时差不明，停止提交订单")
                return False
            request_body["comment"] = "_SR_" + token if contract.get("id") == "session_retest" else token
        self.crypto_account_ready(empty=True)
        self.pending_order.reserve(request_body)
        order_result = mt5.order_send(request_body)
        if order_result is not None:
            definitive = {getattr(mt5, name, -999) for name in (
                "TRADE_RETCODE_REQUOTE", "TRADE_RETCODE_REJECT", "TRADE_RETCODE_INVALID", "TRADE_RETCODE_INVALID_VOLUME",
                "TRADE_RETCODE_INVALID_PRICE", "TRADE_RETCODE_INVALID_STOPS", "TRADE_RETCODE_TRADE_DISABLED",
                "TRADE_RETCODE_MARKET_CLOSED", "TRADE_RETCODE_NO_MONEY", "TRADE_RETCODE_INVALID_FILL")}
            if order_result.retcode in definitive or order_result.retcode == mt5.TRADE_RETCODE_DONE:
                self.pending_order.clear()
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
                    f"手数: {sizing['volume']}",
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

        fill_price = float(getattr(order_result, "price", 0) or 0)
        if not math.isfinite(fill_price) or fill_price <= 0:
            fill_price = None
        logger.info(f"✅ 下单成功: {action.upper()} {sizing['volume']} {SYMBOL}, price={price}, fill_price={fill_price}, SL={result.stop_loss}, TP={result.take_profit}")
        # On netting/hedging MT5 the new position ticket equals the opening order ticket.
        order_ticket = int(getattr(order_result, "order", 0) or 0)
        self.last_order_ticket = order_ticket
        if event_id and order_ticket > 0:
            if not self.strategy_ledger.record_position(order_ticket, event_id, time.time(),
                                                       contract["max_hold_minutes"], contract["cooldown_seconds"]):
                logger.error("策略持仓期限绑定失败，保留预提交事件供护盘恢复: %s", self.strategy_ledger.error)
        if order_ticket > 0 and not manual:
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
                f"手数: {sizing['volume']}",
                f"提交报价: {price}",
                f"成交价: {fill_price if fill_price is not None else '未知（回执未返回）'}",
                f"止损: {result.stop_loss}",
                f"止盈: {result.take_profit}",
                f"提交报价盈亏比: {live_rr:.2f}",
                *([] if manual else [f"置信度: {result.confidence}",
                                     f"BUY评分: {result.score_buy}", f"SELL评分: {result.score_sell}"]),
                f"理由: {result.reasoning}",
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ],
        )
        return True

    def evaluate_once(self):
        df_m15, df_h1, df_m5 = self.get_context()
        result = self.evaluate_context(df_m15, df_h1, df_m5)
        logger.info(
            f"📊 本地策略评分: action={result.action}, confidence={result.confidence}, "
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
            f"🚀 Crypto CFD 策略交易脚本启动: symbol={SYMBOL}, entry=策略契约声明, direction=H1, "
            f"volume=broker_sized, max_positions={MAX_POSITIONS}, threshold={CONFIDENCE_THRESHOLD}, "
            f"min_rr={MIN_REWARD_RISK}, align={'ON' if ALIGN_HARD_FILTER else 'OFF'}, "
            f"max_sl=asset_ATR_percentage_cap, chase={CHASE_MAX_ATR}ATR, trading={'ON' if ENABLE_TRADING else 'DRY-RUN'}"
        )
        logger.info("止损价格=%s BUY向下/SELL向上量化，严格保留M5极值缓冲与止损上限", STOP_PRICE_VERSION)
        logger.info("入场时钟=%s 本地时钟到达M15/M5收盘边界前等待，同根K线下轮重查，护盘继续", ENTRY_CLOCK_VERSION)
        strategies = STRATEGIES.snapshot()
        logger.info("%s 粗筛仅供AI，本地分不作自主AI派发门槛", ADAPTIVE_SESSION_VERSION)
        logger.info("策略框架=%s fingerprint=%s loaded=%s error=%s", STRATEGY_FRAMEWORK_VERSION,
                    strategies.fingerprint, [s.id for s in strategies.strategies if s.enabled], strategies.error or "无")

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
                    if self.entry_clock_ready(current_bar_time, current_m5_time):
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



# Operational entry is crypto_demo/run.py; gold self-tests intentionally omitted.
