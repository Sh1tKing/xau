# -*- coding: utf-8 -*-
"""XAUUSD three-role AI trader.

AI-1 proposes a direction from the local feature pack.
AI-2 may only approve or reject clear defects.
AI-3 may hold, tighten the stop, or close. It cannot widen a stop.
Python still enforces reward/risk, H1/M15 alignment, the configured stop cap, and the local guardian.
"""

import json
import logging
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
# 5 次 60 秒超时大约 300 秒。接管时间必须盖住重试，否则下一根 K 线会丢掉后面的结果。
ENTRY_TAKEOVER_SEC = env_int("FIB_PA_AI_ENTRY_TAKEOVER_SEC", 360)
AI3_TAKEOVER_SEC = env_int("FIB_PA_AI3_TAKEOVER_SEC", 220)
AI_HTTP_RETRIES = max(1, env_int("FIB_PA_AI_HTTP_RETRIES", 5))
AI_HTTP_RETRY_PAUSE = 2.0
API_ALERT_COOLDOWN = env_int("FIB_PA_AI_ALERT_COOLDOWN", 900)
AI3_STATE_FILE = os.path.join(base.LOG_DIR, "ai3_position_state.json")
AI_DECISION_SNAPSHOT_DIR = os.path.join(base.LOG_DIR, "ai_decisions")
_SNAPSHOT_LOCK = threading.RLock()
QUALITY_LEVEL_POINTS = {"strong": 20, "adequate": 16, "weak": 8, "missing": 0}
QUALITY_DIMENSIONS = ("structure", "trigger", "protection", "target", "counter_evidence")
QUALITY_CAUSES = {"volume", "shallow_retrace", "trigger_weakness", "opposing_structure", "stop_vulnerability", "target_uncertainty", "execution_drift", "missing_data", "hard_limit"}
QUALITY_VERSION = "entry-quality-v1"

base.MAGIC = AI_MAGIC
base.ENABLE_TRADING = AI_ENABLE_TRADING
SL_CAP = "%g" % base.MAX_SL_ABS

AI1_INSTRUCTIONS = (
    "你是 XAUUSD 分析师 AI-1。保留自主提出交易的能力，只根据给定结构化数据判断，不看账户，不决定手数，不执行下单。"
    "先看 session 和 setup。setup 是本地按时段选出的进场方式：pullback 是趋势回撤，range_edge 是区间边缘反转，breakout 是突破后回踩。"
    "pullback：tight_range 时 action 必须是 no；wide_range 只做边缘且要有拒绝K线。"
    "range_edge：只在 24 根 M15 区间的边缘、有拒绝K线时做，中轴必须是 no，目标看区间另一侧。"
    "breakout：必须收盘突破后回踩箱体边缘并守住；没有回踩的追突破必须是 no。量能仅加减分，普通量或弱量不能单独否决，引用Python给定的量能基线和比值。breakout 不受 tight_range 限制。"
    f"方向只看 H1。M15 负责斐波那契、成交量和价格行为。M5 不单独决定方向，但在H1方向已经允许时，结构附近已收盘的M5拒绝/吞没可以完成入场时机确认；最近3根M5的拒绝极值加缓冲后仍不超过{SL_CAP}美元时，可以用该极值保护止损。H4 不是否决条件。"
    "M15 与 H1 暂时反向是回撤，不是冲突，不能因此 no。"
    "H1 同向只表示允许的交易方向，不代表当前存在入场信号。分别检查方向、setup结构有效性、已收盘K线确认、目标前的障碍。"
    "输入中的computed_evidence和bar_time/price是Python从同一批已收盘K线计算的事实，优先引用它们；不要重算出另一套Fib、箱体、量能或影线数值。先核实事实，再依次说明在哪里、哪根已收盘K线证明触发、什么价位和周期使理由失效、目标前有什么障碍、最强反证是什么。"
    "如果事实字段互相矛盾，返回no并在fact_conflicts写明冲突；如果关键字段缺失，返回no并在unknowns写明未知，不得猜测。"
    "pullback 的旧波段已被收盘破坏时，不能只凭H1同向把破坏解释为普通回撤；只有给定数据支持新结构及新的确认才可提出交易，否则 no。单根影线刺穿后收回不等于收盘破坏。Fib核心区必须按computed_evidence给出的容差和最近3根探入确认解释，不要把收盘略低于38.2或略高于61.8自动当成结构失效。"
    "breakout 必须引用突破K和后续回踩K的bar_time。突破K自己的影线不是后续回踩；回踩已经守住后，不要因为同一次推动在M15和M5留下多个相近极值就把确认清零。原箱体边缘收盘失守、出现独立的反向结构并收盘确认，或事实明确显示形态失效时，才重新判定。"
    "障碍优先引用computed_evidence.obstacles中的price、timeframe、bar_time、source和event_group。已核实的跨周期同一极值只算同一事件；单根过程极值不能自动升级为独立重要障碍。仍需在未克服的重要障碍前有1.5RR空间，不能为了开单删除真正的障碍。"
    "止盈必须说明给定数据中的目标依据和中间阻力/支撑，不能只为凑够RR把目标延远。确认量偏弱是质量扣分，不可编造放量确认。确认尚未完成、或第一个重要障碍前没有1.5RR空间时，返回no并写明等待条件。"
    "交易质量按输入quality_rubric的五项评分，不猜胜率、不复制本地分、不为了通过门槛抬分。reasoning依次说明setup证据、失效位置、目标依据和主要反证；证据缺失明确写未知。"
    f"M5保护止损是否可用不取决于M15影线是否超过{SL_CAP}美元：只要它保护当前触发结构、覆盖最近3根M5极值和缓冲、并且距离参考入场不超过{SL_CAP}美元，就可以使用。更远的结构止损超过上限时必须no，禁止把止损改进影线里面。"
    f"H1 不支持该方向、止损超过 {SL_CAP} 美元、价格偏离 M15 EMA21 超过 1.5 ATR、止损止盈方向错误或盈亏比低于 1.5 时，action 必须是 no。"
    "stop_loss 和 take_profit 用绝对价格。最终回复只能是一个 JSON 对象，不要 Markdown；字段为action、stop_loss、take_profit、reasoning、trigger_evidence、invalidation、target_basis、fact_conflicts数组、unknowns数组、quality_assessment、wait_basis。"
)
AI2_INSTRUCTIONS = (
    "你是独立风控审核官 AI-2。不能提出新交易，只能 APPROVE 或 REJECT。"
    f"只有明确缺陷才能 REJECT：setup 为 pullback 却处在 tight_range、range_edge 却在区间中轴、breakout 没有回踩箱体就追单、止损方向错误、盈亏比低于 1.5、H1 不支持该方向、止损超过 {SL_CAP} 美元、追价超过 1.5 ATR、止损或止盈缺失、关键数据缺失、订单自相矛盾。"
    "M15 与 H1 暂时反向、轻微背离、逆 H4、波动大或感觉不确定，都不能单独 REJECT。"
    "根据market_evidence及computed_evidence独立核实，不要把AI-1的reasoning当作已核实事实。明确的setup结构失效且没有新结构确认、声称回踩/放量但K线数据反驳，也属于订单自相矛盾，应REJECT并指出对应bar_time、price和source。单根影线收回不能当作收盘破坏。"
    "核对Fib时使用Python给出的容差、recent_probe和confirmation_extension；核对M5时机时，M5可以在H1方向已经确定后提供入场确认，但不能改变方向。核对障碍时按event_group去重：同一次突破在M15/M5的过程极值不能重复算成多个独立硬障碍，真正独立的反向结构仍必须保留。"
    "仅仅本地action=no、量能普通、ADX偏弱或RR接近合格下限不能单独否决；必须区分质量保留意见与可核实的硬伤。数据不足以核实必要条件时指出缺失字段，不得补造事实。"
    "审核Python调整后的最终方案，不审核已经被替换的AI-1原始止损或止盈；如果最终目标与AI理由不一致，要求给出可核实的调整依据，否则REJECT。"
    "最终回复只能是一个 JSON 对象，不要 Markdown；字段为decision、defects数组、reasoning、fact_conflicts数组。APPROVE时defects和fact_conflicts必须为空。"
)
OBSTACLE_EVIDENCE_INSTRUCTIONS = (
    "障碍列表是候选集合，不是自动否决清单，也不穷尽所有结构。Fib端点不天然是独立强障碍；"
    "classification=unknown和strength=unassessed必须结合origin_bar及observed中的反向价格行为、收盘突破证据判断，不能直接升级成硬障碍。"
    "event_group仅标识已核实的同一根来源K线极值；相同价格但不同时间的触碰必须分别保留。"
    "event_group为空表示归属未知，不能将所有未知项合并。同组内仍需阅读每次观察的反向证据。候选障碍归属未知本身不等同于入场关键条件缺失，不能单独否决。"
    "closed_breakthrough的not_observed_in_window仅表示所给窗口未发现，不代表历史从未突破；"
    "opposing_price_action为现有本地分类器输出，不自动等同于有效反转。"
    "若引用列表外的独立结构，必须指出输入中真实存在的timeframe、bar_time、price和来源，归属不明写未知。"
)
AI1_INSTRUCTIONS += OBSTACLE_EVIDENCE_INSTRUCTIONS
AI2_INSTRUCTIONS += OBSTACLE_EVIDENCE_INSTRUCTIONS
PRICE_ACTION_INSTRUCTIONS = (
    "价格行为必须按computed_evidence.price_action中的M15/M5、方向及bar_time分别核对。"
    "本地details.price_action是M15评分及可选M5加分的汇总，不是仅指M5。M15触发而M5无触发可以同时成立，不得写入fact_conflicts。"
    "false_break使用本周期最新已收盘K与前6根极值比较：sell为high>prior_extreme且close<prior_extreme，buy镜像；"
    "这条本地规则不要求sell收阴或长上影，也不要求buy收阳或长下影。不能因K线颜色或M5零分宣称M15假突破不存在。"
    "M15或M5已确认的触发均须纳入评估，不额外要求两周期同时触发；H1方向、setup位置和风险限制仍须满足。"
    "可结合相反证据降低质量评价，但必须区分已成立的触发事实与质量意见，不得把质量疑虑写成事实冲突。"
)
AI1_INSTRUCTIONS += PRICE_ACTION_INSTRUCTIONS
AI2_INSTRUCTIONS += PRICE_ACTION_INSTRUCTIONS
AI1_INSTRUCTIONS += (
    "quality_assessment按输入quality_rubric返回direction、dimensions和reservations。每个dimension含level、reason、evidence_refs、reservation_ids。"
    "evidence_refs是相对本次输入的JSON Pointer，例如/computed_evidence/price_action/M15/sell/checks/false_break。"
    "reservations每项含id、cause、event_group、dimension、severity(minor/material/blocking)、reason、evidence_refs。"
    "同一原因和事件仅由一个dimension承担，其他维度可说明但不能重复扣分；只有minor保留意见时最低为adequate，不能选weak或missing。"
    "weak必须有对应material证据，missing必须有blocking的缺失或失效证据。Python按固定等级加总，忽略自行给出的自由confidence。"
    "action=no时仍评价允许方向的交易质量，并在wait_basis列出code、reason、evidence_refs；"
    "code为hard_limit、missing_structure、missing_trigger、missing_data、target_space或material_counter_evidence。"
    "某周期规则已命中不得以missing_trigger声称触发不存在；可以解释触发质量偏弱或不在setup位置。"
    "结构位置、已有确认、止损保护、目标空间已成立且仅有minor保留意见时，应在允许方向上形成提案交后续审核，不能无限等待更漂亮形态。"
    "方向、行情质量、未克服的重要障碍及有效的新反向结构仍由你判断，不能为了有订单删除它们。"
)
AI3_INSTRUCTIONS = (
    "你是持仓监控 AI-3。只能选择 hold、tighten_sl 或 close。"
    "禁止放宽止损，禁止加仓，禁止新开仓。tighten_sl 必须比当前止损更有利，并给出绝对价格。"
    "核实入场理由失效时必须使用该理由指定周期的已收盘K线：M15边界用m15_bars，H1边界用h1_bars，"
    "不能从M1/M5收盘推断M15/H1已收盘破坏。对应周期或时间覆盖不足时失效证据为未知，不能凭此close。"
    "窄震荡里的上下影线和轻微回撤选择 hold，交给本地虚拟锁盈。"
    "不必等价格碰到初始止损才处理，但不能仅因当前浮亏或持仓时间提前退出。只有指定失效边界被已收盘K线越过，"
    "或至少两根相邻已收盘M1朝不利方向推进、最近M5结构同步恶化且没有有利拒绝时，才可close；否则优先hold。"
    "单根影线刺穿但收盘收回不算结构失效。浮盈单可以把止损收紧到入场价以上（BUY）或以下（SELL）来锁定利润。"
    "输入的r_now和profit只是当前快照，hold_minutes只是持仓时长，均不能证明持续浮亏；没有历史序列不得声称一直亏损。用已给M1/M5收盘序列说明推进或拒绝的具体证据。"
    "未提供原始入场结构时，不得断言原始入场结构已失效；可以依据明确的近期M1/M5结构恶化提前减亏。仅持仓时间长、单次浮亏或一根反向K线不足以close。"
    "先对照保存的entry_context说明新行情是否使原入场理由失效。至少两根相邻已收盘M1朝不利方向推进且近期M5结构同步恶化，才可作为提前close的证据；指定失效边界被已收盘K线越过时可close。"
    "max_adverse为null或覆盖范围有限时，不得写成从未回撤；只用可见区间的MFE/MAE，并明确coverage。"
    "最终回复只能是一个 JSON 对象，不要 Markdown。"
)


