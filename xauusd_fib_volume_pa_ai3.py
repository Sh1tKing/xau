# -*- coding: utf-8 -*-
"""XAUUSD three-role AI trader.

AI-1 proposes a direction from the local feature pack.
AI-2 may only approve or reject clear defects.
AI-3 may hold, tighten the stop, or close. It cannot widen a stop.
Python still enforces reward/risk, H1/M15 alignment, the 4 USD stop cap, and the local guardian.
"""

import json
import os
import sys
import threading
import time
from datetime import datetime

import requests


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ["FIB_PA_ORDER_TAG"] = "FIB_AI3"
os.environ["FIB_PA_TITLE_TAG"] = "三AI"

import xauusd_fib_volume_pa_trader as base


def env_int(name, default):
    return base.env_int(name, default)


def env_float(name, default):
    return base.env_float(name, default)


def env_bool(name, default):
    return base.env_bool(name, default)


AI_ENABLED = env_bool("FIB_PA_AI_ENABLED", True)
AI_ENABLE_TRADING = env_bool("FIB_PA_AI_ENABLE_TRADING", True)
AI_MAGIC = env_int("FIB_PA_AI_MAGIC", 240611)
AI_CONFIDENCE = env_int("FIB_PA_AI_CONFIDENCE", 75)
AI1_MODEL = os.getenv("FIB_PA_AI1_MODEL", os.getenv("GPTSTORE_MODEL", "gpt-5.5"))
AI2_MODEL = os.getenv("FIB_PA_AI2_MODEL", AI1_MODEL)
AI3_MODEL = os.getenv("FIB_PA_AI3_MODEL", AI1_MODEL)
DEFAULT_AI_URL = os.getenv("GPTSTORE_BASE_URL", "https://pool.gptstore.club/v1/chat/completions").strip()
AI1_URL = os.getenv("AI1_URL", DEFAULT_AI_URL).strip() or DEFAULT_AI_URL
AI2_URL = os.getenv("AI2_URL", DEFAULT_AI_URL).strip() or DEFAULT_AI_URL
AI3_URL = os.getenv("AI3_URL", DEFAULT_AI_URL).strip() or DEFAULT_AI_URL
AI1_KEY = os.getenv("AI1_KEY", os.getenv("GPTSTORE_API_KEY", "")).strip().strip('"').strip("'")
AI2_KEY = os.getenv("AI2_KEY", "").strip().strip('"').strip("'")
AI3_KEY = os.getenv("AI3_KEY", "").strip().strip('"').strip("'")
AI1_EFFORT = os.getenv("FIB_PA_AI1_EFFORT", "medium")
AI2_EFFORT = os.getenv("FIB_PA_AI2_EFFORT", "low")
AI3_EFFORT = os.getenv("FIB_PA_AI3_EFFORT", "low")
AI1_TIMEOUT = env_int("FIB_PA_AI1_TIMEOUT", 60)
AI2_TIMEOUT = env_int("FIB_PA_AI2_TIMEOUT", 40)
AI3_TIMEOUT = env_int("FIB_PA_AI3_TIMEOUT", 40)
ENTRY_TAKEOVER_SEC = env_int("FIB_PA_AI_ENTRY_TAKEOVER_SEC", 120)
AI3_TAKEOVER_SEC = env_int("FIB_PA_AI3_TAKEOVER_SEC", 75)
API_ALERT_COOLDOWN = env_int("FIB_PA_AI_ALERT_COOLDOWN", 900)

base.MAGIC = AI_MAGIC
base.ENABLE_TRADING = AI_ENABLE_TRADING

AI1_INSTRUCTIONS = (
    "你是 XAUUSD 分析师 AI-1。只根据给定结构化数据判断方向，不看账户，不决定手数，不执行下单。"
    "先看 regime。tight_range 或方向不清时 action 必须是 no。"
    "wide_range 只允许在区间边缘且有拒绝K线时交易，中轴追单必须是 no。trend 才按趋势回撤做。"
    "方向只看 H1。M15 负责斐波那契、成交量和价格行为。M5 不单独决定方向；最近3根M5的拒绝极值加缓冲后仍不超过4美元时，可以用该极值进场。H4 不是否决条件。"
    "M15 与 H1 暂时反向是回撤，不是冲突，不能因此 no。"
    "M15影线离收盘价超过4美元时，如果最近3根M5的拒绝极值仍在4美元内，止损放在该M5极值外；禁止把更远的影线止损改小。"
    "H1 不支持该方向、止损超过 4 美元、价格偏离 M15 EMA21 超过 1.5 ATR、止损止盈方向错误或盈亏比低于 1.5 时，action 必须是 no。"
    "stop_loss 和 take_profit 用绝对价格。最终回复只能是一个 JSON 对象，不要 Markdown。"
)
AI2_INSTRUCTIONS = (
    "你是独立风控审核官 AI-2。不能提出新交易，只能 APPROVE 或 REJECT。"
    "只有明确缺陷才能 REJECT：regime 为 tight_range、宽震荡却在中轴追单、止损方向错误、盈亏比低于 1.5、H1 不支持该方向、止损超过 4 美元、追价超过 1.5 ATR、止损或止盈缺失、关键数据缺失、订单自相矛盾。"
    "M15 与 H1 暂时反向、轻微背离、逆 H4、波动大或感觉不确定，都不能单独 REJECT。"
    "最终回复只能是一个 JSON 对象，不要 Markdown。"
)
AI3_INSTRUCTIONS = (
    "你是持仓监控 AI-3。只能选择 hold、tighten_sl 或 close。"
    "禁止放宽止损，禁止加仓，禁止新开仓。tighten_sl 必须比当前止损更有利，并给出绝对价格。"
    "窄震荡里的上下影线和轻微回撤选择 hold，交给本地虚拟锁盈。只有结构失效或强反向才 close。"
    "最终回复只能是一个 JSON 对象，不要 Markdown。"
)


def attach_ai_logger():
    log_path = os.path.join(base.LOG_DIR, "Fib_PA_AI3_%s.log" % datetime.now().strftime("%Y-%m-%d"))
    handler = base.DurableFileHandler(log_path, encoding="utf-8")
    handler.setFormatter(base.logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    base.logger.addHandler(handler)
    return log_path

def bars_for_prompt(df, limit=8):
    if df is None or len(df) == 0:
        return []
    rows = []
    for row in df.tail(limit).itertuples(index=False):
        rows.append({
            "time": int(getattr(row, "time", 0) or 0),
            "open": round(float(row.open), 2),
            "high": round(float(row.high), 2),
            "low": round(float(row.low), 2),
            "close": round(float(row.close), 2),
            "tick_volume": round(float(getattr(row, "tick_volume", 0.0) or 0.0), 2),
        })
    return rows


def fib_payload(fib):
    if fib is None:
        return None
    return {
        "direction": fib.direction,
        "swing_low": round(float(fib.swing_low), 2),
        "swing_high": round(float(fib.swing_high), 2),
        "levels": {key: round(float(value), 2) for key, value in fib.levels.items()},
    }


def build_feature_pack(df_m15, df_h1, df_m5, local_result):
    atr = base.calculate_atr(df_m15)
    price = float(df_m15["close"].iloc[-1])
    h1_trend = base.trend_from_ema(df_h1, 50)
    m15_trend = base.trend_from_ema(df_m15, 50)
    fib = base.entry_fib_structure(df_m15, h1_trend, atr)
    pack = {
        "symbol": base.SYMBOL,
        "timeframe": "M15",
        "direction_timeframe": "H1",
        "price": round(price, 2),
        "atr": round(atr, 2),
        "h1_trend": h1_trend,
        "m15_trend": m15_trend,
        "m15_ema21_distance_atr": round(base.ema_distance_atr(df_m15, atr, 21), 2),
        "regime": (local_result.details or {}).get("regime", ""),
        "regime_text": (local_result.details or {}).get("regime_text", ""),
        "fib": fib_payload(fib),
        "local_action": local_result.action,
        "local_confidence": local_result.confidence,
        "local_stop_loss": local_result.stop_loss,
        "local_take_profit": local_result.take_profit,
        "score_buy": local_result.score_buy,
        "score_sell": local_result.score_sell,
        "local_reasoning": local_result.reasoning,
        "details": local_result.details,
        "rules": {
            "h1_sets_direction": True,
            "m15_opposite_is_pullback": True,
            "h4_is_not_a_veto": True,
            "tight_range_is_no": True,
            "wide_range_only_at_edges": True,
            "max_stop_abs": base.MAX_SL_ABS,
            "chase_max_atr": base.CHASE_MAX_ATR,
            "min_rr": base.MIN_REWARD_RISK,
        },
        "m15_bars": bars_for_prompt(df_m15, 12),
        "h1_bars": bars_for_prompt(df_h1, 8),
        "m5_bars": bars_for_prompt(df_m5, 8),
    }
    return pack, fib, atr, price
def normalize_confidence(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if 0 < number <= 1:
        number *= 100
    return int(max(0, min(100, round(number))))


def extract_json_object(text):
    if not text or not str(text).strip():
        return None
    cleaned = str(text).strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def extract_chat_text(result):
    if not isinstance(result, dict):
        return ""
    for choice in result.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            joined = "\n".join(part.strip() for part in parts if part and part.strip())
            if joined:
                return joined
    return ""


def summarize_chat_result(result):
    if not isinstance(result, dict):
        return str(result)[:300]
    choices = result.get("choices") or []
    finish = None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish = choices[0].get("finish_reason")
    return {
        "id": result.get("id"),
        "model": result.get("model"),
        "choices": len(choices) if isinstance(choices, list) else 0,
        "finish_reason": finish,
        "usage": result.get("usage"),
    }

def call_chat(role, model, effort, instructions, prompt, timeout, url, api_key):
    key = str(api_key or "").strip()
    if not key:
        raise RuntimeError("%s 缺少 API key" % role)
    url = str(url or "").strip()
    if not url:
        raise RuntimeError("%s 缺少请求 URL" % role)
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
    }
    attempts = (("primary", effort), ("low_retry", "low"))
    for index, (label, this_effort) in enumerate(attempts, start=1):
        data = {
            "model": model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        if this_effort:
            data["reasoning"] = {"effort": this_effort}
        response = requests.post(url, headers=headers, json=data, timeout=timeout)
        if response.status_code == 400 and "reasoning" in response.text.lower():
            data.pop("reasoning", None)
            base.logger.warning("%s 拒绝 reasoning 参数，去掉后重试 (%s)" % (role, label))
            response = requests.post(url, headers=headers, json=data, timeout=timeout)
        if response.status_code != 200:
            base.logger.error(
                "%s 调用失败 (%s/%s, %s): %s - %s"
                % (role, index, len(attempts), label, response.status_code, response.text[:400])
            )
            continue
        try:
            response.encoding = "utf-8"
            result = response.json()
        except ValueError:
            base.logger.error("%s 返回非 JSON (%s): %s" % (role, label, response.text[:300]))
            continue
        text = extract_chat_text(result)
        if text:
            if index > 1:
                base.logger.info("%s 重试后得到可见输出 (%s)" % (role, label))
            return text
        base.logger.warning("%s 可见输出为空 (%s): %s" % (role, label, summarize_chat_result(result)))
    return ""


def prices_are_valid(action, entry, stop_loss, take_profit):
    if action == "buy":
        return stop_loss > 0 and stop_loss < entry and take_profit > entry
    if action == "sell":
        return stop_loss > entry and take_profit > 0 and take_profit < entry
    return False


def clamp_proposal(action, entry, stop_loss, take_profit, atr):
    min_risk, max_risk = base.entry_risk_limits(atr)
    if action == "buy":
        risk = entry - stop_loss
        if risk > max_risk:
            return 0.0, 0.0
        if risk <= 0 or risk < min_risk:
            stop_loss = entry - min_risk
        stop_loss = round(float(stop_loss), 2)
        risk = entry - stop_loss
        if risk <= 0 or risk > max_risk + 0.001:
            return 0.0, 0.0
        far_tp = entry + 1.8 * risk
        if take_profit <= entry or take_profit > far_tp:
            take_profit = far_tp
        return stop_loss, round(float(take_profit), 2)

    risk = stop_loss - entry
    if risk > max_risk:
        return 0.0, 0.0
    if risk <= 0 or risk < min_risk:
        stop_loss = entry + min_risk
    stop_loss = round(float(stop_loss), 2)
    risk = stop_loss - entry
    if risk <= 0 or risk > max_risk + 0.001:
        return 0.0, 0.0
    far_tp = entry - 1.8 * risk
    if take_profit >= entry or take_profit < far_tp:
        take_profit = far_tp
    return stop_loss, round(float(take_profit), 2)
def finalize_ai_proposal(payload, entry, atr, h1_trend, m15_trend, fib, min_confidence=None, df_m15=None, df_m5=None, regime=None, regime_location=None, pa_score=0, local_stop=0.0, local_target=0.0):
    if min_confidence is None:
        min_confidence = AI_CONFIDENCE
    if not isinstance(payload, dict):
        return None, "AI-1 没有返回有效 JSON"
    action = str(payload.get("action", "no")).strip().lower()
    if action not in ("buy", "sell", "no"):
        action = "no"
    confidence = normalize_confidence(payload.get("confidence", 0))
    reasoning = str(payload.get("reasoning", "")).strip()
    if action == "no":
        return None, reasoning or "AI-1 选择观望"
    if confidence < min_confidence:
        return None, "AI-1 置信度 %s < %s" % (confidence, min_confidence)
    if regime == "tight_range":
        return None, "Python拒绝: 窄震荡或方向不清，不交易"
    if regime == "wide_range" and regime_location is not None:
        if df_m15 is not None:
            pa_score, _ = base.classify_price_action(df_m15, action, atr)
        if pa_score < 12:
            return None, "Python拒绝: 宽震荡缺少边缘拒绝"
        if action == "buy" and float(regime_location) > base.WIDE_EDGE:
            return None, "Python拒绝: 宽震荡不在下沿"
        if action == "sell" and float(regime_location) < 1.0 - base.WIDE_EDGE:
            return None, "Python拒绝: 宽震荡不在上沿"
    if not base.trends_support(action, h1_trend, m15_trend):
        return None, "Python拒绝: %s 与H1不同向(H1=%s, M15=%s)" % (
            action.upper(), h1_trend, m15_trend
        )

    try:
        stop_loss = float(payload.get("stop_loss") or 0)
        take_profit = float(payload.get("take_profit") or 0)
    except (TypeError, ValueError):
        stop_loss, take_profit = 0.0, 0.0

    risk = (entry - stop_loss) if action == "buy" else (stop_loss - entry)
    if prices_are_valid(action, entry, stop_loss, take_profit) and 0 < risk <= base.MAX_SL_ABS:
        stop_loss, take_profit = clamp_proposal(action, entry, stop_loss, take_profit, atr)
    else:
        stop_loss, take_profit = 0.0, 0.0

    if not prices_are_valid(action, entry, stop_loss, take_profit):
        replaced = False
        if prices_are_valid(action, entry, local_stop, local_target):
            local_risk = (entry - local_stop) if action == "buy" else (local_stop - entry)
            if 0 < local_risk <= base.MAX_SL_ABS:
                stop_loss, take_profit = float(local_stop), float(local_target)
                replaced = True
                reasoning = (reasoning + "；AI止损放不下，改用本地结构止损").strip("；")
        if not replaced:
            timed = base.m5_rejection_anchor(df_m5, action, atr) if df_m5 is not None else None
            if timed is None:
                return None, "Python拒绝: 结构止损超过小资金上限 %.2f" % base.MAX_SL_ABS
            entry, setup_price = timed
            stop_loss, take_profit = base.calculate_targets(action, entry, atr, fib, setup_price)
            if not prices_are_valid(action, entry, stop_loss, take_profit):
                return None, "Python拒绝: 结构止损超过小资金上限 %.2f" % base.MAX_SL_ABS
            reasoning = (reasoning + "；AI止损放不下，改用仍在4美元内的M5极值").strip("；")

    if not prices_are_valid(action, entry, stop_loss, take_profit):
        return None, "Python拒绝: 止损止盈方向非法或超过 %.2f 美元上限" % base.MAX_SL_ABS
    guarded_sl, guarded_tp, guard_note = base.reject_inside_m5_wick(
        action, entry, stop_loss, take_profit, atr, fib, df_m5
    )
    if guarded_sl <= 0 and guard_note:
        return None, "Python拒绝: " + guard_note
    if guard_note:
        stop_loss, take_profit = guarded_sl, guarded_tp
        reasoning = (reasoning + "；" + guard_note).strip("；")
    if not prices_are_valid(action, entry, stop_loss, take_profit):
        return None, "Python拒绝: " + (guard_note or base.m5_wick_reject_text())
    if df_m15 is not None:
        chased, chase_text = base.chase_blocks_at(df_m15, entry, action, atr)
        if chased:
            return None, "Python拒绝: " + chase_text
    if df_m5 is not None:
        blocked, m5_text = base.m5_blocks_entry(df_m5, action)
        if blocked:
            return None, "Python拒绝: " + m5_text
    approved, review_text = base.review_entry(
        action, entry, stop_loss, take_profit, h1_trend, m15_trend, base.MIN_REWARD_RISK, base.ALIGN_HARD_FILTER
    )
    full_reason = ("AI-1: %s。%s" % (reasoning, review_text)) if reasoning else review_text
    if not approved:
        return None, full_reason
    result = base.StrategyResult(
        action=action,
        confidence=confidence,
        close_all=False,
        reasoning=full_reason,
        stop_loss=stop_loss,
        take_profit=take_profit,
        score_buy=0,
        score_sell=0,
        details={"source": "ai1", "h1_trend": h1_trend, "m15_trend": m15_trend},
    )
    return result, full_reason
def ai2_allows(payload):
    if not isinstance(payload, dict):
        return False, "AI-2 无有效 JSON，拒绝开仓"
    decision = str(payload.get("decision", "")).strip().upper()
    defects = payload.get("defects") or []
    if not isinstance(defects, list):
        defects = [str(defects)]
    reasoning = str(payload.get("reasoning", "")).strip()
    if decision != "APPROVE":
        defect_text = "，".join(str(item) for item in defects) if defects else "未列明"
        return False, "AI-2 拒绝: %s，缺陷=%s。%s" % (decision or "EMPTY", defect_text, reasoning)
    return True, reasoning or "AI-2 APPROVE"


def task_is_current(seq, bar_time, current_seq, current_bar):
    return seq == current_seq and bar_time == current_bar


def prompt_json(title, payload):
    return title + "\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

class ThreeAITrader(base.FibVolumePATrader):
    def __init__(self):
        super().__init__()
        self._mt5_lock = threading.RLock()
        self._entry_lock = threading.Lock()
        self._entry_inflight = False
        self._entry_seq = 0
        self._entry_bar_time = 0
        self._entry_started = 0.0
        self._ai3_lock = threading.Lock()
        self._ai3_inflight = False
        self._ai3_seq = 0
        self._ai3_bar_time = 0
        self._ai3_started = 0.0
        self._stop = threading.Event()
        self._last_api_alert = 0.0

    def get_rates(self, timeframe, count, shift=1):
        with self._mt5_lock:
            return super().get_rates(timeframe, count, shift)

    def get_positions(self):
        with self._mt5_lock:
            positions = super().get_positions()
        return [item for item in positions if int(getattr(item, "magic", 0) or 0) == base.MAGIC]

    def current_position_count(self):
        return len(self.get_positions())

    def place_order(self, result):
        with self._mt5_lock:
            return super().place_order(result)

    def modify_position_sl(self, position, new_sl, reason):
        with self._mt5_lock:
            return super().modify_position_sl(position, new_sl, reason)

    def close_position_by_ticket(self, position, reason):
        with self._mt5_lock:
            return super().close_position_by_ticket(position, reason)

    def latest_bar_time(self, timeframe):
        with self._mt5_lock:
            rates = base.mt5.copy_rates_from_pos(base.SYMBOL, timeframe, 0, 1)
        if rates is None or len(rates) == 0:
            return 0
        return int(rates[0]["time"])

    def claim_task(self, kind, bar_time, timeout_sec):
        now = time.time()
        lock = self._entry_lock if kind == "entry" else self._ai3_lock
        with lock:
            if kind == "entry":
                inflight = self._entry_inflight
                started = self._entry_started
            else:
                inflight = self._ai3_inflight
                started = self._ai3_started
            if inflight and now - started < timeout_sec:
                return None
            if inflight:
                base.logger.warning("%s 上一轮超过 %ss，新K线接管" % (kind, timeout_sec))
            if kind == "entry":
                self._entry_seq += 1
                seq = self._entry_seq
                self._entry_bar_time = bar_time
                self._entry_inflight = True
                self._entry_started = now
            else:
                self._ai3_seq += 1
                seq = self._ai3_seq
                self._ai3_bar_time = bar_time
                self._ai3_inflight = True
                self._ai3_started = now
            return seq

    def release_task(self, kind, seq):
        lock = self._entry_lock if kind == "entry" else self._ai3_lock
        with lock:
            if kind == "entry" and self._entry_seq == seq:
                self._entry_inflight = False
            if kind == "ai3" and self._ai3_seq == seq:
                self._ai3_inflight = False

    def entry_is_current(self, seq, bar_time):
        with self._entry_lock:
            return task_is_current(seq, bar_time, self._entry_seq, self._entry_bar_time)

    def ai3_is_current(self, seq, bar_time):
        with self._ai3_lock:
            return task_is_current(seq, bar_time, self._ai3_seq, self._ai3_bar_time)

    def notify_lines(self, title, lines):
        base.send_feishu_notification(title, lines)

    def alert_api_failure(self, role, detail):
        message = "%s 请求失败: %s" % (role, detail)
        base.logger.error(message)
        now = time.time()
        if now - self._last_api_alert < API_ALERT_COOLDOWN:
            return
        self._last_api_alert = now
        self.notify_lines(
            "⚠️ %s %s 请求失败" % (base.SYMBOL, role),
            [detail, datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        )

    def dispatch_m5_entry(self, bar_time):
        try:
            df_m15, df_h1, df_m5 = self.get_context()
            local_result = base.build_fib_volume_pa_decision(
                df_m15, df_h1, df_m5, base.CONFIDENCE_THRESHOLD
            )
        except Exception as exc:
            base.logger.error("M5时机评估异常: %s: %s" % (type(exc).__name__, exc))
            return
        if local_result.action not in ("buy", "sell"):
            base.logger.info(
                "M5时机未到 action=%s score=%s"
                % (local_result.action, local_result.confidence)
            )
            return
        base.logger.info(
            "M5拒绝仍在4美元内，派发开仓 action=%s score=%s SL=%s"
            % (local_result.action, local_result.confidence, local_result.stop_loss)
        )
        self.dispatch_entry(bar_time)

    def dispatch_entry(self, bar_time):
        seq = self.claim_task("entry", bar_time, ENTRY_TAKEOVER_SEC)
        if seq is None:
            base.logger.info("AI-1/2 仍在进行，跳过本根K线")
            return
        base.logger.info("AI-1 已派发分析线程 bar=%s seq=%s" % (bar_time, seq))
        threading.Thread(target=self.entry_task, args=(bar_time, seq), daemon=True).start()

    def entry_task(self, bar_time, seq):
        try:
            if self._stop.is_set():
                return
            df_m15, df_h1, df_m5 = self.get_context()
            local_result = base.build_fib_volume_pa_decision(
                df_m15, df_h1, df_m5, base.CONFIDENCE_THRESHOLD
            )
            pack, fib, atr, price = build_feature_pack(df_m15, df_h1, df_m5, local_result)
            reject = ""
            if local_result.action == "no" and "拒绝:" in (local_result.reasoning or ""):
                reject = " 拒绝:" + local_result.reasoning.split("拒绝:", 1)[1].strip().replace("\n", " ")[:160]
            base.logger.info(
                "本地评分 action=%s confidence=%s BUY=%s SELL=%s%s"
                % (
                    local_result.action,
                    local_result.confidence,
                    local_result.score_buy,
                    local_result.score_sell,
                    reject,
                )
            )
            if not AI_ENABLED:
                if local_result.action in ("buy", "sell"):
                    self.place_order(local_result)
                return
            if not self.entry_is_current(seq, bar_time):
                base.logger.info("AI-1 任务已过期，取消请求 seq=%s" % seq)
                return
            raw = call_chat(
                "AI-1",
                AI1_MODEL,
                AI1_EFFORT,
                AI1_INSTRUCTIONS,
                prompt_json(
                    "请只输出 JSON: {\"action\":\"buy|sell|no\",\"confidence\":0-100,\"stop_loss\":0,\"take_profit\":0,\"reasoning\":\"...\"}",
                    pack,
                ),
                AI1_TIMEOUT,
                AI1_URL,
                AI1_KEY,
            )
            if not raw:
                self.alert_api_failure("AI-1", "没有可见 JSON")
                return
            if not self.entry_is_current(seq, bar_time) or self._stop.is_set():
                base.logger.info("AI-1 返回已过期，丢弃 seq=%s" % seq)
                return
            entry_price = float((local_result.details or {}).get("entry_price") or price)
            proposal, reason = finalize_ai_proposal(
                extract_json_object(raw), entry_price, atr, pack["h1_trend"], pack["m15_trend"], fib,
                df_m15=df_m15, df_m5=df_m5, regime=pack.get("regime"),
                regime_location=(local_result.details or {}).get("location"),
                pa_score=int((local_result.details or {}).get("pa_score") or 0),
                local_stop=local_result.stop_loss, local_target=local_result.take_profit,
            )
            base.logger.info("AI-1 结果: %s" % reason)
            if proposal is None:
                if "观望" not in reason and "未给出" not in reason:
                    self.notify_lines(
                        "✋ %s AI-1/Python 未放行" % base.SYMBOL,
                        [reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                    )
                return
            proposal.score_buy = local_result.score_buy
            proposal.score_sell = local_result.score_sell
            review_pack = {
                "proposal": proposal.to_decision(),
                "entry_price": price,
                "atr": pack["atr"],
                "h1_trend": pack["h1_trend"],
                "m15_trend": pack["m15_trend"],
                "regime": pack.get("regime"),
                "regime_text": pack.get("regime_text"),
                "max_stop_abs": base.MAX_SL_ABS,
                "min_rr": base.MIN_REWARD_RISK,
                "local_action": local_result.action,
                "local_reasoning": local_result.reasoning,
            }
            review_raw = call_chat(
                "AI-2",
                AI2_MODEL,
                AI2_EFFORT,
                AI2_INSTRUCTIONS,
                prompt_json(
                    "请只输出 JSON: {\"decision\":\"APPROVE|REJECT\",\"defects\":[],\"reasoning\":\"...\"}",
                    review_pack,
                ),
                AI2_TIMEOUT,
                AI2_URL,
                AI2_KEY,
            )
            if not self.entry_is_current(seq, bar_time) or self._stop.is_set():
                base.logger.info("AI-2 返回已过期，丢弃 seq=%s" % seq)
                return
            if not review_raw:
                self.alert_api_failure("AI-2", "没有可见 JSON，拒绝开仓")
                return
            allowed, review_reason = ai2_allows(extract_json_object(review_raw))
            base.logger.info("AI-2 结果: %s" % review_reason)
            if not allowed:
                self.notify_lines(
                    "✋ %s AI-2 未通过" % base.SYMBOL,
                    [
                        "方向: %s" % proposal.action.upper(),
                        "止损: %s" % proposal.stop_loss,
                        "止盈: %s" % proposal.take_profit,
                        review_reason,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ],
                )
                return
            proposal.reasoning = "%s。%s" % (proposal.reasoning, review_reason)
            self.place_order(proposal)
        except Exception as exc:
            base.logger.error("AI开仓线程异常: %s: %s" % (type(exc).__name__, exc))
        finally:
            self.release_task("entry", seq)

    def dispatch_ai3(self, bar_time):
        if not AI_ENABLED or not self.get_positions():
            return
        seq = self.claim_task("ai3", bar_time, AI3_TAKEOVER_SEC)
        if seq is None:
            base.logger.info("AI-3 仍在进行，跳过本根 M1")
            return
        base.logger.info("AI-3 已派发护盘线程 bar=%s seq=%s" % (bar_time, seq))
        threading.Thread(target=self.ai3_task, args=(bar_time, seq), daemon=True).start()

    def ai3_task(self, bar_time, seq):
        try:
            if self._stop.is_set():
                return
            positions = self.get_positions()
            if not positions:
                return
            df_m1 = self.get_rates(base.mt5.TIMEFRAME_M1, 40, shift=1)
            df_m5 = self.get_rates(base.mt5.TIMEFRAME_M5, 40, shift=1)
            tick = None
            with self._mt5_lock:
                tick = base.mt5.symbol_info_tick(base.SYMBOL)
            payload = {
                "symbol": base.SYMBOL,
                "bid": float(tick.bid) if tick else 0,
                "ask": float(tick.ask) if tick else 0,
                "atr_m5": round(base.calculate_atr(df_m5), 2),
                "m1_bars": bars_for_prompt(df_m1, 12),
                "m5_bars": bars_for_prompt(df_m5, 8),
                "positions": [],
            }
            for position in positions:
                ticket = int(position.ticket)
                state = self.guardian_states.get(ticket)
                payload["positions"].append({
                    "ticket": ticket,
                    "direction": "BUY" if position.type == base.mt5.POSITION_TYPE_BUY else "SELL",
                    "volume": float(position.volume),
                    "entry": float(position.price_open),
                    "sl": float(position.sl or 0),
                    "tp": float(position.tp or 0),
                    "profit": round(float(position.profit or 0), 2),
                    "virtual_sl": round(float(state.virtual_sl), 2) if state else 0,
                })
            raw = call_chat(
                "AI-3",
                AI3_MODEL,
                AI3_EFFORT,
                AI3_INSTRUCTIONS,
                prompt_json(
                    "请只输出 JSON: {\"action\":\"hold|tighten_sl|close\",\"ticket\":0,\"new_stop_loss\":0,\"reasoning\":\"...\"}",
                    payload,
                ),
                AI3_TIMEOUT,
                AI3_URL,
                AI3_KEY,
            )
            if not self.ai3_is_current(seq, bar_time) or self._stop.is_set():
                base.logger.info("AI-3 返回已过期，丢弃 seq=%s" % seq)
                return
            if not raw:
                self.alert_api_failure("AI-3", "没有可见 JSON，本地护盘继续")
                return
            self.execute_ai3(extract_json_object(raw))
        except Exception as exc:
            base.logger.error("AI-3 线程异常: %s: %s" % (type(exc).__name__, exc))
        finally:
            self.release_task("ai3", seq)

    def execute_ai3(self, payload):
        if not isinstance(payload, dict):
            base.logger.warning("AI-3 JSON 无效，忽略")
            return
        action = str(payload.get("action", "hold")).strip().lower()
        reasoning = str(payload.get("reasoning", "")).strip()
        if action in ("hold", "keep", "none", ""):
            base.logger.info("AI-3 持有: %s" % (reasoning or "无"))
            return
        positions = self.select_ai3_positions(payload)
        if not positions:
            base.logger.info("AI-3 没有匹配到本脚本持仓，忽略 action=%s" % action)
            return
        if action in ("close", "close_all"):
            for position in positions:
                self.close_position_by_ticket(position, "AI-3: %s" % (reasoning or "close"))
            return
        if action not in ("tighten_sl", "tighten"):
            base.logger.info("AI-3 未知动作 %s，忽略" % action)
            return
        try:
            new_sl = float(payload.get("new_stop_loss") or payload.get("stop_loss") or 0)
        except (TypeError, ValueError):
            new_sl = 0
        if new_sl <= 0:
            base.logger.info("AI-3 收紧止损缺少价格，忽略")
            return
        for position in positions:
            direction = "BUY" if position.type == base.mt5.POSITION_TYPE_BUY else "SELL"
            current_sl = float(position.sl or 0)
            if not base.sl_change_is_tighter(direction, current_sl, new_sl):
                base.logger.info("AI-3 止损不会放宽 ticket=%s %s -> %s" % (position.ticket, current_sl, new_sl))
                continue
            self.modify_position_sl(position, new_sl, "AI-3 tighten")

    def select_ai3_positions(self, payload):
        positions = self.get_positions()
        try:
            ticket = int(payload.get("ticket") or 0)
        except (TypeError, ValueError):
            ticket = 0
        if ticket:
            return [item for item in positions if int(item.ticket) == ticket]
        if len(positions) == 1:
            return positions
        return []

    def run(self, once=False):
        if not self.connect_mt5():
            return 1
        log_path = attach_ai_logger()
        base.logger.info(
            "三AI交易启动 symbol=%s magic=%s trading=%s ai=%s confidence=%s min_rr=%s models=%s/%s/%s log=%s"
            % (
                base.SYMBOL,
                base.MAGIC,
                "ON" if base.ENABLE_TRADING else "DRY-RUN",
                "ON" if AI_ENABLED else "OFF",
                AI_CONFIDENCE,
                base.MIN_REWARD_RISK,
                AI1_MODEL,
                AI2_MODEL,
                AI3_MODEL,
                log_path,
            )
        )
        base.logger.info("AI URLs: AI1=%s AI2=%s AI3=%s" % (AI1_URL, AI2_URL, AI3_URL))
        base.logger.info(
            "AI keys: AI1=%s AI2=%s AI3=%s"
            % ("set" if AI1_KEY else "missing", "set" if AI2_KEY else "missing", "set" if AI3_KEY else "missing")
        )
        self.notify_lines(
            "🚀 %s 三AI交易启动" % base.SYMBOL,
            [
                "magic: %s" % base.MAGIC,
                "下单: %s" % ("开启" if base.ENABLE_TRADING else "关闭"),
                "AI-1: %s / %s" % (AI1_MODEL, AI1_EFFORT),
                "AI-2: %s / %s" % (AI2_MODEL, AI2_EFFORT),
                "AI-3: %s / %s" % (AI3_MODEL, AI3_EFFORT),
                "时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ],
        )
        # Evaluate the latest closed M15 immediately so a valid signal can place an order.
        last_m15 = 0
        last_m5 = 0
        last_m1 = 0
        if base.ENABLE_TRADING:
            base.logger.info("下单已开启，启动后立即评估最新已收盘M15")
        else:
            base.logger.info("下单关闭，仅评估不下单")
        try:
            while not self._stop.is_set():
                try:
                    self.run_guardian()
                except Exception as exc:
                    base.logger.error("护盘异常: %s: %s" % (type(exc).__name__, exc))
                try:
                    self.sync_position_close_notifications()
                except Exception as exc:
                    base.logger.error("平仓监听异常: %s: %s" % (type(exc).__name__, exc))

                m15_time = self.latest_bar_time(base.ENTRY_BAR_TIMEFRAME)
                m5_time = self.latest_bar_time(base.mt5.TIMEFRAME_M5)
                m1_time = self.latest_bar_time(base.mt5.TIMEFRAME_M1)
                if m15_time and m15_time != last_m15:
                    last_m15 = m15_time
                    if once:
                        seq = self.claim_task("entry", m15_time, ENTRY_TAKEOVER_SEC)
                        if seq is not None:
                            self.entry_task(m15_time, seq)
                        return 0
                    self.dispatch_entry(m15_time)
                if m5_time and m5_time != last_m5 and m5_time != m15_time and not once:
                    last_m5 = m5_time
                    self.dispatch_m5_entry(m5_time)
                if m1_time and m1_time != last_m1:
                    last_m1 = m1_time
                    if not once:
                        self.dispatch_ai3(m1_time)
                time.sleep(base.POLL_SECONDS)
        except KeyboardInterrupt:
            base.logger.info("收到人工停止信号")
            return 0
        finally:
            self._stop.set()
            with self._mt5_lock:
                base.mt5.shutdown()
            base.logger.info("MT5 已关闭")

def run_self_test():
    fenced = extract_json_object("```json\n{\"action\":\"buy\",\"confidence\":0.8}\n```")
    assert fenced["action"] == "buy", fenced
    assert normalize_confidence(0.8) == 80
    assert normalize_confidence(75) == 75
    assert not task_is_current(1, 10, 2, 10)
    assert task_is_current(2, 10, 2, 10)

    passed, reason = finalize_ai_proposal(
        {"action": "buy", "confidence": 88, "stop_loss": 96, "take_profit": 107, "reasoning": "结构回踩"},
        100, 2.0, "bull", "bull", None, 75,
    )
    assert passed is not None and passed.action == "buy", reason
    assert passed.take_profit <= 100 + 1.8 * (100 - passed.stop_loss) + 0.02

    rejected, reject_reason = finalize_ai_proposal(
        {"action": "buy", "confidence": 90, "stop_loss": 96, "take_profit": 102, "reasoning": "目标太近"},
        100, 2.0, "bull", "bull", None, 75,
    )
    assert rejected is None and "盈亏比" in reject_reason, reject_reason

    against, against_reason = finalize_ai_proposal(
        {"action": "sell", "confidence": 91, "stop_loss": 104, "take_profit": 92, "reasoning": "逆势"},
        100, 2.0, "bull", "bull", None, 75,
    )
    assert against is None and "不同向" in against_reason, against_reason
    wide, wide_reason = finalize_ai_proposal(
        {"action": "buy", "confidence": 90, "stop_loss": 90, "take_profit": 120, "reasoning": "wide"},
        100, 2.0, "bull", "bull", None, 75,
    )
    assert wide is None and "4" in wide_reason, wide_reason

    tight_sl, tight_tp = clamp_proposal("sell", 100.0, 102.5, 90.0, 10.0)
    assert 100.0 < tight_sl <= 100.0 + base.MAX_SL_ABS + 0.02, (tight_sl, tight_tp)
    local_kept, local_reason = finalize_ai_proposal(
        {"action": "sell", "confidence": 88, "stop_loss": 112, "take_profit": 90, "reasoning": "远影线"},
        100, 10.0, "bear", "bear", None, 75, local_stop=103.2, local_target=94.0,
    )
    assert local_kept is not None and abs(local_kept.stop_loss - 103.2) < 0.001, local_reason
    near_m5 = base.pd.DataFrame([
        {"open": 108.0, "high": 109.2, "low": 107.4, "close": 107.6},
        {"open": 107.6, "high": 109.0, "low": 107.2, "close": 107.4},
        {"open": 107.4, "high": 109.1, "low": 106.8, "close": 107.0},
    ])
    timed_kept, timed_reason = finalize_ai_proposal(
        {"action": "sell", "confidence": 88, "stop_loss": 120, "take_profit": 90, "reasoning": "远影线"},
        107.0, 10.0, "bear", "bear", None, 75, df_m5=near_m5,
    )
    assert timed_kept is not None and 0 < timed_kept.stop_loss - 107.0 <= base.MAX_SL_ABS + 0.02, timed_reason
    far_m5 = base.pd.DataFrame([
        {"open": 100.0, "high": 111.0, "low": 99.0, "close": 100.0},
        {"open": 100.0, "high": 111.5, "low": 99.0, "close": 100.0},
        {"open": 100.0, "high": 112.0, "low": 99.0, "close": 99.5},
    ])
    far_kept, far_reason = finalize_ai_proposal(
        {"action": "sell", "confidence": 88, "stop_loss": 120, "take_profit": 90, "reasoning": "远影线"},
        99.5, 2.0, "bear", "bear", None, 75, df_m5=far_m5,
    )
    assert far_kept is None and "4" in far_reason, far_reason

    wick_m5 = base.pd.DataFrame([
        {"open": 100.2, "high": 104.8, "low": 99.4, "close": 99.8},
        {"open": 99.8, "high": 104.6, "low": 99.2, "close": 99.6},
        {"open": 99.6, "high": 104.4, "low": 99.0, "close": 99.4},
    ])
    inside_wick, inside_reason = finalize_ai_proposal(
        {"action": "sell", "confidence": 88, "stop_loss": 102.0, "take_profit": 94.0, "reasoning": "影线内止损"},
        100.0, 2.0, "bear", "bear", None, 75, df_m5=wick_m5, local_stop=102.0, local_target=94.0,
    )
    assert inside_wick is None and "影线" in inside_reason, inside_reason
    near_wick = base.pd.DataFrame([
        {"open": 100.4, "high": 101.2, "low": 99.8, "close": 100.1},
        {"open": 100.1, "high": 101.1, "low": 99.6, "close": 99.9},
        {"open": 99.9, "high": 101.0, "low": 99.4, "close": 99.6},
    ])
    moved, moved_reason = finalize_ai_proposal(
        {"action": "sell", "confidence": 88, "stop_loss": 101.4, "take_profit": 96.0, "reasoning": "止损偏近"},
        100.0, 2.0, "bear", "bear", None, 75, df_m5=near_wick,
    )
    assert moved is not None and moved.stop_loss >= 101.6 - 0.01, moved_reason

    assert ai2_allows({"decision": "APPROVE", "reasoning": "ok"})[0]
    assert not ai2_allows({"decision": "REJECT", "defects": ["盈亏比不足"]})[0]
    assert not ai2_allows(None)[0]
    assert not base.sl_change_is_tighter("BUY", 100, 99)
    assert base.sl_change_is_tighter("SELL", 110, 109)
    print("AI3 SELF-TEST OK")
    print(passed.to_decision())
    print(reject_reason)
    print(against_reason)
    return 0


def main():
    parser = base.argparse.ArgumentParser(description="XAUUSD three-AI Fib/volume/price-action trader.")
    parser.add_argument("--once", action="store_true", help="连接后只评估一次")
    parser.add_argument("--self-test", action="store_true", help="运行本地规则测试，不连接 MT5")
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()
    trader = ThreeAITrader()
    return trader.run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