def attach_ai_logger():
    log_dir = os.path.normcase(os.path.abspath(base.LOG_DIR))
    for existing in base.logger.handlers:
        if (isinstance(existing, base.DailyDurableFileHandler)
                and os.path.normcase(existing.log_dir) == log_dir
                and existing.prefix == "Fib_PA_AI3_"):
            return existing.baseFilename
    handler = base.DailyDurableFileHandler(base.LOG_DIR, "Fib_PA_AI3_", encoding="utf-8")
    handler.setFormatter(base.logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    base.logger.addHandler(handler)
    return handler.baseFilename

def bars_for_prompt(df, limit=8):
    if df is None or len(df) == 0:
        return []
    return [_bar_payload(row) for row in df.tail(limit).itertuples(index=False)]


def bar_times_for_prompt(df, limit):
    if df is None or len(df) == 0 or "time" not in df.columns:
        return []
    return [int(value) for value in df["time"].tail(limit).tolist()]


def fib_payload(fib):
    if fib is None:
        return None
    return {
        "direction": fib.direction,
        "swing_low": round(float(fib.swing_low), 2),
        "swing_high": round(float(fib.swing_high), 2),
        "levels": {key: round(float(value), 2) for key, value in fib.levels.items()},
    }


def _rounded_number(value, digits=2):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _bar_payload(row, role=None):
    """Return one JSON-safe candle record with an optional evidence role."""
    if row is None:
        return None
    result = {
        "time": int(getattr(row, "time", 0) or 0),
        "open": _rounded_number(getattr(row, "open", 0.0)),
        "high": _rounded_number(getattr(row, "high", 0.0)),
        "low": _rounded_number(getattr(row, "low", 0.0)),
        "close": _rounded_number(getattr(row, "close", 0.0)),
        "tick_volume": _rounded_number(getattr(row, "tick_volume", 0.0)),
    }
    if role:
        result["role"] = role
    parts = base.candle_parts({key: float(getattr(row, key)) for key in ("open", "high", "low", "close")})
    result["candle_parts"] = {key: _rounded_number(parts[key], 6) for key in ("body", "range", "upper", "lower")}
    return result


def _extreme_bar(df, column, mode="min", role=None):
    if df is None or len(df) == 0 or column not in df.columns:
        return None
    try:
        index = df[column].idxmin() if mode == "min" else df[column].idxmax()
        return _bar_payload(df.loc[index], role=role)
    except (KeyError, TypeError, ValueError):
        return None


def _closest_bar(df, column, target, role=None):
    if df is None or len(df) == 0 or column not in df.columns or target is None:
        return None
    try:
        matches = df[(df[column].astype(float) - float(target)).abs() < 1e-8]
        # Price alone cannot identify which repeated touch supplied a Fib endpoint.
        return _bar_payload(matches.iloc[0], role=role) if len(matches) == 1 else None
    except (KeyError, TypeError, ValueError):
        return None


def _fib_evidence(df_m15, fib, atr, price):
    if fib is None or not fib.levels:
        return {"available": False, "reason": "no_valid_entry_fib"}
    levels = {key: float(value) for key, value in fib.levels.items()}
    tolerance = max(0.35 * float(atr), 0.5)
    if fib.direction == "bull":
        core_low = levels["61.8"] - tolerance
        core_high = levels["38.2"] + tolerance
        deep_low = levels["78.6"] - tolerance
        probe = float(df_m15["low"].tail(3).min()) if df_m15 is not None and len(df_m15) >= 3 else None
        probe_bar = _extreme_bar(df_m15.tail(3), "low", "min", "recent_probe") if probe is not None else None
    else:
        core_low = levels["38.2"] - tolerance
        core_high = levels["61.8"] + tolerance
        deep_high = levels["78.6"] + tolerance
        probe = float(df_m15["high"].tail(3).max()) if df_m15 is not None and len(df_m15) >= 3 else None
        probe_bar = _extreme_bar(df_m15.tail(3), "high", "max", "recent_probe") if probe is not None else None
    fib_result = {
        "available": True,
        "direction": fib.direction,
        "swing_low": _rounded_number(fib.swing_low),
        "swing_high": _rounded_number(fib.swing_high),
        "swing_low_bar": _closest_bar(df_m15, "low", fib.swing_low, "fib_swing_low"),
        "swing_high_bar": _closest_bar(df_m15, "high", fib.swing_high, "fib_swing_high"),
        "levels": levels,
        "tolerance": _rounded_number(tolerance),
        "core_zone": {"low": _rounded_number(core_low), "high": _rounded_number(core_high)},
        "recent_probe": _rounded_number(probe),
        "recent_probe_bar": probe_bar,
        "reference_price": _rounded_number(price),
        "reference_in_core": bool(core_low <= price <= core_high),
    }
    if fib.direction == "bull":
        fib_result["deep_zone"] = {"low": _rounded_number(deep_low), "high": _rounded_number(core_low)}
        fib_result["probe_in_core"] = bool(probe is not None and core_low <= probe <= core_high)
        fib_result["probe_in_deep"] = bool(probe is not None and deep_low <= probe < core_low)
    else:
        fib_result["deep_zone"] = {"low": _rounded_number(core_high), "high": _rounded_number(deep_high)}
        fib_result["probe_in_core"] = bool(probe is not None and core_low <= probe <= core_high)
        fib_result["probe_in_deep"] = bool(probe is not None and core_high < probe <= deep_high)
    fib_result["confirmation_extension"] = _rounded_number(max(0.8 * float(atr), 1.2))
    return fib_result


def _breakout_evidence(df_m15, atr, action):
    """Expose the same breakout window used by base.score_breakout without rescoring it."""
    if df_m15 is None or len(df_m15) < base.BREAKOUT_BOX + 26:
        return {"available": False, "reason": "insufficient_breakout_bars"}
    work = df_m15.reset_index(drop=True)
    box = work.iloc[-(base.BREAKOUT_BOX + 4):-4]
    recent = work.iloc[-4:]
    low = float(box["low"].min())
    high = float(box["high"].max())
    edge = high if action == "buy" else low
    sign = 1 if action == "buy" else -1
    pushes = recent.iloc[:-1]
    broke = pushes[sign * (pushes["close"] - edge) > 0.1 * float(atr)]
    volume_base = float(work["tick_volume"].iloc[-24:-4].mean())
    result = {
        "available": True,
        "direction": action,
        "box_low": _rounded_number(low),
        "box_high": _rounded_number(high),
        "box_atr": _rounded_number((high - low) / float(atr)) if atr else None,
        "box_edge": _rounded_number(edge),
        "box_bar_times": bar_times_for_prompt(box, len(box)),
        "breakout_bar": None,
        "retest_bars": [],
        "retest_probe": None,
        "retest_distance_to_edge": None,
        "retest_holds": False,
        "retested": False,
        "close_holds_edge": False,
        "post_breakout_bars": [],
        "breakout_volume_baseline": _rounded_number(volume_base),
        "breakout_volume_ratio": None,
        "breakout_closed": bool(not broke.empty),
        "same_event_group": "breakout_sequence",
    }
    if broke.empty:
        return result
    first_index = int(broke.index[0])
    breakout_row = recent.loc[first_index]
    result["breakout_bar"] = _bar_payload(breakout_row, "breakout_close")
    if volume_base > 0:
        result["breakout_volume_ratio"] = _rounded_number(float(broke["tick_volume"].max()) / volume_base)
    facts = base.breakout_retest_facts(recent, broke, edge, atr, action)
    result["same_event_group"] = "breakout_%s" % result["breakout_bar"]["time"]
    result["post_breakout_bars"] = bars_for_prompt(facts["after"], len(facts["after"]))
    result["retest_bars"] = [_bar_payload(row, "edge_retest") for row in facts["touches"].itertuples(index=False)]
    result.update({key: facts[key] for key in ("retested", "close_holds_edge", "retest_holds")})
    if facts["probe"] is not None:
        probe = facts["probe"]
        result["retest_probe"] = _rounded_number(probe)
        result["retest_distance_to_edge"] = _rounded_number(abs(probe - edge))
    return result


def _range_edge_evidence(df_m15, atr, action):
    if df_m15 is None or len(df_m15) < base.RANGE_LOOKBACK:
        return {"available": False, "reason": "insufficient_range_bars"}
    window = df_m15.tail(base.RANGE_LOOKBACK)
    low = float(window["low"].min())
    high = float(window["high"].max())
    width = max(high - low, 1e-9)
    price = float(df_m15["close"].iloc[-1])
    location = (price - low) / width
    edge_location = location if action == "buy" else 1.0 - location
    return {
        "available": True,
        "direction": action,
        "low": _rounded_number(low),
        "high": _rounded_number(high),
        "width_atr": _rounded_number(width / float(atr)) if atr else None,
        "location": _rounded_number(location),
        "edge_location": _rounded_number(edge_location),
        "window_bar_times": bar_times_for_prompt(window, len(window)),
        "low_bar": _extreme_bar(window, "low", "min", "range_low"),
        "high_bar": _extreme_bar(window, "high", "max", "range_high"),
    }


def _obstacle_candidates(df_m15, df_m5, fib, breakout, price, action):
    """Candidates, not automatic vetoes; merge only a verified candle event."""
    target_side = (lambda value: value < price) if action == "sell" else (lambda value: value > price)
    candidates = []
    field = "low" if action == "sell" else "high"
    opposite = "buy" if action == "sell" else "sell"
    sign = -1 if action == "sell" else 1
    breakout_time = ((breakout or {}).get("breakout_bar") or {}).get("time")

    def event_for(value, timeframe, bar_time):
        if not bar_time:
            return None
        if timeframe == "M5" and df_m15 is not None and "time" in df_m15.columns:
            parents = df_m15[(df_m15["time"] <= bar_time) & (df_m15["time"] + 900 > bar_time)]
            if len(parents) == 1 and abs(float(parents.iloc[0][field]) - value) < 1e-8:
                # Equality and temporal containment establish the shared extremum.
                return "M15_%d_%s" % (int(parents.iloc[0]["time"]), field)
        return "%s_%d_%s" % (timeframe, int(bar_time), field)

    def add(value, source, timeframe, bar_time=None):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        if not target_side(value):
            return
        event_group = event_for(value, timeframe, bar_time)
        observation = {"source": source, "timeframe": timeframe, "bar_time": bar_time, "event_group": event_group}
        frame = df_m15 if timeframe == "M15" else df_m5
        origin = None
        broken_bars = []
        rejection = {"status": "unknown"}
        if bar_time and frame is not None and "time" in frame.columns:
            prefix = frame[frame["time"] <= bar_time]
            matching = frame[frame["time"] == bar_time]
            if len(matching) == 1:
                origin = _bar_payload(matching.iloc[0])
                later = frame[frame["time"] > bar_time]
                broken_bars = bars_for_prompt(later[sign * (later["close"] - value) > 0], len(later))
                if len(prefix) >= 8:
                    score, description = base.classify_price_action(prefix, opposite, base.calculate_atr(prefix))
                    rejection = {"status": "evaluated", "action": opposite, "score": int(score),
                                 "description": description, "bar_time": int(bar_time), "timeframe": timeframe}
        breakthrough = {"status": "observed" if broken_bars else "not_observed_in_window" if origin else "unknown",
                        "bars": broken_bars, "timeframe": timeframe}
        observation.update({"origin_bar": origin, "closed_breakthrough": breakthrough,
                            "opposing_price_action": rejection})
        for existing in candidates:
            if event_group and existing["event_group"] == event_group and abs(existing["price"] - value) < 1e-8:
                existing["observed"].append(observation)
                return
        # Only the breakout candle itself is known to be the breakout impulse.
        # Later rejection candles and old Fib points need independent assessment.
        classification = "process_extreme" if timeframe == "M15" and bar_time == breakout_time and bar_time else "unknown"
        if source == "breakout_box_edge":
            classification = "setup_boundary"
        candidates.append({
            "price": value,
            "source": source,
            "timeframe": timeframe,
            "bar_time": int(bar_time) if bar_time else None,
            "event_group": event_group,
            "classification": classification,
            "strength": "unassessed",
            "origin_bar": origin,
            "closed_breakthrough": breakthrough,
            "opposing_price_action": rejection,
            "observed": [],
        })

    if fib is not None:
        swing_value = fib.swing_low if action == "sell" else fib.swing_high
        swing_field = "low" if action == "sell" else "high"
        matches = df_m15[(df_m15[swing_field] - swing_value).abs() < 1e-8] if df_m15 is not None else []
        if len(matches):
            for row in matches.itertuples(index=False):
                add(swing_value, "fib_swing_endpoint_candidate", "M15", getattr(row, "time", None))
        else:
            add(swing_value, "fib_swing_endpoint", "M15")

    if breakout and breakout.get("available"):
        edge = breakout.get("box_edge")
        add(edge, "breakout_box_edge", "M15")
        ref = _extreme_bar(df_m15.tail(4), field, "min" if action == "sell" else "max", "post_breakout_extreme")
        if ref:
            add(ref[field], "post_breakout_extreme", "M15", ref["time"])

    if df_m15 is not None and len(df_m15):
        recent = df_m15.tail(8)
        field = "low" if action == "sell" else "high"
        mode = "min" if action == "sell" else "max"
        value = float(recent[field].min() if mode == "min" else recent[field].max())
        for row in recent[(recent[field] - value).abs() < 1e-8].itertuples(index=False):
            add(value, "recent_m15_extreme", "M15", getattr(row, "time", None))
        # Retain confirmed secondary turns, including lower highs/higher lows
        # which would disappear if only the window's absolute extreme survived.
        window = df_m15.tail(24).reset_index(drop=True)
        highs, lows = base.find_pivots(window)
        for index in (lows if action == "sell" else highs):
            row = window.iloc[index]
            add(row[field], "confirmed_m15_pivot", "M15", int(row["time"]) if "time" in row else None)

    if df_m5 is not None and len(df_m5):
        recent_m5 = df_m5.tail(3)
        field = "low" if action == "sell" else "high"
        mode = "min" if action == "sell" else "max"
        value = float(recent_m5[field].min() if mode == "min" else recent_m5[field].max())
        for row in recent_m5[(recent_m5[field] - value).abs() < 1e-8].itertuples(index=False):
            add(value, "m5_process_extreme", "M5", getattr(row, "time", None))
    return sorted(candidates, key=lambda item: abs(float(item["price"]) - float(price)))


def _price_action_evidence(df, timeframe, action, atr):
    facts = {}
    score, description = base.classify_price_action(df, action, atr, evidence=facts)
    facts.update({"timeframe": timeframe, "score": int(score), "description": description,
                  "bar_time": int(df.iloc[-1]["time"]) if df is not None and len(df) and "time" in df.columns else None,
                  "bars": bars_for_prompt(df, 8),
                  "prior_bar_times": bar_times_for_prompt(df.iloc[-7:-1], 6) if df is not None else [],
                  "source": "classify_price_action"})
    if facts.get("checks", {}).get("liquidity_sweep"):
        facts["sweep_input_bars"] = bars_for_prompt(df, len(df))
    return facts


def build_computed_entry_evidence(df_m15, df_h1, df_m5, local_result, fib, atr, price):
    """Build deterministic facts; this function never changes local scores or decisions."""
    details = local_result.details or {}
    h1_trend = base.trend_from_ema(df_h1, 50)
    candidate_actions = ["buy"] if h1_trend == "bull" else ["sell"] if h1_trend == "bear" else ["buy", "sell"]
    setup = details.get("setup", "")
    m5_atr = base.calculate_atr(df_m5)
    timing = {}
    price_action = {"M15": {}, "M5": {}}
    for action in candidate_actions:
        price_action["M15"][action] = _price_action_evidence(df_m15, "M15", action, atr)
        price_action["M5"][action] = _price_action_evidence(df_m5, "M5", action, m5_atr)
        extreme = base.recent_m5_extreme(df_m5, action)
        anchor = base.m5_rejection_anchor(df_m5, action, atr)
        buffer = base.stop_buffer(atr)
        protected_stop = None
        if extreme is not None:
            protected_stop = extreme + buffer if action == "sell" else extreme - buffer
        m5_pa = price_action["M5"][action]["score"]
        m5_pa_text = price_action["M5"][action]["description"]
        blocked, blocked_text = base.m5_blocks_entry(df_m5, action)
        timing[action] = {
            "bars": bars_for_prompt(df_m5, 3),
            "extreme": _rounded_number(extreme),
            "protected_stop": _rounded_number(protected_stop),
            "stop_buffer": _rounded_number(buffer),
            "risk_from_reference": _rounded_number(base.directional_stop_distance(action, price, protected_stop)) if protected_stop is not None else None,
            "anchor": {"entry": _rounded_number(anchor[0]), "extreme": _rounded_number(anchor[1])} if anchor else None,
            "price_action_score": int(m5_pa),
            "price_action": m5_pa_text,
            "blocked": bool(blocked),
            "blocked_reason": blocked_text,
            "event_group": "m5_confirmation_window",
        }
    breakout = _breakout_evidence(df_m15, atr, candidate_actions[0]) if setup == "breakout" else {"available": False, "reason": "setup_not_breakout"}
    range_edge = _range_edge_evidence(df_m15, atr, candidate_actions[0]) if setup == "range_edge" else {"available": False, "reason": "setup_not_range_edge"}
    obstacles = {action: _obstacle_candidates(df_m15, df_m5, fib, breakout, price, action) for action in candidate_actions}
    return {
        "contract_version": "entry-evidence-v5",
        "price_action": price_action,
        "source": "python_local_functions",
        "closed_bar_source_shift": 1,
        "candidate_actions": candidate_actions,
        "setup": setup,
        "fib": _fib_evidence(df_m15, fib, atr, price),
        "m5_timing": timing,
        "breakout": breakout,
        "range_edge": range_edge,
        "obstacles": obstacles,
        "quote_is_snapshot_only": True,
    }


def build_feature_pack(df_m15, df_h1, df_m5, local_result, quote=None, decision_id=None):
    atr = base.calculate_atr(df_m15)
    price = float(df_m15["close"].iloc[-1])
    reference_bar_time = int(df_m15["time"].iloc[-1]) if "time" in df_m15.columns else 0
    h1_trend = base.trend_from_ema(df_h1, 50)
    m15_trend = base.trend_from_ema(df_m15, 50)
    fib = base.entry_fib_structure(df_m15, h1_trend, atr)
    pack = {
        "symbol": base.SYMBOL,
        "timeframe": "M15",
        "direction_timeframe": "H1",
        "price": round(price, 2),
        "decision_id": decision_id,
        "bars_are_closed": True,
        "bar_closure": {
            "source_shift": 1,
            "m15_last_closed_bar_time": reference_bar_time,
            "m5_last_closed_bar_time": int(df_m5["time"].iloc[-1]) if "time" in df_m5.columns else 0,
            "h1_last_closed_bar_time": int(df_h1["time"].iloc[-1]) if "time" in df_h1.columns else 0,
            "live_quote_is_not_a_bar": True,
        },
        "reference_price": {
            "price": round(price, 2),
            "source": "previous_closed_m15_close",
            "bar_time": reference_bar_time,
        },
        "live_quote": quote or {"bid": None, "ask": None, "time": None},
        "atr": round(atr, 2),
        "h1_trend": h1_trend,
        "m15_trend": m15_trend,
        "m15_ema21_distance_atr": round(base.ema_distance_atr(df_m15, atr, 21), 2),
        "regime": (local_result.details or {}).get("regime", ""),
        "regime_text": (local_result.details or {}).get("regime_text", ""),
        "session": (local_result.details or {}).get("session", ""),
        "setup": (local_result.details or {}).get("setup", ""),
        "momentum": (local_result.details or {}).get("momentum", ""),
        "fib": fib_payload(fib),
        "local_action": local_result.action,
        "local_confidence": local_result.confidence,
        "local_stop_loss": local_result.stop_loss,
        "local_take_profit": local_result.take_profit,
        "score_buy": local_result.score_buy,
        "score_sell": local_result.score_sell,
        "local_reasoning": local_result.reasoning,
        "details": local_result.details,
        "computed_evidence": build_computed_entry_evidence(
            df_m15, df_h1, df_m5, local_result, fib, atr, price
        ),
        "quality_rubric": quality_rubric(),
        "rules": {
            "h1_sets_direction": True,
            "m15_opposite_is_pullback": True,
            "h4_is_not_a_veto": True,
            "pullback_tight_range_is_no": True,
            "pullback_wide_range_only_at_edges": True,
            "breakout_and_range_edge_use_own_conditions": True,
            "max_stop_abs": base.MAX_SL_ABS,
            "chase_max_atr": base.CHASE_MAX_ATR,
            "min_rr": base.MIN_REWARD_RISK,
        },
        "m15_bars": bars_for_prompt(df_m15, 32),
        "evidence_windows": {
            "m15_structure_lookback": 24,
            "m15_structure_bar_times": bar_times_for_prompt(df_m15, 24),
            "breakout_box_lookback": 16,
            "breakout_recent_bars": 4,
            "breakout_bar_times": bar_times_for_prompt(df_m15, 20),
            "m5_rejection_lookback": 3,
            "m5_rejection_bar_times": bar_times_for_prompt(df_m5, 3),
        },
        "h1_bars": bars_for_prompt(df_h1, 8),
        "m5_bars": bars_for_prompt(df_m5, 8),
    }
    plan_entry = float((local_result.details or {}).get("entry_price") or price)
    pack["local_plan_feasibility"] = plan_feasibility(local_result.action, plan_entry, local_result.stop_loss, local_result.take_profit, pack["live_quote"])
    return pack, fib, atr, price


def plan_feasibility(action, reference_entry, stop_loss, take_profit, quote):
    """Snapshot arithmetic for one plan, never a substitute for order-time checks."""
    result = {"action": action, "reference_entry": reference_entry, "stop_loss": stop_loss,
              "take_profit": take_profit, "quote_time": quote.get("time"), "available": False}
    if action not in ("buy", "sell") or not prices_are_valid(action, reference_entry, stop_loss, take_profit):
        return result
    live_entry = quote.get("ask" if action == "buy" else "bid")
    try:
        live_entry = float(live_entry)
    except (TypeError, ValueError):
        return result
    if not base.math.isfinite(live_entry) or live_entry <= 0:
        return result
    sign = 1 if action == "buy" else -1
    risk = sign * (live_entry - stop_loss)
    reward = sign * (take_profit - live_entry)
    result.update({"available": True, "live_entry": live_entry, "live_risk": round(risk, 6),
                   "live_reward": round(reward, 6), "live_rr": round(reward / risk, 6) if risk > 0 else None,
                   "stop_within_cap": bool(0 < risk <= base.MAX_SL_ABS),
                   "rr_passes": bool(risk > 0 and reward >= base.MIN_REWARD_RISK * risk)})
    return result


def quality_rubric():
    """A transparent ordinal scale, not a calibrated probability of profit."""
    return {
        "version": QUALITY_VERSION,
        "meaning": "entry_quality_not_win_probability",
        "level_points": QUALITY_LEVEL_POINTS,
        "dimensions": {
            "structure": "评价本时段setup的位置及有效性。符合现有Fib容差/探入或箱体/区间条件为adequate；浅回撤本身不是结构缺失。",
            "trigger": "本地M15或M5已收盘规则命中可为adequate；更明确的独立确认可为strong，不强制双周期。颜色、弱量不否定已命中的false_break。",
            "protection": "止损保护命名结构且覆盖最近3根M5极值和缓冲、距离不超过上限为adequate；不能只因相对ATR较紧就认定无保护。",
            "target": "目标有给定依据、重要未克服障碍之前有空间且有效报价RR至少1.5为adequate；刚过1.5无需在其他项重复扣分。",
            "counter_evidence": "没有可核实反向证据为strong；普通量、浅回撤、H1旧影线、未证实过程极值等minor意见为adequate；独立及时的反向结构为material。",
        },
        "reservations_policy": "一个cause/event仅归一个dimension；minor最多使strong变adequate。weak需material，missing需blocking。已在目标或触发项计入的风险不在反向证据项重复扣分。",
        "reservation_causes": sorted(QUALITY_CAUSES),
        "proposal_policy": "硬伤或必要证据缺失则no；已成立的候选仅有minor意见时给出提案，最终分数及Python/AI2决定是否放行。不要将不确定未来获利当成必要字段缺失。",
        "quote_policy": "reference_price与live_quote来自不同时间，差异本身不是事实冲突；用live_quote核实当前可行性，AI2/Python还会复核执行。",
    }


def _evidence_pointer_exists(pack, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False
    value = pack
    try:
        for token in pointer[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            value = value[int(token)] if isinstance(value, list) else value[token]
        return value is not None
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def normalize_ai1_quality(payload, pack):
    """Validate the AI scorecard without promoting its action or inventing facts."""
    if not isinstance(payload, dict):
        return payload, "AI-1质量评估缺失: 无有效JSON"
    card = payload.get("quality_assessment")
    if not isinstance(card, dict):
        return payload, "AI-1质量评估缺失: quality_assessment"
    direction = card.get("direction")
    action = str(payload.get("action", "no")).lower()
    if direction not in ("buy", "sell") or action not in ("buy", "sell", "no") or (action != "no" and action != direction):
        return payload, "AI-1质量评估无效: 方向不一致"
    dimensions = card.get("dimensions")
    reservations = card.get("reservations")
    if not isinstance(dimensions, dict) or set(dimensions) != set(QUALITY_DIMENSIONS) or not isinstance(reservations, list):
        return payload, "AI-1质量评估无效: 五项或reservations缺失"

    def refs_valid(item):
        refs = item.get("evidence_refs")
        return isinstance(refs, list) and bool(refs) and all(_evidence_pointer_exists(pack, ref) for ref in refs)

    issues = {}
    causes = set()
    for issue in reservations:
        if not isinstance(issue, dict):
            return payload, "AI-1质量评估无效: reservation格式"
        identifier = issue.get("id")
        cause = issue.get("cause")
        event = issue.get("event_group")
        if not isinstance(identifier, str) or not identifier or identifier in issues or not isinstance(cause, str) or cause not in QUALITY_CAUSES:
            return payload, "AI-1质量评估无效: 原因标识缺失或重复"
        if event is not None and not isinstance(event, str):
            return payload, "AI-1质量评估无效: event_group格式"
        if issue.get("dimension") not in QUALITY_DIMENSIONS or issue.get("severity") not in ("minor", "material", "blocking") or not isinstance(issue.get("reason"), str) or not issue["reason"].strip() or not refs_valid(issue):
            return payload, "AI-1质量评估无效: 原因缺少维度/程度/真实证据"
        key = (cause, event if event else tuple(sorted(issue["evidence_refs"])))
        if key in causes:
            return payload, "AI-1质量评估重复扣分: 同一原因和事件"
        causes.add(key)
        if action != "no" and issue["severity"] == "blocking":
            return payload, "AI-1质量评估矛盾: 声明阻断条件却提出交易"
        issues[identifier] = issue
    score = 0
    used = set()
    for name in QUALITY_DIMENSIONS:
        item = dimensions[name]
        if not isinstance(item, dict) or not isinstance(item.get("level"), str) or item["level"] not in QUALITY_LEVEL_POINTS or not isinstance(item.get("reason"), str) or not item["reason"].strip() or not refs_valid(item):
            return payload, "AI-1质量评估无效: %s缺少等级/理由/真实证据" % name
        identifiers = item.get("reservation_ids")
        if not isinstance(identifiers, list) or any(not isinstance(i, str) for i in identifiers):
            return payload, "AI-1质量评估无效: %s原因引用" % name
        for identifier in identifiers:
            if identifier in used or identifier not in issues or issues[identifier]["dimension"] != name:
                return payload, "AI-1质量评估重复或错误扣分: " + identifier
            used.add(identifier)
        severities = {issues[i]["severity"] for i in identifiers}
        level = item["level"]
        if (level == "strong" and severities
                or level == "adequate" and severities.intersection({"material", "blocking"})
                or level == "weak" and not severities.intersection({"material", "blocking"})
                or level == "missing" and "blocking" not in severities):
            return payload, "AI-1质量评估程度不符: " + name
        if name == "trigger" and level == "missing":
            facts = pack.get("computed_evidence", {}).get("price_action", {})
            if any(facts.get(tf, {}).get(direction, {}).get("score", 0) > 0 for tf in ("M15", "M5")):
                return payload, "AI-1事实误读: 本周期已有触发，不能评为不存在"
        score += QUALITY_LEVEL_POINTS[level]
    if used != set(issues):
        return payload, "AI-1质量评估无效: 未归属原因"
    waits = payload.get("wait_basis")
    if action == "no":
        if not isinstance(waits, list) or not waits:
            return payload, "AI-1观望依据缺失: wait_basis"
        for wait in waits:
            if not isinstance(wait, dict) or wait.get("code") not in ("hard_limit", "missing_structure", "missing_trigger", "missing_data", "target_space", "material_counter_evidence") or not isinstance(wait.get("reason"), str) or not wait["reason"].strip() or not refs_valid(wait):
                return payload, "AI-1观望依据无效: 缺少可核实条件"
            if wait["code"] == "material_counter_evidence" and not any(issue["severity"] in ("material", "blocking") for issue in issues.values()):
                return payload, "AI-1观望依据矛盾: 仅有轻微保留意见"
            if wait["code"] == "missing_trigger":
                facts = pack.get("computed_evidence", {}).get("price_action", {})
                if any(facts.get(tf, {}).get(direction, {}).get("score", 0) > 0 for tf in ("M15", "M5")):
                    return payload, "AI-1事实误读: 已命中规则，不能以缺少触发观望"
    normalized = dict(payload)
    normalized["model_reported_confidence"] = payload.get("confidence")
    normalized["confidence"] = score
    normalized["quality_score"] = score
    normalized["quality_version"] = QUALITY_VERSION
    return normalized, ""
def normalize_confidence(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if 0 < number <= 1:
        number *= 100
    return int(max(0, min(100, round(number))))


def format_ai_action(payload):
    """Make the raw AI-1 action explicit in logs and notifications."""
    if not isinstance(payload, dict):
        return "未知"
    action = str(payload.get("action", "")).strip().lower()
    if action == "buy":
        return "BUY"
    if action == "sell":
        return "SELL"
    if action == "no":
        return "观望"
    return "无效"


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

class AIRequestError(RuntimeError):
    def __init__(self, role, detail):
        self.role = role
        self.detail = detail
        super().__init__("%s: %s" % (role, detail))


def call_chat(role, model, effort, instructions, prompt, timeout, url, api_key, still_current=None):
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

    def task_alive():
        return still_current is None or still_current()

    def post_with_retry(data, label):
        last_exc = None
        for attempt in range(1, AI_HTTP_RETRIES + 1):
            if not task_alive():
                base.logger.info(
                    "%s 任务已过期或已停止，停止重试 (%s %s/%s)"
                    % (role, label, attempt, AI_HTTP_RETRIES)
                )
                return None
            try:
                response = requests.post(url, headers=headers, json=data, timeout=timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                base.logger.warning(
                    "%s 请求超时或连接失败 (%s/%s, %s): %s: %s"
                    % (role, attempt, AI_HTTP_RETRIES, label, type(exc).__name__, exc)
                )
                if attempt >= AI_HTTP_RETRIES:
                    break
                time.sleep(AI_HTTP_RETRY_PAUSE)
                continue
            if not task_alive():
                base.logger.info("%s 请求返回时任务已过期，丢弃 (%s)" % (role, label))
                return None
            if attempt > 1:
                base.logger.info("%s 第 %s/%s 次请求成功 (%s)" % (role, attempt, AI_HTTP_RETRIES, label))
            return response
        detail = "连续 %s 次超时或连接失败 (%s): %s: %s" % (
            AI_HTTP_RETRIES,
            label,
            type(last_exc).__name__,
            last_exc,
        )
        base.logger.error("%s %s" % (role, detail[:300]))
        raise AIRequestError(role, detail[:300])

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
        response = post_with_retry(data, label)
        if response is None:
            return ""
        if response.status_code == 400 and "reasoning" in response.text.lower():
            data.pop("reasoning", None)
            base.logger.warning("%s 拒绝 reasoning 参数，去掉后重试 (%s)" % (role, label))
            response = post_with_retry(data, label + "/no_reasoning")
            if response is None:
                return ""
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
def finalize_ai_proposal(payload, entry, atr, h1_trend, m15_trend, fib, min_confidence=None, df_m15=None, df_m5=None, regime=None, regime_location=None, pa_score=0, local_stop=0.0, local_target=0.0, setup="pullback"):
    if min_confidence is None:
        min_confidence = AI_CONFIDENCE
    if not isinstance(payload, dict):
        return None, "AI-1 没有返回有效 JSON"
    action = str(payload.get("action", "no")).strip().lower()
    if action not in ("buy", "sell", "no"):
        action = "no"
    confidence = normalize_confidence(payload.get("confidence", 0))
    reasoning = str(payload.get("reasoning", "")).strip()
    fact_conflicts = payload.get("fact_conflicts") or []
    if not isinstance(fact_conflicts, list):
        fact_conflicts = [str(fact_conflicts)]
    fact_conflicts = [str(item).strip() for item in fact_conflicts if str(item).strip()]
    unknowns = payload.get("unknowns") or []
    if not isinstance(unknowns, list):
        unknowns = [str(unknowns)]
    unknowns = [str(item).strip() for item in unknowns if str(item).strip()]
    if action == "no":
        return None, reasoning or "AI-1 选择观望"
    if fact_conflicts:
        return None, "AI-1事实冲突，拒绝猜测: " + "；".join(fact_conflicts[:4])
    required_evidence = {
        "trigger_evidence": payload.get("trigger_evidence"),
        "invalidation": payload.get("invalidation"),
        "target_basis": payload.get("target_basis"),
    }
    missing_evidence = [name for name, value in required_evidence.items() if not str(value or "").strip()]
    if missing_evidence:
        return None, "AI-1证据字段缺失: " + ",".join(missing_evidence)
    if confidence < min_confidence:
        return None, "AI-1 置信度 %s < %s" % (confidence, min_confidence)
    if setup == "pullback" and regime == "tight_range":
        return None, "Python拒绝: 窄震荡或方向不清，不交易"
    if setup == "pullback" and regime == "wide_range" and regime_location is not None:
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

    entry_price_source = "previous_closed_m15_close"
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
            entry_price_source = "m5_rejection_fallback_close"
            stop_loss, take_profit = base.calculate_targets(action, entry, atr, fib, setup_price)
            if not prices_are_valid(action, entry, stop_loss, take_profit):
                return None, "Python拒绝: 结构止损超过小资金上限 %.2f" % base.MAX_SL_ABS
            reasoning = (reasoning + "；AI止损放不下，改用仍在%g美元内的M5极值" % base.MAX_SL_ABS).strip("；")

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
        details={
            "source": "ai1",
            "h1_trend": h1_trend,
            "m15_trend": m15_trend,
            "entry_price": entry,
            "trigger_evidence": required_evidence["trigger_evidence"],
            "invalidation": required_evidence["invalidation"],
            "target_basis": required_evidence["target_basis"],
            "fact_conflicts": fact_conflicts,
            "unknowns": unknowns,
            "entry_price_source": entry_price_source,
            "quality_score": payload.get("quality_score"),
            "quality_version": payload.get("quality_version"),
            "quality_assessment": payload.get("quality_assessment"),
        },
    )
    return result, full_reason
def ai2_allows(payload):
    if not isinstance(payload, dict):
        return False, "AI-2 无有效 JSON，拒绝开仓"
    decision = str(payload.get("decision", "")).strip().upper()
    defects = payload.get("defects") or []
    if not isinstance(defects, list):
        defects = [str(defects)]
    fact_conflicts = payload.get("fact_conflicts") or []
    if not isinstance(fact_conflicts, list):
        fact_conflicts = [str(fact_conflicts)]
    fact_conflicts = [str(item).strip() for item in fact_conflicts if str(item).strip()]
    reasoning = str(payload.get("reasoning", "")).strip()
    if decision != "APPROVE":
        defect_text = "，".join(str(item) for item in defects) if defects else "未列明"
        return False, "AI-2 拒绝: %s，缺陷=%s。%s" % (decision or "EMPTY", defect_text, reasoning)
    if defects or fact_conflicts:
        defect_text = "，".join(str(item) for item in defects + fact_conflicts)
        return False, "AI-2拒绝: APPROVE同时列出缺陷或事实冲突: %s" % (defect_text or "未列明")
    if not reasoning:
        return False, "AI-2拒绝: 审核理由缺失"
    return True, reasoning


def task_is_current(seq, bar_time, current_seq, current_bar):
    return seq == current_seq and bar_time == current_bar


def prompt_json(title, payload):
    return title + "\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def append_decision_snapshot(decision_id, stage, payload):
    """Persist redacted decision evidence without ever writing credentials."""
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "decision_id": str(decision_id),
        "stage": str(stage),
        "payload": payload,
    }
    try:
        os.makedirs(AI_DECISION_SNAPSHOT_DIR, exist_ok=True)
        path = os.path.join(AI_DECISION_SNAPSHOT_DIR, "%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        with _SNAPSHOT_LOCK:
            with open(path, "a", encoding="utf-8") as snapshot_file:
                snapshot_file.write(line + "\n")
                snapshot_file.flush()
    except (OSError, TypeError, ValueError) as exc:
        base.logger.warning("决策证据快照保存失败 stage=%s: %s: %s", stage, type(exc).__name__, exc)


class ThreeAITrader(base.FibVolumePATrader):
    def __init__(self):
        super().__init__()
        self._mt5_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self.entry_contexts = {}
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
        self.load_entry_contexts()

    def load_entry_contexts(self):
        with self._state_lock:
            self.entry_contexts = {}
            try:
                with open(AI3_STATE_FILE, "r", encoding="utf-8") as state_file:
                    data = json.load(state_file)
            except FileNotFoundError:
                return
            except (OSError, ValueError) as exc:
                base.logger.warning("AI-3 持仓上下文读取失败，历史字段标记未知: %s: %s", type(exc).__name__, exc)
                return
            if not isinstance(data, dict):
                base.logger.warning("AI-3 持仓上下文格式无效，历史字段标记未知")
                return
            contexts = data.get("positions", data)
            if isinstance(contexts, dict):
                self.entry_contexts = {
                    str(ticket): value
                    for ticket, value in contexts.items()
                    if isinstance(value, dict)
                }

    def save_entry_contexts(self):
        with self._state_lock:
            os.makedirs(base.LOG_DIR, exist_ok=True)
            temp_path = AI3_STATE_FILE + ".tmp"
            payload = {"version": 1, "positions": self.entry_contexts}
            try:
                with open(temp_path, "w", encoding="utf-8") as state_file:
                    json.dump(payload, state_file, ensure_ascii=False, indent=2, sort_keys=True)
                    state_file.flush()
                    os.fsync(state_file.fileno())
                os.replace(temp_path, AI3_STATE_FILE)
            except OSError as exc:
                base.logger.warning("AI-3 持仓上下文保存失败: %s: %s", type(exc).__name__, exc)
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass

    def reconcile_entry_contexts(self):
        positions = self.get_positions()
        active = {str(int(position.ticket)): position for position in positions}
        changed = False
        with self._state_lock:
            for ticket in list(self.entry_contexts):
                if ticket not in active:
                    self.entry_contexts.pop(ticket, None)
                    changed = True
            for ticket, position in active.items():
                if ticket in self.entry_contexts:
                    continue
                self.entry_contexts[ticket] = {
                    "status": "unknown_after_restart",
                    "ticket": int(position.ticket),
                    "identifier": int(getattr(position, "identifier", 0) or 0),
                    "symbol": getattr(position, "symbol", base.SYMBOL),
                    "direction": "BUY" if position.type == base.mt5.POSITION_TYPE_BUY else "SELL",
                    "entry_price": float(position.price_open),
                    "initial_sl": float(position.sl or 0.0),
                    "initial_tp": float(position.tp or 0.0),
                    "initial_risk": None,
                    "source": "unknown",
                    "decision_id": None,
                    "setup": None,
                    "session": None,
                    "entry_reasoning": None,
                    "trigger_evidence": None,
                    "invalidation": None,
                    "target_basis": None,
                    "fact_conflicts": None,
                    "unknowns": None,
                    "entry_price_source": None,
                    "coverage": "since_restart",
                    "coverage_started_at": time.time(),
                }
                changed = True
        if changed:
            self.save_entry_contexts()

    def record_entry_context(self, result, position):
        ticket = int(getattr(position, "ticket", 0) or 0)
        if ticket <= 0:
            return
        details = dict(result.details or {})
        entry_price = float(getattr(position, "price_open", 0.0) or details.get("entry_price", 0.0) or 0.0)
        initial_sl = float(getattr(position, "sl", 0.0) or result.stop_loss or 0.0)
        initial_tp = float(getattr(position, "tp", 0.0) or result.take_profit or 0.0)
        risk = abs(entry_price - initial_sl) if initial_sl > 0 else None
        context = {
            "status": "complete",
            "ticket": ticket,
            "identifier": int(getattr(position, "identifier", 0) or 0),
            "symbol": getattr(position, "symbol", base.SYMBOL),
            "direction": "BUY" if position.type == base.mt5.POSITION_TYPE_BUY else "SELL",
            "entry_price": entry_price,
            "initial_sl": initial_sl,
            "initial_tp": initial_tp,
            "final_stop_loss": initial_sl,
            "final_take_profit": initial_tp,
            "initial_risk": risk,
            "source": details.get("source", "ai1"),
            "decision_id": details.get("decision_id"),
            "setup": details.get("setup"),
            "session": details.get("session"),
            "entry_reasoning": result.reasoning,
            "local_action_before_ai": details.get("local_action_before_ai"),
            "local_confidence": details.get("local_confidence"),
            "ai_confidence": result.confidence,
            "quality_score": details.get("quality_score"),
            "quality_version": details.get("quality_version"),
            "quality_assessment": details.get("quality_assessment"),
            "trigger_evidence": details.get("trigger_evidence"),
            "invalidation": details.get("invalidation"),
            "target_basis": details.get("target_basis"),
            "fact_conflicts": details.get("fact_conflicts", []),
            "unknowns": details.get("unknowns", []),
            "entry_price_source": details.get("entry_price_source"),
            "reference_entry_price": details.get("reference_entry_price", details.get("entry_price")),
            "reference_price_source": details.get("reference_price_source"),
            "coverage": "from_entry",
            "coverage_started_at": time.time(),
            "last_observed_at": time.time(),
        }
        with self._state_lock:
            self.entry_contexts[str(ticket)] = context
        self.save_entry_contexts()

    def remove_entry_context(self, ticket):
        with self._state_lock:
            removed = self.entry_contexts.pop(str(int(ticket)), None)
        if removed is not None:
            self.save_entry_contexts()

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
            placed = super().place_order(result)
            if not placed:
                return False
            positions = self.get_positions()
            ticket = int(getattr(self, "last_order_ticket", 0) or 0)
            position = next(
                (item for item in positions if int(getattr(item, "ticket", 0) or 0) == ticket),
                None,
            )
            if position is None and positions:
                position = max(positions, key=lambda item: int(getattr(item, "time", 0) or 0))
            if position is None:
                base.logger.warning("开仓成功但暂未找到持仓，AI-3上下文将在下次启动/轮询标记未知")
            else:
                self.record_entry_context(result, position)
            return True

    def modify_position_sl(self, position, new_sl, reason):
        with self._mt5_lock:
            return super().modify_position_sl(position, new_sl, reason)

    def close_position_by_ticket(self, position, reason):
        with self._mt5_lock:
            ticket = int(getattr(position, "ticket", 0) or 0)
            closed = super().close_position_by_ticket(position, reason)
            if closed:
                self.remove_entry_context(ticket)
            return closed

    def sync_position_close_notifications(self):
        super().sync_position_close_notifications()
        active = {str(int(item.ticket)) for item in self.get_positions()}
        with self._state_lock:
            stale = [ticket for ticket in self.entry_contexts if ticket not in active]
            for ticket in stale:
                self.entry_contexts.pop(ticket, None)
        if stale:
            self.save_entry_contexts()

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
            reject = ""
            if "拒绝:" in (local_result.reasoning or ""):
                reject = " 拒绝:" + local_result.reasoning.split("拒绝:", 1)[1].strip().replace("\n", " ")[:160]
            base.logger.info(
                "M5时机未到 action=%s confidence=%s BUY=%s SELL=%s%s"
                % (
                    local_result.action,
                    local_result.confidence,
                    local_result.score_buy,
                    local_result.score_sell,
                    reject,
                )
            )
            return
        base.logger.info(
            "M5拒绝仍在%g美元内，派发开仓 action=%s score=%s SL=%s"
            % (base.MAX_SL_ABS, local_result.action, local_result.confidence, local_result.stop_loss)
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
            decision_id = "%s-%s" % (bar_time, seq)
            with self._mt5_lock:
                tick = base.mt5.symbol_info_tick(base.SYMBOL)
            quote = {
                "bid": round(float(tick.bid), 2) if tick else None,
                "ask": round(float(tick.ask), 2) if tick else None,
                "time": int(getattr(tick, "time", 0) or 0) if tick else None,
            }
            pack, fib, atr, price = build_feature_pack(
                df_m15, df_h1, df_m5, local_result, quote=quote, decision_id=decision_id
            )
            append_decision_snapshot(
                decision_id,
                "ai1_input",
                {
                    "market_evidence": pack,
                    "local_result": local_result.to_decision(),
                },
            )
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
                    local_result.details.update(
                        {
                            "source": "local",
                            "decision_id": decision_id,
                            "local_action_before_ai": local_result.action,
                            "local_confidence": local_result.confidence,
                        }
                    )
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
                    "请只输出JSON，含action、stop_loss、take_profit、reasoning、trigger_evidence、invalidation、target_basis、fact_conflicts、unknowns；quality_assessment={direction,dimensions:{structure,trigger,protection,target,counter_evidence},reservations:[]}，各维度={level,reason,evidence_refs:[],reservation_ids:[]}；wait_basis=[]（no必须列明等待依据）。由Python汇总质量分，不输出自由confidence。",
                    pack,
                ),
                AI1_TIMEOUT,
                AI1_URL,
                AI1_KEY,
                still_current=lambda: self.entry_is_current(seq, bar_time) and not self._stop.is_set(),
            )
            if not self.entry_is_current(seq, bar_time) or self._stop.is_set():
                base.logger.info("AI-1 返回已过期，丢弃 seq=%s" % seq)
                return
            if not raw:
                append_decision_snapshot(decision_id, "ai1_empty", {"reason": "no_visible_json"})
                self.alert_api_failure("AI-1", "没有可见 JSON")
                return
            entry_price = float((local_result.details or {}).get("entry_price") or price)
            parsed_raw = extract_json_object(raw)
            parsed, quality_error = normalize_ai1_quality(parsed_raw, pack)
            proposal, reason = None, quality_error
            if not quality_error:
                proposal, reason = finalize_ai_proposal(
                    parsed, entry_price, atr, pack["h1_trend"], pack["m15_trend"], fib,
                    df_m15=df_m15, df_m5=df_m5, regime=pack.get("regime"),
                    regime_location=(local_result.details or {}).get("location"),
                    pa_score=int((local_result.details or {}).get("pa_score") or 0),
                    local_stop=local_result.stop_loss, local_target=local_result.take_profit,
                    setup=(local_result.details or {}).get("setup", "pullback"),
                )
            ai_confidence = None
            if isinstance(parsed, dict) and not quality_error:
                ai_confidence = normalize_confidence(parsed.get("confidence", 0))
            append_decision_snapshot(
                decision_id,
                "ai1_result",
                {
                    "raw": raw,
                    "parsed": parsed,
                    "quality_error": quality_error,
                    "proposal": proposal.to_decision() if proposal is not None else None,
                    "python_reason": reason,
                },
            )
            action_line = "AI-1动作: %s" % format_ai_action(parsed)
            confidence_line = "%s，本地置信度: %s (BUY=%s SELL=%s)，AI-1质量分: %s，门槛: 本地%s / AI%s" % (
                action_line,
                local_result.confidence,
                local_result.score_buy,
                local_result.score_sell,
                "无" if ai_confidence is None else ai_confidence,
                base.CONFIDENCE_THRESHOLD,
                AI_CONFIDENCE,
            )
            base.logger.info(
                "AI-1 结果 decision=%s local_action=%s %s: %s | %s"
                % (decision_id, local_result.action, action_line, reason, confidence_line)
            )
            if not quality_error:
                card = parsed["quality_assessment"]
                base.logger.info("AI-1质量明细 decision=%s version=%s grades=%s reservations=%s wait_basis=%s",
                                 decision_id, QUALITY_VERSION,
                                 {key: item["level"] for key, item in card["dimensions"].items()},
                                 card["reservations"], parsed.get("wait_basis", []))
            if proposal is None:
                if "观望" not in reason and "未给出" not in reason:
                    self.notify_lines(
                        "✋ %s AI-1/Python 未放行" % base.SYMBOL,
                        [confidence_line, reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                    )
                return
            proposal.score_buy = local_result.score_buy
            proposal.score_sell = local_result.score_sell
            requested_sl = parsed.get("stop_loss") if isinstance(parsed, dict) else None
            requested_tp = parsed.get("take_profit") if isinstance(parsed, dict) else None
            proposal.details.update(
                {
                    "source": "ai1",
                    "decision_id": decision_id,
                    "local_action_before_ai": local_result.action,
                    "local_confidence": local_result.confidence,
                    "session": pack.get("session"),
                    "setup": pack.get("setup"),
                    "reference_price_source": pack["reference_price"].get("source"),
                    "reference_entry_price": entry_price,
                    "trigger_evidence": parsed.get("trigger_evidence") if isinstance(parsed, dict) else None,
                    "invalidation": parsed.get("invalidation") if isinstance(parsed, dict) else None,
                    "target_basis": parsed.get("target_basis") if isinstance(parsed, dict) else None,
                    "fact_conflicts": parsed.get("fact_conflicts", []) if isinstance(parsed, dict) else [],
                    "unknowns": parsed.get("unknowns", []) if isinstance(parsed, dict) else [],
                    "entry_price_source": proposal.details.get("entry_price_source", "previous_closed_m15_close"),
                    "computed_evidence_contract": pack.get("computed_evidence", {}).get("contract_version"),
                    "ai_requested_stop_loss": requested_sl,
                    "ai_requested_take_profit": requested_tp,
                    "final_stop_loss": proposal.stop_loss,
                    "final_take_profit": proposal.take_profit,
                    "python_finalization": reason,
                }
            )
            final_entry_price = float(proposal.details.get("entry_price") or entry_price)
            review_pack = {
                "proposal": proposal.to_decision(),
                "ai1_raw_proposal": parsed,
                "python_finalization": {
                    "reference_entry_price": entry_price,
                    "final_entry_price": final_entry_price,
                    "final_stop_loss": proposal.stop_loss,
                    "final_take_profit": proposal.take_profit,
                    "reason": reason,
                },
                "decision_id": decision_id,
                "reference_entry_price": entry_price,
                "entry_price": final_entry_price,
                "final_plan_feasibility": plan_feasibility(proposal.action, final_entry_price,
                                                          proposal.stop_loss, proposal.take_profit, pack["live_quote"]),
                "atr": pack["atr"],
                "h1_trend": pack["h1_trend"],
                "m15_trend": pack["m15_trend"],
                "regime": pack.get("regime"),
                "regime_text": pack.get("regime_text"),
                "session": pack.get("session"),
                "setup": pack.get("setup"),
                "max_stop_abs": base.MAX_SL_ABS,
                "min_rr": base.MIN_REWARD_RISK,
                "local_action": local_result.action,
                "local_reasoning": local_result.reasoning,
                "market_evidence": pack,
            }
            review_raw = call_chat(
                "AI-2",
                AI2_MODEL,
                AI2_EFFORT,
                AI2_INSTRUCTIONS,
                prompt_json(
                    "请只输出 JSON: {\"decision\":\"APPROVE|REJECT\",\"defects\":[],\"reasoning\":\"...\",\"fact_conflicts\":[]}",
                    review_pack,
                ),
                AI2_TIMEOUT,
                AI2_URL,
                AI2_KEY,
                still_current=lambda: self.entry_is_current(seq, bar_time) and not self._stop.is_set(),
            )
            if not self.entry_is_current(seq, bar_time) or self._stop.is_set():
                base.logger.info("AI-2 返回已过期，丢弃 seq=%s" % seq)
                return
            if not review_raw:
                append_decision_snapshot(decision_id, "ai2_empty", {"proposal": proposal.to_decision()})
                self.alert_api_failure("AI-2", "没有可见 JSON，拒绝开仓")
                return
            review_payload = extract_json_object(review_raw)
            allowed, review_reason = ai2_allows(review_payload)
            append_decision_snapshot(
                decision_id,
                "ai2_result",
                {
                    "raw": review_raw,
                    "parsed": review_payload,
                    "allowed": allowed,
                    "reason": review_reason,
                    "proposal": proposal.to_decision(),
                },
            )
            base.logger.info("AI-2 结果 decision=%s: %s" % (decision_id, review_reason))
            if not allowed:
                self.notify_lines(
                    "✋ %s AI-2 未通过" % base.SYMBOL,
                    [
                        "方向: %s" % proposal.action.upper(),
                        confidence_line,
                        "止损: %s" % proposal.stop_loss,
                        "止盈: %s" % proposal.take_profit,
                        review_reason,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ],
                )
                return
            proposal.reasoning = "%s。%s" % (proposal.reasoning, review_reason)
            proposal.details["ai2_review"] = review_reason
            placed = self.place_order(proposal)
            append_decision_snapshot(
                decision_id,
                "execution_result",
                {
                    "placed": bool(placed),
                    "proposal": proposal.to_decision(),
                },
            )
        except AIRequestError as exc:
            self.alert_api_failure(exc.role, exc.detail)
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
            df_m15 = self.get_rates(base.mt5.TIMEFRAME_M15, 40, shift=1)
            df_h1 = self.get_rates(base.mt5.TIMEFRAME_H1, 40, shift=1)
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
                "m15_bars": bars_for_prompt(df_m15, 16),
                "h1_bars": bars_for_prompt(df_h1, 8),
                "bar_closure": {"source_shift": 1, "time_is": "bar_open_time",
                                "timeframe_seconds": {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}},
                "positions": [],
            }
            state_changed = False
            for position in positions:
                ticket = int(position.ticket)
                state = self.guardian_states.get(ticket)
                with self._state_lock:
                    saved_context = self.entry_contexts.get(str(ticket))
                    context = dict(saved_context) if isinstance(saved_context, dict) else None
                is_buy = position.type == base.mt5.POSITION_TYPE_BUY
                entry = float(position.price_open)
                now_price = (float(tick.bid) if is_buy else float(tick.ask)) if tick else 0.0
                saved_risk = context.get("initial_risk") if context else None
                try:
                    risk = float(saved_risk) if saved_risk is not None else 0.0
                except (TypeError, ValueError):
                    risk = 0.0
                if risk <= 0:
                    risk = 0.0
                    if context is not None:
                        context["status"] = "unknown_after_restart"
                        context.setdefault("coverage", "since_restart")
                        with self._state_lock:
                            self.entry_contexts[str(ticket)] = context
                        state_changed = True
                move = (now_price - entry) if is_buy else (entry - now_price)
                best = adverse = None
                if state:
                    best = (state.highest_price - entry) if is_buy else (entry - state.lowest_price)
                    adverse = (entry - state.lowest_price) if is_buy else (state.highest_price - entry)
                    best = max(0.0, best)
                    adverse = max(0.0, adverse)
                    if context is not None:
                        context["last_observed_at"] = time.time()
                        context["observed_mfe"] = round(best, 2)
                        context["observed_mae"] = round(adverse, 2)
                        with self._state_lock:
                            self.entry_contexts[str(ticket)] = context
                        state_changed = True
                # Server time on both sides, so the difference is the real holding time.
                hold_minutes = (int(tick.time) - int(position.time)) / 60.0 if tick else 0.0
                if context is None:
                    context = {
                        "status": "unknown_after_restart",
                        "ticket": ticket,
                        "initial_risk": None,
                        "coverage": "since_current_process_observed",
                    }
                payload["positions"].append({
                    "ticket": ticket,
                    "direction": "BUY" if is_buy else "SELL",
                    "volume": float(position.volume),
                    "entry": entry,
                    "sl": float(position.sl or 0),
                    "tp": float(position.tp or 0),
                    "profit": round(float(position.profit or 0), 2),
                    "virtual_sl": round(float(state.virtual_sl), 2) if state else 0,
                    "hold_minutes": round(max(0.0, hold_minutes), 1),
                    "initial_risk": round(risk, 2) if risk > 0 else None,
                    "r_now": round(move / risk, 2) if risk > 0 else None,
                    "max_favorable": round(best, 2) if best is not None else None,
                    "max_adverse": round(adverse, 2) if adverse is not None else None,
                    "entry_context": context,
                })
            if state_changed:
                self.save_entry_contexts()
            for item in payload["positions"]:
                base.logger.info(
                    "AI-3证据 ticket=%s context=%s risk=%s r_now=%s MFE=%s MAE=%s coverage=%s"
                    % (
                        item["ticket"],
                        item["entry_context"].get("status"),
                        item["initial_risk"],
                        item["r_now"],
                        item["max_favorable"],
                        item["max_adverse"],
                        item["entry_context"].get("coverage"),
                    )
                )
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
                still_current=lambda: self.ai3_is_current(seq, bar_time) and not self._stop.is_set(),
            )
            if not self.ai3_is_current(seq, bar_time) or self._stop.is_set():
                base.logger.info("AI-3 返回已过期，丢弃 seq=%s" % seq)
                return
            if not raw:
                self.alert_api_failure("AI-3", "没有可见 JSON，本地护盘继续")
                return
            self.execute_ai3(extract_json_object(raw))
        except AIRequestError as exc:
            self.alert_api_failure(exc.role, exc.detail)
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
        self.reconcile_entry_contexts()
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
        base.logger.info(
            "AI证据契约=%s，决策快照目录=%s，参考入场=上一根已收盘M15"
            % ("entry-evidence-v5", AI_DECISION_SNAPSHOT_DIR)
        )
        base.logger.info("AI质量评分=%s，等级分值=%s，门槛=%s（非胜率）", QUALITY_VERSION, QUALITY_LEVEL_POINTS, AI_CONFIDENCE)
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


def run_http_retry_self_test():
    global AI_HTTP_RETRIES, AI_HTTP_RETRY_PAUSE
    old_post = requests.post
    old_logger = base.logger
    old_retries = AI_HTTP_RETRIES
    old_pause = AI_HTTP_RETRY_PAUSE
    AI_HTTP_RETRIES = 5
    AI_HTTP_RETRY_PAUSE = 0

    class OkResponse:
        status_code = 200
        text = "ok"
        encoding = "utf-8"

        def json(self):
            return {"choices": [{"message": {"content": "{\"action\":\"no\"}"}}]}

    try:
        # Mock failures must never look like live API failures in trading logs.
        self_test_logger = logging.Logger("xauusd_fib_volume_pa.http_retry_self_test")
        self_test_logger.addHandler(logging.NullHandler())
        base.logger = self_test_logger
        state = {"n": 0}

        def flaky(url, headers=None, json=None, timeout=None):
            state["n"] += 1
            if state["n"] < 3:
                raise requests.ReadTimeout("read timed out")
            return OkResponse()

        requests.post = flaky
        text = call_chat("AI-1", "m", "low", "sys", "hi", 1, "http://example.test", "key")
        assert "action" in text and state["n"] == 3, (text, state["n"])

        state["n"] = 0

        def always_down(url, headers=None, json=None, timeout=None):
            state["n"] += 1
            raise requests.ConnectionError("connection refused")

        requests.post = always_down
        try:
            call_chat("AI-2", "m", "low", "sys", "hi", 1, "http://example.test", "key")
            raise AssertionError("timeout retries should fail")
        except AIRequestError as exc:
            assert exc.role == "AI-2" and state["n"] == 5 and "没有可见 JSON" not in exc.detail, exc.detail

        state["n"] = 0

        def timeout_once(url, headers=None, json=None, timeout=None):
            state["n"] += 1
            raise requests.Timeout("slow")

        requests.post = timeout_once
        stale = call_chat(
            "AI-3", "m", "low", "sys", "hi", 1, "http://example.test", "key",
            still_current=lambda: state["n"] == 0,
        )
        assert stale == "" and state["n"] == 1, state["n"]
    finally:
        requests.post = old_post
        base.logger = old_logger
        AI_HTTP_RETRIES = old_retries
        AI_HTTP_RETRY_PAUSE = old_pause


def run_self_test():
    global AI3_STATE_FILE, AI_DECISION_SNAPSHOT_DIR
    fenced = extract_json_object("```json\n{\"action\":\"buy\",\"confidence\":0.8}\n```")
    assert fenced["action"] == "buy", fenced
    assert normalize_confidence(0.8) == 80
    assert normalize_confidence(75) == 75
    assert format_ai_action({"action": "no"}) == "观望"
    assert format_ai_action({"action": "buy"}) == "BUY"
    assert format_ai_action({"action": "sell"}) == "SELL"
    assert format_ai_action({"action": "other"}) == "无效"
    assert format_ai_action(None) == "未知"
    assert not task_is_current(1, 10, 2, 10)
    assert task_is_current(2, 10, 2, 10)

    def proposal(**values):
        result = {
            "trigger_evidence": "已收盘确认",
            "invalidation": "确认周期收盘失守结构边界",
            "target_basis": "前方可见结构目标",
        }
        result.update(values)
        return result

    passed, reason = finalize_ai_proposal(
        proposal(action="buy", confidence=88, stop_loss=96, take_profit=107, reasoning="结构回踩"),
        100, 2.0, "bull", "bull", None, 75,
    )
    assert passed is not None and passed.action == "buy", reason
    assert passed.details["trigger_evidence"] == "已收盘确认", passed.details
    assert passed.details["invalidation"] == "确认周期收盘失守结构边界", passed.details
    assert passed.take_profit <= 100 + 1.8 * (100 - passed.stop_loss) + 0.02
    missing, missing_reason = finalize_ai_proposal(
        {"action": "buy", "confidence": 90, "stop_loss": 96, "take_profit": 107, "reasoning": "无证据字段"},
        100, 2.0, "bull", "bull", None, 75,
    )
    assert missing is None and "证据字段缺失" in missing_reason, missing_reason

    rejected, reject_reason = finalize_ai_proposal(
        proposal(action="buy", confidence=90, stop_loss=96, take_profit=102, reasoning="目标太近"),
        100, 2.0, "bull", "bull", None, 75,
    )
    assert rejected is None and "盈亏比" in reject_reason, reject_reason

    against, against_reason = finalize_ai_proposal(
        proposal(action="sell", confidence=91, stop_loss=104, take_profit=92, reasoning="逆势"),
        100, 2.0, "bull", "bull", None, 75,
    )
    assert against is None and "不同向" in against_reason, against_reason
    conflicted, conflict_reason = finalize_ai_proposal(
        proposal(
            action="buy",
            confidence=91,
            stop_loss=96,
            take_profit=107,
            reasoning="事实冲突",
            fact_conflicts=["Fib方向与H1冲突"],
        ),
        100,
        2.0,
        "bull",
        "bull",
        None,
        75,
    )
    assert conflicted is None and "事实冲突" in conflict_reason, conflict_reason
    wide, wide_reason = finalize_ai_proposal(
        proposal(action="buy", confidence=90, stop_loss=90, take_profit=120, reasoning="wide"),
        100, 2.0, "bull", "bull", None, 75,
    )
    cap_text = "%.2f" % base.MAX_SL_ABS
    assert wide is None and cap_text in wide_reason, wide_reason

    tight_sl, tight_tp = clamp_proposal("sell", 100.0, 102.5, 90.0, 10.0)
    assert 100.0 < tight_sl <= 100.0 + base.MAX_SL_ABS + 0.02, (tight_sl, tight_tp)
    local_kept, local_reason = finalize_ai_proposal(
        proposal(action="sell", confidence=88, stop_loss=112, take_profit=90, reasoning="远影线"),
        100, 10.0, "bear", "bear", None, 75, local_stop=103.2, local_target=94.0,
    )
    assert local_kept is not None and abs(local_kept.stop_loss - 103.2) < 0.001, local_reason
    near_m5 = base.pd.DataFrame([
        {"open": 108.0, "high": 109.2, "low": 107.4, "close": 107.6},
        {"open": 107.6, "high": 109.0, "low": 107.2, "close": 107.4},
        {"open": 107.4, "high": 109.1, "low": 106.8, "close": 107.0},
    ])
    timed_kept, timed_reason = finalize_ai_proposal(
        proposal(action="sell", confidence=88, stop_loss=120, take_profit=90, reasoning="远影线"),
        107.0, 10.0, "bear", "bear", None, 75, df_m5=near_m5,
    )
    assert timed_kept is not None and 0 < timed_kept.stop_loss - 107.0 <= base.MAX_SL_ABS + 0.02, timed_reason
    assert timed_kept.details["entry_price_source"] == "m5_rejection_fallback_close", timed_kept.details
    far_m5 = base.pd.DataFrame([
        {"open": 100.0, "high": 111.0, "low": 99.0, "close": 100.0},
        {"open": 100.0, "high": 111.5, "low": 99.0, "close": 100.0},
        {"open": 100.0, "high": 112.0, "low": 99.0, "close": 99.5},
    ])
    far_kept, far_reason = finalize_ai_proposal(
        proposal(action="sell", confidence=88, stop_loss=120, take_profit=90, reasoning="远影线"),
        99.5, 2.0, "bear", "bear", None, 75, df_m5=far_m5,
    )
    assert far_kept is None and cap_text in far_reason, far_reason

    wick_m5 = base.pd.DataFrame([
        {"open": 100.2, "high": 100.0 + base.MAX_SL_ABS + 1.8, "low": 99.4, "close": 99.8},
        {"open": 99.8, "high": 100.0 + base.MAX_SL_ABS + 1.6, "low": 99.2, "close": 99.6},
        {"open": 99.6, "high": 100.0 + base.MAX_SL_ABS + 1.4, "low": 99.0, "close": 99.4},
    ])
    inside_wick, inside_reason = finalize_ai_proposal(
        proposal(action="sell", confidence=88, stop_loss=102.0, take_profit=94.0, reasoning="影线内止损"),
        100.0, 2.0, "bear", "bear", None, 75, df_m5=wick_m5, local_stop=102.0, local_target=94.0,
    )
    assert inside_wick is None and "影线" in inside_reason, inside_reason
    near_wick = base.pd.DataFrame([
        {"open": 100.4, "high": 101.2, "low": 99.8, "close": 100.1},
        {"open": 100.1, "high": 101.1, "low": 99.6, "close": 99.9},
        {"open": 99.9, "high": 101.0, "low": 99.4, "close": 99.6},
    ])
    moved, moved_reason = finalize_ai_proposal(
        proposal(action="sell", confidence=88, stop_loss=101.4, take_profit=96.0, reasoning="止损偏近"),
        100.0, 2.0, "bear", "bear", None, 75, df_m5=near_wick,
    )
    assert moved is not None and moved.stop_loss >= 101.6 - 0.01, moved_reason

    assert ai2_allows({"decision": "APPROVE", "reasoning": "ok"})[0]
    assert not ai2_allows({"decision": "APPROVE", "defects": ["有缺陷"], "reasoning": "ok"})[0]
    assert not ai2_allows({"decision": "APPROVE", "fact_conflicts": ["数据冲突"], "reasoning": "ok"})[0]
    assert not ai2_allows({"decision": "APPROVE"})[0]
    assert not ai2_allows({"decision": "REJECT", "defects": ["盈亏比不足"]})[0]
    assert not ai2_allows(None)[0]

    feature_rows = []
    for index in range(40):
        close = 100.0 + index * 0.1
        feature_rows.append(
            {
                "time": index,
                "open": close - 0.05,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "tick_volume": 100 + index,
            }
        )
    feature_m15 = base.pd.DataFrame(feature_rows)
    feature_h1 = base.pd.DataFrame(feature_rows + feature_rows)
    feature_m5 = feature_m15.tail(10).copy()
    feature_local = base.StrategyResult(
        "buy", 80, False, "test", 99.0, 103.0, 80, 10,
        {"regime": "trend", "session": "asia", "setup": "pullback", "location": "0.5", "pa_score": "20"},
    )
    feature_pack, _, _, _ = build_feature_pack(
        feature_m15, feature_h1, feature_m5, feature_local,
        quote={"bid": 103.0, "ask": 103.1, "time": 40}, decision_id="test-1",
    )
    assert feature_pack["decision_id"] == "test-1"
    assert feature_pack["bars_are_closed"] is True
    assert len(feature_pack["m15_bars"]) == 32
    assert len(feature_pack["evidence_windows"]["m15_structure_bar_times"]) == 24
    assert len(feature_pack["evidence_windows"]["m5_rejection_bar_times"]) == 3
    assert feature_pack["reference_price"]["source"] == "previous_closed_m15_close"
    assert feature_pack["bar_closure"]["source_shift"] == 1
    computed = feature_pack["computed_evidence"]
    assert computed["contract_version"] == "entry-evidence-v5"
    assert computed["closed_bar_source_shift"] == 1
    assert computed["m5_timing"]["buy"]["event_group"] == "m5_confirmation_window"
    assert "obstacles" in computed and "buy" in computed["obstacles"]

    obstacle_m15 = base.pd.DataFrame(
        [
            {"time": 1, "open": 100, "high": 101, "low": 95, "close": 96, "tick_volume": 100},
            {"time": 2, "open": 96, "high": 99, "low": 95, "close": 98, "tick_volume": 120},
            {"time": 3, "open": 98, "high": 99, "low": 96, "close": 97, "tick_volume": 110},
        ]
    )
    obstacle_m5 = obstacle_m15.copy()
    grouped = _obstacle_candidates(
        obstacle_m15,
        obstacle_m5,
        None,
        {"available": True, "box_edge": 99.0, "breakout_bar": {"time": 2}},
        100.0,
        "sell",
    )
    low_candidate = next(item for item in grouped if item["price"] == 95.0)
    assert any(item["source"] == "m5_process_extreme" for item in low_candidate["observed"]), grouped
    fib_fact = _fib_evidence(obstacle_m15, base.build_fib_levels("bear", 90.0, 110.0), 2.0, 98.0)
    assert fib_fact["reference_in_core"] is True and fib_fact["tolerance"] == 0.7, fib_fact

    import tempfile
    old_state_file = AI3_STATE_FILE
    with tempfile.TemporaryDirectory() as temp_dir:
        AI3_STATE_FILE = os.path.join(temp_dir, "position-state.json")
        state_writer = ThreeAITrader()
        state_writer.entry_contexts = {
            "123": {
                "initial_risk": 2.5,
                "setup": "pullback",
                "trigger_evidence": "已收盘确认",
                "invalidation": "收盘失守",
                "target_basis": "前高",
            }
        }
        state_writer.save_entry_contexts()
        state_reader = ThreeAITrader()
        assert state_reader.entry_contexts["123"]["initial_risk"] == 2.5
        assert state_reader.entry_contexts["123"]["target_basis"] == "前高"
    AI3_STATE_FILE = old_state_file

    old_snapshot_dir = AI_DECISION_SNAPSHOT_DIR
    with tempfile.TemporaryDirectory() as temp_dir:
        AI_DECISION_SNAPSHOT_DIR = temp_dir
        append_decision_snapshot("test-snapshot", "self_test", {"secret": "not included", "value": 1})
        snapshot_files = os.listdir(temp_dir)
        assert snapshot_files, snapshot_files
        with open(os.path.join(temp_dir, snapshot_files[0]), "r", encoding="utf-8") as snapshot_file:
            snapshot_text = snapshot_file.read()
        assert "test-snapshot" in snapshot_text and "value" in snapshot_text, snapshot_text
    AI_DECISION_SNAPSHOT_DIR = old_snapshot_dir

    assert not base.sl_change_is_tighter("BUY", 100, 99)
    assert base.sl_change_is_tighter("SELL", 110, 109)
    run_http_retry_self_test()
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
