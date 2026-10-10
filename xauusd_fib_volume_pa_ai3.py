# -*- coding: utf-8 -*-
"""XAUUSD three-role AI trader.

AI-1 proposes a direction from the local feature pack.
AI-2 may only approve or reject clear defects.
AI-3 may hold, tighten the stop, or close. It cannot widen a stop.
Python still enforces reward/risk, H1/M15 alignment, the configured stop cap, and the local guardian.
"""

import copy
import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit

import requests
from strategies.evidence import _rounded_number, _bar_payload, _extreme_bar, bars_for_prompt, bar_times_for_prompt


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


def request_trace_id(prefix):
    """Keep readable trace prefixes without relying on clock resolution for uniqueness."""
    return "%s-%s-%s" % (prefix, time.time_ns(), uuid.uuid4().hex)


AI_ENABLED = env_bool("FIB_PA_AI_ENABLED", True)
AI_ENABLE_TRADING = env_bool("FIB_PA_AI_ENABLE_TRADING", True)
AI_MAGIC = env_int("FIB_PA_AI_MAGIC", 240611)
AI_CONFIDENCE = env_int("FIB_PA_AI_CONFIDENCE", 75)
AI1_MODEL = os.getenv("FIB_PA_AI1_MODEL", os.getenv("GPTSTORE_MODEL", "gpt-5.5"))
AI2_MODEL = os.getenv("FIB_PA_AI2_MODEL", AI1_MODEL)
AI3_MODEL = os.getenv("FIB_PA_AI3_MODEL", AI1_MODEL)
DEFAULT_AI_URL = os.getenv("GPTSTORE_BASE_URL", "https://hapiopen.cc/v1/messages").strip() or "https://hapiopen.cc/v1/messages"
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
AI2_TIMEOUT = env_int("FIB_PA_AI2_TIMEOUT", 90)
AI3_TIMEOUT = env_int("FIB_PA_AI3_TIMEOUT", 40)
# 5 次 60 秒超时大约 300 秒。接管时间必须盖住重试，否则下一根 K 线会丢掉后面的结果。
ENTRY_TAKEOVER_SEC = env_int("FIB_PA_AI_ENTRY_TAKEOVER_SEC", 360)
AI3_TAKEOVER_SEC = env_int("FIB_PA_AI3_TAKEOVER_SEC", 220)
AI_HTTP_RETRIES = max(1, env_int("FIB_PA_AI_HTTP_RETRIES", 5))
# A read timeout may leave a billable generation running upstream.
AI_READ_TIMEOUT_RETRIES = max(0, env_int("FIB_PA_AI_READ_TIMEOUT_RETRIES", 0))
AI_MAX_TOKENS = {
    "AI-1": max(1024, env_int("FIB_PA_AI1_MAX_TOKENS", 4096)),
    "AI-2": max(1024, env_int("FIB_PA_AI2_MAX_TOKENS", 4096)),
    "AI-3": max(512, env_int("FIB_PA_AI3_MAX_TOKENS", 2048)),
}
AI_HTTP_RETRY_PAUSE = 2.0
API_ALERT_COOLDOWN = env_int("FIB_PA_AI_ALERT_COOLDOWN", 900)
AI3_STATE_FILE = os.path.join(base.LOG_DIR, "ai3_position_state.json")
AI_DECISION_SNAPSHOT_DIR = os.path.join(base.LOG_DIR, "ai_decisions")
_SNAPSHOT_LOCK = threading.RLock()
AI_USAGE_DIR = os.path.join(base.LOG_DIR, "ai_usage")
_USAGE_LOCK = threading.RLock()
_CHAT_OPTIONS_LOCK = threading.RLock()
_CHAT_OPTIONS = {}
COST_CONTROL_VERSION = "cost-latency-v2"
MARKET_FRESHNESS_VERSION = "request-freshness-v2"
RESPONSE_CONTRACT_VERSION = "response-contract-v3"
QUALITY_LEVEL_POINTS = {"strong": 20, "adequate": 16, "weak": 8, "missing": 0}
QUALITY_DIMENSIONS = ("structure", "trigger", "protection", "target", "counter_evidence")
QUALITY_CAUSES = {"volume", "shallow_retrace", "trigger_weakness", "opposing_structure", "stop_vulnerability", "target_uncertainty", "execution_drift", "missing_data", "hard_limit"}
LEGACY_QUALITY_VERSION = "entry-quality-v1"
QUALITY_VERSION = "entry-quality-v2"

base.MAGIC = AI_MAGIC
base.ENABLE_TRADING = AI_ENABLE_TRADING
SL_CAP = "%g" % base.MAX_SL_ABS

AI1_INSTRUCTIONS = (
    "你是XAUUSD分析师AI-1，自主提出buy/sell/no；不看账户、不定手数、不下单。"
    "按session和strategy_contract分析；parameters/ai_rules.entry定义本次策略，strategy_evidence是同版本评分事实。"
    "当前方向的blockers是策略硬条件；本地低分或no不剥夺自主提出合法方案的权利。"
    "方向仅H1，M15管本策略结构/量能/价格行为，M5只管时机；H4不否决，M15暂反向可以是回撤。"
    "H1同向只是方向许可，还须策略有效、真实收盘证据及目标空间；analysis_mode允许原始K线自主触发证据，不要求固定检测器确认；analysis_checkpoint只是分析及去重范围，不是交易触发。引用computed_evidence和strategy_evidence的事实、bar_time/price，禁止另算或编造输入已提供的指标。"
    "事实冲突写fact_conflicts并no，必要的OHLC/来源/时效/保护/目标证据缺失写unknowns并no；外部新闻日历未知只作信息上下文，不自动迫使no，不猜测。"
    "涉及Fib时按给定容差/recent_probe/confirmation_extension，不因略越38.2/61.8否决。"
    "边缘收盘失守、独立反向结构收盘确认或明确形态失效才重判。"
    "目标须有数据依据及中间障碍，不为凑RR延远；首个未克服的重要障碍前不足1.5RR或无收盘触发则no并写等待条件。"
    "量能只加减分，普通/弱量不单独否决，不编造放量，使用给定基线/比值。"
    f"M5拒绝/吞没可确认时机；止损须保护命名结构、覆盖最近3根M5极值+缓冲，距参考价≤{SL_CAP}美元，不能放进影线。"
    "M15远影线不自动否定合格M5保护；结构止损放不下则no。"
    f"H1不许可、止损>{SL_CAP}美元、偏离M15 EMA21>1.5ATR、价格方向错或RR<1.5均no。"
    "输出一个紧凑JSON，字段action、stop_loss、take_profit、reasoning、trigger_evidence、invalidation、target_basis、fact_conflicts、unknowns、quality_assessment、wait_basis。"
    "SL/TP为绝对价格。reasoning用≤140汉字概括setup/失效/目标/最强反证；证据字段各≤60汉字，含必要周期/时间/价位，未知明示。"
)
AI2_INSTRUCTIONS = (
    "你是独立审核官AI-2，只APPROVE/REJECT，不提新交易、不重新评分。审核proposal及python_finalization最终价格，不审核已替换的AI1价格。"
    "按market_evidence.strategy_contract.ai_rules.review独立核对同版本策略硬条件和strategy_evidence，不复用其他策略条件。"
    f"仅明确硬伤REJECT：违反本策略硬条件、"
    f"H1不许可、SL/TP缺失或方向错、止损>{SL_CAP}美元、追价>1.5ATR、RR<1.5、必要数据缺失或订单自相矛盾。"
    "根据完整market_evidence原始K线和computed_evidence独立核实，不信任AI1解释。"
    "setup明确失效且无新结构确认、回踩/放量被数据反驳须指出bar_time/price/source；影线收回不算收盘破坏。"
    "涉及Fib时用给定容差/recent_probe/confirmation_extension；M5可确认时机不能改方向；合格M5保护不因远处M15影线而否决。"
    "M15暂反向、轻微背离、逆H4、普通量、低ADX、本地no、RR刚过线或感觉不确定不能单独否决。"
    "最终目标与AI理由不同须有可核实调整依据，否则REJECT；数据不足指出字段，不猜事实。"
    "输出紧凑JSON：decision、defects数组、reasoning、fact_conflicts数组；APPROVE两数组为空。"
    "reasoning≤120汉字，缺陷只列独立硬伤及必要时间/价格/来源，不复述行情和评分卡。"
)
AI1_INSTRUCTIONS += (
    "输入存在opportunities时一次自主选择一个opportunity_id，包括no也必须指明评估候选。动作及质量方向绑定该候选，"
    "只引用自身/opportunities/原索引/下证据或共享OHLC/price_action/patterns，不能借用其他候选或本地赢家别名。"
    "本地分低或参考方案失败不剥夺自主提出安全方案的权限；patterns仅几何与位置事实，不是吞没硬门或首轮趋势判断。"
    "live_quote是本次请求重新采集的报价，不是参考K线收盘价；按当前报价核实方案可执行性，不能把止损贴近现价形成的高RR当作质量提升。"
    "local_plan_feasibility只评价原本地SL/TP，不是否决其他自主方案的全局开关；本地no或该方案不可执行时，仍可提出有证据的其他合法方案。"
    "Python按rules.price_finalization定稿；按reference_price的模式参考价考虑M15 ATR最小止损宽度及target_max_reference_r目标上限；analysis_mode买用成功请求Ask、卖用Bid，旧模式用其闭合K线参考，再核实实时报价RR，不能按随后会被截短的远目标宣称通过。"
)
AI2_INSTRUCTIONS += (
    "ai1_snapshot是原分析报价，market_evidence.live_quote是本次审核新报价；比较变化，按final_plan_feasibility核实当前执行区间、止损触发侧剩余空间及追价。"
    "executable=false是当前方案硬伤；不得扩大执行区间或移动SL/TP来放行。APPROVE只针对本次快照，Python仍会复核下单瞬间报价。"
)
OBSTACLE_EVIDENCE_INSTRUCTIONS = (
    "obstacles是候选且不穷尽结构，Fib端点不天然为独立强障碍。unknown/unassessed结合origin_bar/observed评估，不自动硬否决。"
    "event_group仅合并已核实同源极值，同价不同时间分别保留，空组不合并，同组各次反向证据仍需看。归属未知不等于关键证据缺失。"
    "not_observed_in_window不代表历史从未突破；opposing_price_action不自动等于有效反转。"
    "不得删除真障碍，列表外结构须引用真实timeframe/bar_time/price/source，未知明示。"
)
AI1_INSTRUCTIONS += OBSTACLE_EVIDENCE_INSTRUCTIONS
AI2_INSTRUCTIONS += OBSTACLE_EVIDENCE_INSTRUCTIONS
PRICE_ACTION_INSTRUCTIONS = (
    "按computed_evidence.price_action分周期/方向/时间核实。details.price_action是M15及可选M5汇总。"
    "M15或M5触发即可，不要求双周期；M5零分不否定M15触发。false_break对本周期前6根极值：sell高破后收回，buy镜像，不要求颜色/长影线。"
    "触发事实与质量意见分开，不能编造冲突；方向、setup、风险限制仍有效。原始K线省略的candle_parts可由OHLC还原。"
)
AI1_INSTRUCTIONS += PRICE_ACTION_INSTRUCTIONS
AI2_INSTRUCTIONS += PRICE_ACTION_INSTRUCTIONS
AI1_INSTRUCTIONS += (
    "按quality_rubric评五项，不猜胜率、不复制本地分、不抬分。quality_assessment={version,direction,dimensions}，version=entry-quality-v2。"
    "dimension={level,reason,evidence_refs,reservations}；reservation={cause,event_group,reason,evidence_refs}，直接放入唯一归属维度，禁止另填id/dimension/severity/reservation_ids。"
    "level一次声明程度：strong=20且无保留；adequate=16且保留仅轻微；weak=8且保留是重大反证；missing=0且保留是阻断条件。weak/missing必须有保留。"
    "direction为buy/sell，dimensions是rubric五项为键的对象；reservations、wait_basis、fact_conflicts、unknowns、evidence_refs均为数组。"
    "refs为本次输入真实JSON Pointer。每项reason≤35汉字，refs只列必要证据；同cause/event只扣一项。"
    "普通量/浅回撤最多轻微，不能评weak/missing；Python据level生成程度并加总，不输出自由confidence。"
    "执行止损空间问题归protection，实时RR不足归target，不能因报价漂移把已命中的触发评missing。"
    "no仍评允许方向，wait_basis列{code,reason,evidence_refs}，code为hard_limit/missing_structure/missing_trigger/missing_data/target_space/material_counter_evidence。"
    "已有周期触发不写missing_trigger；仅minor且位置/确认/保护/目标成立应提案交审核，不无限等待更漂亮形态。"
    "独立重大反证仍自主判断，不为出单删真障碍；只省重复说明，不能漏独立硬伤或关键证据。"
)
AI3_INSTRUCTIONS = (
    "按entry_context.strategy_contract.ai_rules.guardian及入场保存的策略证据护盘；不套用后来的新策略。契约缺失标未知。"
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
    "先对照entry_context核实原入场理由，不重新评分。"
    "max_adverse为null或覆盖范围有限时，不得写成从未回撤；只用可见区间的MFE/MAE，并明确coverage。"
    "原始K线省略的candle_parts可由OHLC还原。输出完整JSON：action、ticket（原持仓票号）、new_stop_loss（非收紧时为null）、reasoning。"
    "reasoning≤100汉字，保留判断所需周期/时间/价位，不复述全部行情。"
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

def fib_payload(fib):
    if fib is None:
        return None
    return {
        "direction": fib.direction,
        "swing_low": round(float(fib.swing_low), 2),
        "swing_high": round(float(fib.swing_high), 2),
        "levels": {key: round(float(value), 2) for key, value in fib.levels.items()},
    }


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


def _breakout_evidence(df_m15, atr, action, parameters=None):
    return base.STRATEGIES.snapshot().get("breakout").describe(df_m15, atr, action, parameters)


def _range_edge_evidence(df_m15, atr, action, parameters=None):
    return base.STRATEGIES.snapshot().get("range_edge").describe(df_m15, atr, action, parameters)


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
            boundary = extreme + buffer if action == "sell" else extreme - buffer
            protected_stop = base.round_protective_stop(action, boundary)
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
    parameters = (local_result.details or {}).get("strategy_contract", {}).get("parameters", {})
    pinned = (local_result.details or {}).get("strategy_assessments", {}).get(candidate_actions[0], {}).get("evidence")
    breakout = (copy.deepcopy(pinned) if pinned is not None else _breakout_evidence(df_m15, atr, candidate_actions[0], parameters)) if setup == "breakout" else {"available": False, "reason": "setup_not_breakout"}
    range_edge = (copy.deepcopy(pinned) if pinned is not None else _range_edge_evidence(df_m15, atr, candidate_actions[0], parameters)) if setup == "range_edge" else {"available": False, "reason": "setup_not_range_edge"}
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


def opportunity_selection(payload, pack):
    """Resolve only the successful request's pool; legacy packs have no pool key."""
    if "opportunities" not in pack:
        return None, ""
    identifier = payload.get("opportunity_id") if isinstance(payload, dict) else None
    matches = [(i, item) for i, item in enumerate(pack["opportunities"])
               if item.get("opportunity_id") == identifier]
    if not isinstance(identifier, str) or not identifier or len(matches) != 1:
        return None, "AI-1 /opportunity_id缺失、未知或重复"
    index, candidate = matches[0]
    card = payload.get("quality_assessment")
    direction = card.get("direction") if isinstance(card, dict) else None
    if direction != candidate["action"] or payload.get("action") not in ("no", candidate["action"]):
        return None, "AI-1 /opportunity_id动作或quality_assessment/direction不一致"
    def check_refs(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "evidence_refs" and isinstance(child, list):
                    for pointer in child:
                        if not isinstance(pointer, str):
                            return "AI-1证据路径无效"
                        parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
                        own = len(parts) >= 3 and parts[:2] == ["opportunities", str(index)]
                        if own and "strategy_evidence" in parts:
                            evidence_at = parts.index("strategy_evidence")
                            own = len(parts) > evidence_at + 1 and parts[evidence_at + 1] == candidate["action"]
                        shared = parts and parts[0] in ("m15_bars", "m5_bars", "h1_bars", "patterns", "live_quote", "atr", "h1_trend", "m15_trend", "m15_ema21", "bar_closure")
                        shared = shared or parts[:2] == ["computed_evidence", "price_action"]
                        if not (own or shared) or any(p in ("host_verified_region", "host_verified_entry_event", "host_verified_analysis_checkpoint", "candidates") for p in parts):
                            return "AI-1证据隔离拒绝: " + pointer
                error = check_refs(child)
                if error:
                    return error
        elif isinstance(value, list):
            for child in value:
                error = check_refs(child)
                if error:
                    return error
        return ""
    error = check_refs(payload)
    return (index, candidate), error


def selected_opportunity_pack(pack, selection):
    """Separate execution aliases; never alter what AI1's pointers meant."""
    if selection is None:
        return copy.deepcopy(pack)
    index, candidate = selection
    result = copy.deepcopy(pack)
    result["opportunities"] = [{} for _ in pack["opportunities"]]
    result["opportunities"][index] = copy.deepcopy(candidate)
    result["opportunity_id"] = candidate["opportunity_id"]
    result["opportunity_blockers"] = [item for item in result.get("opportunity_blockers", [])
                                       if item.get("opportunity_id") == candidate["opportunity_id"]]
    result["strategy_contract"] = copy.deepcopy(candidate["strategy_contract"])
    result["strategy_fingerprint"] = candidate["strategy_fingerprint"]
    result["strategy_evidence"] = copy.deepcopy(candidate["strategy_evidence"])
    result["reference_price"] = copy.deepcopy(candidate["reference_price"])
    if candidate.get("coarse_reference_price"):
        result["coarse_reference_price"] = copy.deepcopy(candidate["coarse_reference_price"])
    result["price"] = candidate["reference_price"]["price"]
    result["details"] = copy.deepcopy(candidate["local_plan"].get("details", {}))
    result["setup"] = result["details"].get("setup", candidate["strategy_contract"].get("setup", candidate["strategy_contract"].get("id")))
    result["local_action"] = candidate["action"]
    result["local_confidence"] = candidate["local_score"]
    result["local_reasoning"] = candidate["local_plan"].get("reasoning", "")
    result["local_stop_loss"] = candidate["local_plan"].get("stop_loss", 0)
    result["local_take_profit"] = candidate["local_plan"].get("take_profit", 0)
    result["computed_evidence"] = {"price_action": result.get("computed_evidence", {}).get("price_action", {})}
    result["fib"] = None
    result["timeframe"] = "M5" if "m5" in candidate["reference_price"].get("source", "").lower() else "M15"
    finalization = result.get("rules", {}).get("price_finalization", {})
    finalization["reference"] = candidate["reference_price"].get("source")
    finalization["target_max_reference_r"] = candidate["strategy_contract"].get("target_max_reference_r", 1.8)
    result["local_plan_feasibility"] = plan_feasibility(candidate["action"],
        candidate.get("coarse_reference_price", candidate["reference_price"])["price"],
        result["local_stop_loss"], result["local_take_profit"], result["live_quote"], result["atr"], result.get("m15_ema21"))
    return result


def role_rules(role, contracts):
    key = {"AI-1": "entry", "AI-2": "review", "AI-3": "guardian"}[role]
    return [{"profile": contract.get("id"), "rule": contract.get("ai_rules", {}).get(key, "")}
            for contract in contracts if contract]


def role_instructions(role, contracts):
    generic = {"AI-1": AI1_INSTRUCTIONS, "AI-2": AI2_INSTRUCTIONS, "AI-3": AI3_INSTRUCTIONS}[role]
    rules = role_rules(role, contracts)
    extra = ""
    if role == "AI-3" and any(contract.get("analysis_mode") for contract in contracts):
        extra = "\nanalysis_checkpoint有效期及消费只限制新入场；开仓后检查点过期或已消费本身不是交易失效，不据此平仓。原始打法失效证据与入场保存的原始持仓契约中Python持仓期限分别判断。"
    return generic + extra + "\n本次来源绑定的策略角色规则：" + json.dumps(rules, ensure_ascii=False)


def request_contracts(pack):
    return [item["strategy_contract"] for item in pack.get("opportunities", [])] or [pack.get("strategy_contract", {})]


def analysis_scope_keys(pack):
    """Closed-source request identity, independent of an asserted trade trigger."""
    scopes = set()
    for item in pack.get("opportunities", []):
        if not item.get("strategy_contract", {}).get("analysis_mode"):
            continue
        checkpoint = item["strategy_evidence"][item["action"]]["evidence"]["analysis_checkpoint"]
        scopes.add((item["strategy_contract"]["id"], item["strategy_fingerprint"],
                    checkpoint["m5_bar_time"], checkpoint["m15_bar_time"]))
    return scopes


def build_feature_pack(df_m15, df_h1, df_m5, local_result, quote=None, decision_id=None):
    atr = base.calculate_atr(df_m15)
    price = float(df_m15["close"].iloc[-1])
    reference_bar_time = int(df_m15["time"].iloc[-1]) if "time" in df_m15.columns else 0
    h1_trend = base.trend_from_ema(df_h1, 50)
    m15_trend = base.trend_from_ema(df_m15, 50)
    fib = base.entry_fib_structure(df_m15, h1_trend, atr)
    contract = copy.deepcopy((local_result.details or {}).get("strategy_contract", {}))
    price, reference_source, reference_time = base.strategy_reference(df_m15, df_m5, contract)
    if contract.get("stop_reference") == "m5_extreme":
        fib = None
    parameters = contract.get("parameters", {})
    pack = {
        "symbol": base.SYMBOL,
        "timeframe": "M5" if contract.get("entry_reference") == "closed_m5" else "M15",
        "risk_timeframe": "M15",
        "direction_timeframe": "H1",
        "price": round(price, 2),
        "decision_id": decision_id,
        "strategy_framework": base.STRATEGY_FRAMEWORK_VERSION,
        "strategy_fingerprint": (local_result.details or {}).get("strategy_fingerprint"),
        "strategy_contract": contract,
        "strategy_evidence": copy.deepcopy((local_result.details or {}).get("strategy_assessments", {})),
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
            "source": reference_source,
            "bar_time": reference_time,
        },
        "live_quote": quote or {"bid": None, "ask": None, "time": None},
        "atr": round(atr, 2),
        "m15_ema21": base.ema_last(df_m15["close"], 21),
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
        "quality_rubric": quality_rubric(contract.get("analysis_mode", False)),
        "rules": {
            "h1_sets_direction": True,
            "m15_opposite_is_pullback": contract.get("regime_filter", "pullback") == "pullback",
            "h4_is_not_a_veto": True,
            "pullback_tight_range_is_no": contract.get("regime_filter", "pullback") == "pullback",
            "pullback_wide_range_only_at_edges": contract.get("regime_filter", "pullback") == "pullback",
            "breakout_and_range_edge_use_own_conditions": True,
            "max_stop_abs": base.MAX_SL_ABS,
            "chase_max_atr": base.CHASE_MAX_ATR,
            "min_rr": base.MIN_REWARD_RISK,
            "price_finalization": {"reference": reference_source,
                                   "stop_price_version": base.STOP_PRICE_VERSION,
                                   "stop_rounding": "BUY floor / SELL ceiling to 0.01, then enforce max_stop_abs",
                                   "generation_min_risk": base.entry_risk_limits(atr)[0],
                                   "target_max_reference_r": contract.get("target_max_reference_r", 1.8),
                                   "execution_min_stop_clearance": 2 * base.stop_buffer(atr),
                                   "execution_clearance_price": "BUY bid / SELL ask",
                                   "generation_floor_is_not_execution_floor": True},
        },
        "m15_bars": bars_for_prompt(df_m15, 32),
        "evidence_windows": {
            "m15_structure_lookback": parameters.get("lookback", 24),
            "m15_structure_bar_times": bar_times_for_prompt(df_m15, parameters.get("lookback", 24)),
            "breakout_box_lookback": parameters.get("box_bars", 16),
            "breakout_recent_bars": parameters.get("recent_bars", 4),
            "breakout_bar_times": bar_times_for_prompt(df_m15, parameters.get("box_bars", 16) + parameters.get("recent_bars", 4)),
            "m5_rejection_lookback": 3,
            "m5_rejection_bar_times": bar_times_for_prompt(df_m5, 3),
        },
        "h1_bars": bars_for_prompt(df_h1, 8),
        "m5_bars": bars_for_prompt(df_m5, 8),
    }
    plan_entry = float((local_result.details or {}).get("entry_price") or price)
    pack["local_plan_feasibility"] = plan_feasibility(local_result.action, plan_entry, local_result.stop_loss,
                                                     local_result.take_profit, pack["live_quote"], atr,
                                                     pack["m15_ema21"])
    if "opportunities" in (local_result.details or {}):
        pack["opportunities"] = copy.deepcopy(local_result.details["opportunities"])
        pack["opportunity_blockers"] = [{"opportunity_id": item["opportunity_id"],
                                          "blockers": item["structural_blockers"],
                                          "local_plan_status": item["local_plan_status"]}
                                         for item in pack["opportunities"]]
        pack["opportunities"] = [item for item in pack["opportunities"] if base.opportunity_eligible(item)]
        for item in pack["opportunities"]:
            if item["strategy_contract"].get("analysis_mode"):
                item["coarse_reference_price"] = copy.deepcopy(item["reference_price"])
                side = item["action"]
                live = pack["live_quote"]
                value = live.get("ask" if side == "buy" else "bid")
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise MarketSnapshotError("自主分析实时参考报价无效")
                item["reference_price"] = {"price": value,
                    "source": "ai1_request_live_ask" if side == "buy" else "ai1_request_live_bid",
                    "snapshot_time": live.get("observed_at"), "tick_time": live.get("time")}
                item["local_plan_feasibility"] = plan_feasibility(side, item["coarse_reference_price"]["price"],
                    item["local_plan"]["stop_loss"], item["local_plan"]["take_profit"], live, atr, pack["m15_ema21"])
        if any(item["strategy_contract"].get("analysis_mode") for item in pack["opportunities"]):
            pack["rules"]["price_finalization"]["reference"] = "selected opportunity successful AI1 request live Ask for BUY / Bid for SELL"
        # Full shared source frames once, including region reconstruction/ATR warmup.
        source_count = max([32] + [int(item["strategy_contract"].get("parameters", {}).get("pivot_lookback", 0))
                                  + int(item["strategy_contract"].get("parameters", {}).get("atr_period", 14))
                                  for item in pack["opportunities"]])
        pack["m15_bars"] = bars_for_prompt(df_m15, source_count)
        pack["m5_bars"] = bars_for_prompt(df_m5, len(df_m5))
        from strategies.patterns import patterns_facts
        pack["patterns"] = {"M15": patterns_facts(df_m15, "M15"), "M5": patterns_facts(df_m5, "M5")}
    return pack, fib, atr, price


def compact_market_pack(pack):
    """Drop only reconstructible candle math and exact duplicate metadata."""
    result = copy.deepcopy(pack)
    if "opportunities" in result:
        # Execution keeps the full host snapshot. Requests expose one selected event,
        # not nested alternative event payloads or duplicate full host windows.
        def compact_source(value):
            if isinstance(value, dict):
                value.pop("host_verified_region", None)
                value.pop("host_verified_entry_event", None)
                value.pop("host_verified_analysis_checkpoint", None)
                for diagnostic in value.get("candidates", []) if isinstance(value.get("candidates"), list) else []:
                    if isinstance(diagnostic, dict):
                        diagnostic.pop("candidate", None)
                for child in value.values():
                    compact_source(child)
            elif isinstance(value, list):
                for child in value:
                    compact_source(child)
        for candidate in result["opportunities"]:
            if candidate:
                candidate["strategy_evidence"] = {candidate["action"]: candidate.get("strategy_evidence", {}).get(candidate["action"], {})}
            plan = candidate.get("local_plan", {})
            details = plan.get("details", {})
            plan["details"] = {key: value for key, value in details.items()
                               if key in ("setup", "entry_price", "entry_price_source", "location", "pa_score", "fib")}
            compact_source(candidate)
        result.get("details", {}).pop("opportunities", None)
        # No winner-specific aliases are valid AI1 evidence in pool mode.
        # Keep selected execution aliases only for AI2; pool already has all facts.
        if not result.get("opportunity_id"):
            for key in ("details", "strategy_evidence", "strategy_contract", "reference_price", "fib", "local_plan_feasibility",
                        "local_stop_loss", "local_take_profit", "local_reasoning", "setup"):
                result.pop(key, None)
            computed = result.get("computed_evidence", {})
            result["computed_evidence"] = {"price_action": computed.get("price_action", {})}
        else:
            chosen = next(item for item in result["opportunities"] if item.get("opportunity_id") == result["opportunity_id"])
            result["strategy_evidence"] = copy.deepcopy(chosen["strategy_evidence"])
            result["details"] = copy.deepcopy(chosen.get("local_plan", {}).get("details", {}))
        compact_source(result)

    def compact_bar(bar):
        compact = dict(bar)
        parts = bar.get("candle_parts")
        if isinstance(parts, dict) and set(parts) == {"body", "range", "upper", "lower"}:
            try:
                derived = base.candle_parts(bar)
                if all(parts[key] == round(derived[key], 6) for key in parts):
                    compact.pop("candle_parts")
            except (KeyError, TypeError, ValueError):
                pass  # Incomplete OHLC or finer source precision must stay explicit.
        return compact

    for key in ("m1_bars", "m5_bars", "m15_bars", "h1_bars"):
        if key in result:
            result[key] = [compact_bar(bar) for bar in result[key]]
    details = result.get("details", {})
    for detail_key, pack_key in (("strategy_contract", "strategy_contract"),
                                  ("strategy_fingerprint", "strategy_fingerprint"),
                                  ("strategy_assessments", "strategy_evidence")):
        if detail_key in details and details[detail_key] == result.get(pack_key):
            details.pop(detail_key)
    for key in ("session", "setup", "regime", "regime_text", "momentum"):
        if key in result and details.get(key) == result[key]:
            details.pop(key, None)
    windows = result.get("evidence_windows", {})
    for key, bars_key, count in (
        ("m15_structure_bar_times", "m15_bars", 24),
        ("breakout_bar_times", "m15_bars", 20),
        ("m5_rejection_bar_times", "m5_bars", 3),
    ):
        times = [bar.get("time") for bar in result.get(bars_key, [])[-count:]]
        if key in windows and windows[key] == times:
            windows.pop(key)
    # Stable rules/rubric precede changing quotes and bars for prefix caching.
    stable = ("symbol", "timeframe", "direction_timeframe", "bars_are_closed",
              "rules", "quality_rubric", "strategy_framework", "strategy_contract", "evidence_windows", "bar_closure")
    return {**{key: result[key] for key in stable if key in result},
            **{key: value for key, value in result.items() if key not in stable}}


def compact_review_pack(review):
    """AI-2 needs final prices and original thesis, not another quality scoring pass."""
    result = copy.deepcopy(review)
    result["market_evidence"] = compact_market_pack(result["market_evidence"])
    result["proposal"] = {key: value for key, value in result["proposal"].items()
                          if key not in ("details", "reasoning")}
    for key in ("quality_assessment", "quality_score", "quality_version", "model_reported_confidence"):
        result["ai1_raw_proposal"].pop(key, None)
    market = result["market_evidence"]
    for key in ("atr", "h1_trend", "m15_trend", "regime", "regime_text", "session", "setup",
                "local_action", "local_reasoning"):
        if key in market and result.get(key) == market[key]:
            result.pop(key, None)
    for key, rule in (("max_stop_abs", "max_stop_abs"), ("min_rr", "min_rr")):
        if key in result and result[key] == market.get("rules", {}).get(rule):
            result.pop(key)
    return {"market_evidence": result.pop("market_evidence"), **result}


def plan_feasibility(action, reference_entry, stop_loss, take_profit, quote, atr=None, ema21=None):
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
    if atr is not None:
        try:
            atr = float(atr)
            bid, ask = float(quote.get("bid")), float(quote.get("ask"))
            ema21 = float(ema21) if ema21 is not None else None
        except (TypeError, ValueError):
            result.update({"executable": False, "input_error": "当前Bid/Ask、ATR或EMA21缺失/无效"})
            return result
        if (not base.math.isfinite(atr) or atr <= 0
                or not all(base.math.isfinite(p) and p > 0 for p in (bid, ask)) or ask < bid
                or ema21 is not None and not base.math.isfinite(ema21)):
            result.update({"executable": False, "input_error": "当前Bid/Ask、ATR或EMA21缺失/无效"})
            return result
        generation_min_risk, max_risk = base.entry_risk_limits(atr)
        spread = ask - bid
        # Generation width is measured at the M15 reference. At execution, use
        # actual stop-trigger-side clearance, with one additional buffer for drift.
        min_clearance = 2 * base.stop_buffer(atr)
        min_execution_risk = min_clearance + spread
        rr_edge = (take_profit + base.MIN_REWARD_RISK * stop_loss) / (1 + base.MIN_REWARD_RISK)
        lower, upper = ((stop_loss + min_execution_risk, min(stop_loss + max_risk, rr_edge)) if action == "buy"
                        else (max(stop_loss - max_risk, rr_edge), stop_loss - min_execution_risk))
        clearance = sign * ((bid if action == "buy" else ask) - stop_loss)
        chase = abs(live_entry - ema21) / atr if ema21 is not None else None
        result.update({
            "entry_bounds": {"lower": round(lower, 6), "upper": round(upper, 6),
                             "source": "fixed_sl_tp_stop_clearance_and_rr"},
            "execution_version": MARKET_FRESHNESS_VERSION,
            "generation_min_risk": generation_min_risk,
            "min_execution_risk": min_execution_risk,
            "spread": round(spread, 6),
            "within_entry_bounds": lower - 1e-6 <= live_entry <= upper + 1e-6,
            "remaining_stop_space": round(clearance, 6) if base.math.isfinite(clearance) else None,
            "min_stop_clearance": min_clearance,
            "protection_space_passes": base.math.isfinite(clearance) and clearance + 1e-6 >= min_clearance,
            "chase_distance_atr": round(chase, 6) if chase is not None else None,
            "chase_passes": chase is None or base.math.isfinite(chase) and chase <= base.CHASE_MAX_ATR + 1e-6,
        })
        result["executable"] = all(result[key] for key in (
            "stop_within_cap", "rr_passes", "within_entry_bounds", "protection_space_passes", "chase_passes"))
    return result


class MarketSnapshotError(RuntimeError):
    """Fresh evidence is unavailable or the reviewed plan no longer applies."""


def quote_from_tick(tick):
    try:
        bid, ask = float(tick.bid), float(tick.ask)
    except (AttributeError, TypeError, ValueError):
        raise MarketSnapshotError("无法取得当前Bid/Ask，停止本轮，不复用旧报价")
    if not all(base.math.isfinite(p) and p > 0 for p in (bid, ask)) or ask < bid:
        raise MarketSnapshotError("当前Bid/Ask无效，停止本轮")
    return {"bid": bid, "ask": ask, "time": int(getattr(tick, "time", 0) or 0),
            "time_msc": int(getattr(tick, "time_msc", 0) or 0), "observed_at": time.time()}


def closed_context_times(df_m15, df_h1, df_m5):
    return {name: int(df["time"].iloc[-1]) for name, df in (
        ("m15_last_closed_bar_time", df_m15), ("h1_last_closed_bar_time", df_h1),
        ("m5_last_closed_bar_time", df_m5))}


def execution_rejection(facts):
    if not facts.get("available") or "executable" not in facts:
        return "当前报价或执行风险数据不足"
    if facts.get("input_error"):
        return facts["input_error"]
    if not facts.get("protection_space_passes"):
        return "扣除点差后的止损触发侧保护空间不足（剩余%s，至少%s）" % (
            facts.get("remaining_stop_space"), facts.get("min_stop_clearance"))
    if not facts.get("stop_within_cap"):
        return "当前止损距离无效或超过上限"
    if not facts.get("rr_passes"):
        return "当前固定SL/TP的实时盈亏比%s低于%s" % (facts.get("live_rr"), base.MIN_REWARD_RISK)
    if not facts.get("chase_passes"):
        return "当前报价偏离M15 EMA21超过追价上限"
    if not facts.get("within_entry_bounds"):
        return "现价已离开固定SL/TP和保护空间允许的执行区间"
    return "" if facts.get("executable") else "当前止损距离或盈亏比不符合硬限制"


def quality_rubric(analysis_mode=False):
    """A transparent ordinal scale, not a calibrated probability of profit."""
    return {
        "version": QUALITY_VERSION,
        "meaning": "entry_quality_not_win_probability",
        "level_points": QUALITY_LEVEL_POINTS,
        "dimensions": {
            "structure": ("按真实原始K线、位置及H1方向评价自主打法有效性，分盘菜单是参考；不要求固定Fib/建箱/首次事件。" if analysis_mode else "按strategy_contract评价本时段策略位置及有效性；满足该策略结构条件为adequate。Fib策略用现有容差/探入；浅回撤本身不是结构缺失。"),
            "trigger": ("真实原始已收盘K线所证自主打法触发可为adequate，无需固定检测器；analysis_checkpoint本身不能证明触发。" if analysis_mode else "本地M15或M5已收盘规则或strategy_evidence.closed_trigger命中可为adequate；更明确独立确认可为strong，不强制双周期。颜色、弱量不否定false_break。"),
            "protection": "止损保护命名结构且覆盖最近3根M5极值和缓冲、距离不超过上限为adequate；不能只因相对ATR较紧就认定无保护。",
            "target": "目标有给定依据、重要未克服障碍之前有空间且有效报价RR至少1.5为adequate；刚过1.5无需在其他项重复扣分。",
            "counter_evidence": "没有可核实反向证据为strong；普通量、浅回撤、H1旧影线、未证实过程极值等minor意见为adequate；独立及时的反向结构为material。",
        },
        "reservations_policy": "保留意见直接嵌在所属维度，一个cause/event仅归一项。level是唯一程度声明：adequate的保留为minor，weak为material，missing为blocking；strong不得有保留，weak/missing必须有保留。普通量/浅回撤不能weak/missing。Python生成ID/归属/程度，不由模型重复声明。",
        "reservation_causes": sorted(QUALITY_CAUSES),
        "proposal_policy": "硬伤或必要证据缺失则no；已成立的候选仅有minor意见时给出提案，最终分数及Python/AI2决定是否放行。不要将不确定未来获利当成必要字段缺失。",
        "quote_policy": ("自主候选reference_price是本次成功POST live_quote的BUY Ask/SELL Bid；coarse_reference_price与local_plan_feasibility保留原本地M15方案诊断。AI2/Python再核新报价。" if analysis_mode else "reference_price与live_quote来自不同时间，差异本身不是事实冲突；用live_quote核实当前可行性，AI2/Python还会复核执行。"),
        "reference_paths": ["/h1_trend", "/live_quote", "/local_plan_feasibility",
                            "/computed_evidence/fib", "/computed_evidence/m5_timing",
                            "/computed_evidence/price_action", "/computed_evidence/obstacles"],
        "reference_path_policy": "路径相对本次输入根对象；local_plan_feasibility是顶层，不能放进computed_evidence。引用列表中的实际方向和索引，不照抄示例占位符。",
    }


def _evidence_pointer_exists(pack, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False
    value = pack
    try:
        for token in pointer[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list) and (not token.isascii() or not token.isdigit()
                                           or len(token) > 1 and token.startswith("0")):
                return False
            value = value[int(token)] if isinstance(value, list) else value[token]
        return value is not None
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def normalize_ai1_quality(payload, pack):
    """Decode the declared contract; never repair legacy cards into trades."""
    selection, error = opportunity_selection(payload, pack)
    if error:
        return payload, error
    if not isinstance(payload, dict) or not isinstance(payload.get("quality_assessment"), dict):
        return _normalize_ai1_quality_v1(payload, pack)
    card = payload["quality_assessment"]
    if "wait_basis" in card:
        return payload, "AI-1字段层级错误: wait_basis必须位于顶层，不能嵌在quality_assessment内"
    version = card.get("version", LEGACY_QUALITY_VERSION)
    if version == LEGACY_QUALITY_VERSION:
        return _normalize_ai1_quality_v1(payload, pack)
    if version != QUALITY_VERSION:
        return payload, "AI-1质量评估版本无效: %s" % version
    if set(card) != {"version", "direction", "dimensions"}:
        return payload, "AI-1质量评估v2字段错误: 保留意见只能放入各维度"
    dimensions = card.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != set(QUALITY_DIMENSIONS):
        return payload, "AI-1质量评估无效: 五项缺失"
    decoded = copy.deepcopy(payload)
    decoded_card = {"version": QUALITY_VERSION, "direction": card.get("direction"),
                    "dimensions": {}, "reservations": []}
    severity_by_level = {"adequate": "minor", "weak": "material", "missing": "blocking"}
    for name in QUALITY_DIMENSIONS:
        item = dimensions[name]
        if not isinstance(item, dict) or set(item) != {"level", "reason", "evidence_refs", "reservations"}:
            return payload, "AI-1质量评估v2字段错误: %s" % name
        level, reservations = item.get("level"), item.get("reservations")
        if not isinstance(level, str) or level not in QUALITY_LEVEL_POINTS or not isinstance(reservations, list):
            return payload, "AI-1质量评估无效: %s等级或保留意见" % name
        if (level == "strong" and reservations or level in ("weak", "missing") and not reservations):
            return payload, "AI-1质量评估程度不符: %s缺少对应保留意见或strong含保留" % name
        converted = {"level": level, "reason": item["reason"], "evidence_refs": item["evidence_refs"],
                     "reservation_ids": []}
        for index, issue in enumerate(reservations, 1):
            if not isinstance(issue, dict) or set(issue) != {"cause", "event_group", "reason", "evidence_refs"}:
                return payload, "AI-1质量评估v2保留意见字段错误: %s/%s" % (name, index)
            if (not isinstance(issue["cause"], str) or issue["cause"] not in QUALITY_CAUSES
                    or issue["event_group"] is not None and not isinstance(issue["event_group"], str)
                    or not isinstance(issue["reason"], str) or not issue["reason"].strip()):
                return payload, "AI-1质量评估v2保留意见无效: %s/%s" % (name, index)
            if level in ("weak", "missing") and issue["cause"] in ("volume", "shallow_retrace"):
                return payload, "AI-1质量评估程度不符: 普通量/浅回撤仅是轻微意见，不能评%s" % level
            identifier = "%s:%s" % (name, index)
            converted["reservation_ids"].append(identifier)
            decoded_card["reservations"].append({**issue, "id": identifier, "dimension": name,
                                                "severity": severity_by_level[level]})
        decoded_card["dimensions"][name] = converted
    # Check paths before the shared validator to expose the precise invalid pointer.
    for item in list(dimensions.values()) + decoded_card["reservations"]:
        refs = item.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            return payload, "AI-1质量评估无效: evidence_refs必须是非空数组"
        invalid = [ref for ref in refs if not _evidence_pointer_exists(pack, ref)]
        if invalid:
            return payload, "AI-1质量评估证据路径无效: %s" % invalid
    decoded["quality_assessment"] = decoded_card
    normalized, error = _normalize_ai1_quality_v1(decoded, pack)
    if error:
        return payload, error
    normalized["quality_version"] = QUALITY_VERSION
    return normalized, ""


def has_confirmed_trigger(pack, direction):
    if "opportunities" in pack:
        identifier = pack.get("_quality_opportunity_id")
        candidate = next((item for item in pack["opportunities"] if item.get("opportunity_id") == identifier), None)
        trigger = (candidate or {}).get("strategy_evidence", {}).get(direction, {}).get("evidence", {}).get("closed_trigger", {})
        return trigger.get("confirmed") is True
    facts = pack.get("computed_evidence", {}).get("price_action", {})
    if any(facts.get(tf, {}).get(direction, {}).get("score", 0) > 0 for tf in ("M15", "M5")):
        return True
    trigger = pack.get("strategy_evidence", {}).get(direction, {}).get("evidence", {}).get("closed_trigger", {})
    return isinstance(trigger, dict) and trigger.get("confirmed") is True


def _normalize_ai1_quality_v1(payload, pack):
    """Validate the AI scorecard without promoting its action or inventing facts."""
    selection, error = opportunity_selection(payload, pack)
    if error:
        return payload, error
    if selection is not None:
        pack = {**pack, "_quality_opportunity_id": selection[1]["opportunity_id"]}
    if not isinstance(payload, dict):
        return payload, "AI-1质量评估缺失: 无有效JSON"
    card = payload.get("quality_assessment")
    if not isinstance(card, dict):
        return payload, "AI-1质量评估缺失: quality_assessment"
    if "wait_basis" in card:
        return payload, "AI-1字段层级错误: wait_basis必须位于顶层，不能嵌在quality_assessment内"
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
            if has_confirmed_trigger(pack, direction):
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
                if has_confirmed_trigger(pack, direction):
                    return payload, "AI-1事实误读: 已命中规则，不能以缺少触发观望"
    normalized = dict(payload)
    normalized["model_reported_confidence"] = payload.get("confidence")
    normalized["confidence"] = score
    normalized["quality_score"] = score
    normalized["quality_version"] = LEGACY_QUALITY_VERSION
    return normalized, ""


def ai1_result_status(parsed, quality_error, proposal):
    if quality_error:
        return "contract_invalid"
    if isinstance(parsed, dict) and parsed.get("action") == "no":
        return "watch"
    return "review_pending" if proposal is not None else "python_rejected"
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


def parse_json_object(text):
    """Extract one complete object without repairing incomplete trading decisions."""
    if not text or not str(text).strip():
        return None, "JSON正文为空"
    cleaned = str(text).strip().lstrip("\ufeff")
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            return None, "JSON代码块未闭合"
        cleaned = "\n".join(lines[1:-1]).strip()
    if cleaned.startswith("["):
        return None, "JSON顶层必须是对象，不能从数组中挑选交易"
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0:
        return None, "正文没有JSON对象"

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("重复JSON字段: %s" % key[:60])
            result[key] = value
        return result

    def finite_constant(value):
        raise ValueError("JSON包含非有限数值: %s" % value)

    def finite_float(value):
        number = float(value)
        return number if base.math.isfinite(number) else finite_constant(value)

    # Keep the incomplete suffix for an accurate EOF location instead of silently
    # selecting a valid nested object or synthesizing a missing closing brace.
    candidate = cleaned[start:] if cleaned.startswith("{") or end <= start else cleaned[start:end + 1]
    try:
        data = json.loads(candidate, object_pairs_hook=unique_keys, parse_constant=finite_constant, parse_float=finite_float)
    except json.JSONDecodeError as exc:
        return None, "JSON语法错误: %s（行%s 列%s，正文%s字符）" % (exc.msg, exc.lineno, exc.colno, len(cleaned))
    except ValueError as exc:
        return None, str(exc)
    return (data, "") if isinstance(data, dict) else (None, "JSON顶层必须是对象")


def extract_json_object(text):
    return parse_json_object(text)[0]


def _object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def response_json_schema(role):
    text = {"type": "string"}
    texts = {"type": "array", "items": text}
    price = {"anyOf": [{"type": "number"}, {"type": "null"}]}
    if role == "AI-1":
        reservation = _object_schema({
            "cause": {"type": "string", "enum": sorted(QUALITY_CAUSES)},
            "event_group": {"anyOf": [text, {"type": "null"}]},
            "reason": text, "evidence_refs": texts})
        dimension = _object_schema({"level": {"type": "string", "enum": list(QUALITY_LEVEL_POINTS)},
                                    "reason": text, "evidence_refs": texts,
                                    "reservations": {"type": "array", "items": reservation}})
        wait = _object_schema({"code": {"type": "string", "enum": ["hard_limit", "missing_structure", "missing_trigger",
                               "missing_data", "target_space", "material_counter_evidence"]},
                               "reason": text, "evidence_refs": texts})
        return _object_schema({
            "opportunity_id": text,
            "action": {"type": "string", "enum": ["buy", "sell", "no"]}, "stop_loss": price, "take_profit": price,
            "reasoning": text, "trigger_evidence": text, "invalidation": text, "target_basis": text,
            "fact_conflicts": texts, "unknowns": texts,
            "quality_assessment": _object_schema({"version": {"type": "string", "enum": [QUALITY_VERSION]},
                "direction": {"type": "string", "enum": ["buy", "sell"]},
                "dimensions": _object_schema({name: dimension for name in QUALITY_DIMENSIONS})}),
            "wait_basis": {"type": "array", "items": wait}})
    if role == "AI-2":
        return _object_schema({"decision": {"type": "string", "enum": ["APPROVE", "REJECT"]},
                               "defects": texts, "reasoning": text, "fact_conflicts": texts})
    if role == "AI-3":
        return _object_schema({"action": {"type": "string", "enum": ["hold", "tighten_sl", "close"]},
                               "ticket": {"type": "integer"}, "new_stop_loss": price, "reasoning": text})
    return None


def ai1_json_template():
    return {"opportunity_id": "<本次候选ID；仅无opportunities的legacy输入可空字符串>", "action": "no", "stop_loss": None, "take_profit": None,
            "reasoning": "<本次结论>", "trigger_evidence": "<实际触发或缺失>",
            "invalidation": "<失效条件>", "target_basis": "<目标依据>", "fact_conflicts": [], "unknowns": [],
            "quality_assessment": {"version": QUALITY_VERSION, "direction": "buy", "dimensions": {
                name: {"level": "adequate", "reason": "<本项证据结论>", "evidence_refs": ["/<本次真实路径>"],
                       "reservations": []} for name in QUALITY_DIMENSIONS}},
            "wait_basis": [{"code": "missing_trigger", "reason": "<实际等待条件>", "evidence_refs": ["/<本次真实路径>"]}]}


AI1_INSTRUCTIONS += (
    "严格输出一个完整JSON对象，闭合所有括号，不输出Markdown或分析过程。wait_basis必须在顶层，与quality_assessment并列，"
    "quality_assessment内部只有version/direction/dimensions，保留意见在各维度的reservations内。以下仅示范层级，示例方向/等级/理由/路径不可复用，须按实际证据重填；"
    "有交易提案时wait_basis为空数组。格式模板：" + json.dumps(ai1_json_template(), ensure_ascii=False, separators=(",", ":"))
)


def request_protocol(url):
    return "anthropic" if urlsplit(url).path.rstrip("/").endswith("/messages") else "chat"


def messages_supports_schema(url):
    """DeepSeek's official Messages route supports effort but ignores format."""
    parsed = urlsplit(url)
    return not (parsed.hostname == "api.deepseek.com"
                and parsed.path.rstrip("/") == "/anthropic/v1/messages")


def extract_chat_text(result):
    if not isinstance(result, dict):
        return ""
    if result.get("type") == "message":
        return "\n".join(block["text"].strip() for block in result.get("content", [])
                         if isinstance(block, dict) and block.get("type") == "text"
                         and isinstance(block.get("text"), str) and block["text"].strip())
    for choice in result.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            continue
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
    return {
        "id": result.get("id"),
        "model": result.get("model"),
        "choices": len(choices) if isinstance(choices, list) else 0,
        "finish_reason": chat_finish_reason(result),
        "usage": result.get("usage"),
    }

class AIRequestError(RuntimeError):
    def __init__(self, role, detail):
        self.role = role
        self.detail = detail
        super().__init__("%s: %s" % (role, detail))


def backup_chat_config(role, model, effort, url, api_key):
    primary = {"model": model, "effort": effort, "url": str(url or "").strip(),
               "key": str(api_key or "").strip()}
    role_id = {"AI-1": "1", "AI-2": "2", "AI-3": "3"}.get(role)
    if role_id is None:
        return primary
    names = {"url": "AI%s_BACKUP_URL" % role_id, "key": "AI%s_BACKUP_KEY" % role_id,
             "model": "FIB_PA_AI%s_BACKUP_MODEL" % role_id,
             "effort": "FIB_PA_AI%s_BACKUP_EFFORT" % role_id}
    return {field: os.getenv(name, "").strip().strip('"').strip("'").strip() or primary[field]
            for field, name in names.items()}


def chat_request_timeout(role, primary_timeout, using_backup=False):
    """A backup may use its own wait without creating another model route."""
    role_id = {"AI-1": "1", "AI-2": "2", "AI-3": "3"}.get(role)
    if not using_backup or role_id is None:
        return primary_timeout
    value = os.getenv("FIB_PA_AI%s_BACKUP_TIMEOUT" % role_id, "").strip().strip('"').strip("'")
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return primary_timeout
    return candidate if candidate > 0 else primary_timeout


def record_ai_usage(event):
    """One record per actual POST; a timeout has unknown usage, never zero cost."""
    try:
        now = datetime.now()
        row = {"timestamp": now.isoformat(timespec="milliseconds"), **event}
        with _USAGE_LOCK:
            os.makedirs(AI_USAGE_DIR, exist_ok=True)
            path = os.path.join(AI_USAGE_DIR, now.strftime("%Y-%m-%d") + ".jsonl")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        usage = row.get("usage") or {}
        base.logger.info(
            "%s 请求统计 call=%s decision=%s trigger=%s %s/%s %s outcome=%s elapsed=%.2fs input=%s output=%s reasoning=%s cache_read=%s cache_write=%s",
            row["role"], row["call_id"], row.get("decision_id"), row.get("trigger"), row["attempt"],
            row["request_budget"], row["channel"], row["outcome"], row["elapsed_sec"],
            usage.get("input_tokens"), usage.get("output_tokens"), usage.get("reasoning_tokens"),
            usage.get("cache_read_tokens"), usage.get("cache_write_tokens"),
        )
    except (OSError, TypeError, ValueError) as exc:
        base.logger.warning("AI用量记录失败: %s", type(exc).__name__)


def normalized_chat_usage(result):
    raw = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(raw, dict):
        return None
    input_details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    output_details = raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
    if not isinstance(input_details, dict):
        input_details = {}
    if not isinstance(output_details, dict):
        output_details = {}

    def number(*values):
        return next((value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)), None)

    if result.get("type") == "message":
        uncached = number(raw.get("input_tokens"))
        cache_read = number(raw.get("cache_read_input_tokens"))
        cache_write = number(raw.get("cache_creation_input_tokens"))
        # Native Messages input_tokens excludes cache reads and writes. Unknown
        # fields remain unknown; never guess a fully inclusive total from them.
        inclusive = sum((uncached, cache_read, cache_write)) if all(
            value is not None for value in (uncached, cache_read, cache_write)) else None
        output = number(raw.get("output_tokens"))
        return {"input_tokens": inclusive, "uncached_input_tokens": uncached, "output_tokens": output,
                "total_tokens": inclusive + output if inclusive is not None and output is not None else None,
                "reasoning_tokens": number(raw.get("reasoning_tokens")), "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write, "input_semantics": "anthropic_cache_exclusive"}

    # Providers differ on whether cached input is included; do not add it again.
    return {
        "input_tokens": number(raw.get("prompt_tokens"), raw.get("input_tokens")),
        "output_tokens": number(raw.get("completion_tokens"), raw.get("output_tokens")),
        "total_tokens": number(raw.get("total_tokens")),
        "reasoning_tokens": number(output_details.get("reasoning_tokens"), output_details.get("thinking_tokens"), raw.get("reasoning_tokens")),
        "cache_read_tokens": number(input_details.get("cached_tokens"), raw.get("cache_read_input_tokens"), raw.get("prompt_cache_hit_tokens")),
        "cache_write_tokens": number(input_details.get("cache_write_tokens"), raw.get("cache_creation_input_tokens")),
        "uncached_input_tokens": number(raw.get("prompt_cache_miss_tokens")),
        "input_semantics": "provider_reported",
    }


def chat_finish_reason(result):
    if isinstance(result, dict) and result.get("type") == "message":
        return result.get("stop_reason")
    choices = result.get("choices") if isinstance(result, dict) else None
    return choices[0].get("finish_reason") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None


def call_chat(role, model, effort, instructions, prompt, timeout, url, api_key, still_current=None,
              request_context=None, refresh_request=None):
    primary = {"model": model, "effort": effort, "url": str(url or "").strip(),
               "key": str(api_key or "").strip()}
    backup = backup_chat_config(role, model, effort, url, api_key)
    has_backup = backup != primary and bool(backup["url"] and backup["key"])
    active = primary
    using_backup = False
    low_retry = False
    omit_reasoning = False
    token_fallback_used = False
    read_failures = {"primary": 0, "backup": 0}
    context = request_context or {}
    call_id = request_trace_id(role)
    schema = response_json_schema(role)

    def task_alive():
        return still_current is None or still_current()

    def switch_to_backup():
        nonlocal active, using_backup, low_retry, omit_reasoning, token_fallback_used
        if not has_backup or using_backup:
            return False
        active, using_backup = backup, True
        low_retry, omit_reasoning = False, False
        token_fallback_used = False
        base.logger.warning("%s 主接口失败，切换备用 host=%s model=%s（共用%s次请求预算）",
                            role, urlsplit(active["url"]).hostname, active["model"], AI_HTTP_RETRIES)
        return True

    if not primary["key"] or not primary["url"]:
        if not switch_to_backup():
            raise RuntimeError("%s 缺少 %s" % (role, "API key" if not primary["key"] else "请求 URL"))

    last_problem = "接口未返回有效响应"
    transport_failures = 0
    attempted = 0
    for attempt in range(1, AI_HTTP_RETRIES + 1):
        channel = "backup" if using_backup else "primary"
        request_timeout = chat_request_timeout(role, timeout, using_backup)
        label = channel
        protocol = request_protocol(active["url"])
        option_key = (active["url"], active["model"])
        default_token_field = "max_completion_tokens" if active["model"].lower().startswith(("gpt-", "o1", "o3", "o4")) else "max_tokens"
        with _CHAT_OPTIONS_LOCK:
            options = dict(_CHAT_OPTIONS.get(option_key, {}))
        token_field = "max_tokens" if protocol == "anthropic" else options.get("token_field", default_token_field)
        json_mode = options.get("json_mode", "schema") if schema else "prompt"
        if protocol == "anthropic" and not messages_supports_schema(active["url"]):
            json_mode = "prompt"
        omit_reasoning = omit_reasoning or options.get("omit_reasoning", False)
        if low_retry:
            label += "/low_retry"
        if omit_reasoning:
            label += "/no_reasoning"
        if not task_alive():
            base.logger.info("%s 任务已过期或已停止，停止重试 (%s %s/%s)",
                             role, label, attempt, AI_HTTP_RETRIES)
            return ""
        snapshot = refresh_request() if refresh_request is not None else {}
        if not isinstance(snapshot, dict) or (refresh_request is not None and not snapshot.get("prompt")):
            raise MarketSnapshotError("无法构造新的请求快照，停止本轮，不发送旧提示词")
        request_prompt = snapshot.get("prompt", prompt)
        request_instructions = snapshot.get("instructions", instructions)
        if not isinstance(request_instructions, str) or not request_instructions.strip():
            raise MarketSnapshotError("请求角色规则为空或类型无效")
        if not task_alive():
            return ""
        if snapshot.get("snapshot_id"):
            request_quote = snapshot.get("quote") or {}
            base.logger.info("%s 请求行情 snapshot=%s bid=%s ask=%s tick_time=%s observed_at=%s (%s %s/%s)",
                             role, snapshot["snapshot_id"], request_quote.get("bid"), request_quote.get("ask"),
                             request_quote.get("time"), request_quote.get("observed_at"), label, attempt, AI_HTTP_RETRIES)
        data = {
            "model": active["model"],
            "messages": [
                {"role": "system", "content": request_instructions},
                {"role": "user", "content": request_prompt},
            ],
            "stream": False,
            token_field: AI_MAX_TOKENS.get(role, 4096),
        }
        this_effort = "low" if low_retry else active["effort"]
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + active["key"]}
        if protocol == "anthropic":
            data["system"] = request_instructions
            data["messages"] = [{"role": "user", "content": request_prompt}]
            output_config = {}
            if this_effort and not omit_reasoning:
                output_config["effort"] = this_effort
            if json_mode == "schema":
                output_config["format"] = {"type": "json_schema", "schema": schema}
            if output_config:
                data["output_config"] = output_config
            headers["anthropic-version"] = "2023-06-01"
            headers["x-api-key"] = active["key"]
        else:
            if this_effort and not omit_reasoning:
                data["reasoning_effort"] = this_effort
            if json_mode == "schema":
                data["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": role.replace("-", "_").lower() + "_decision", "strict": True, "schema": schema}}
            elif json_mode == "object":
                data["response_format"] = {"type": "json_object"}
        attempted = attempt
        started = time.monotonic()
        response_progress = {}

        def response_headers_received(response, *args, **kwargs):
            response_progress.update({
                "http_status": response.status_code,
                "headers_elapsed_sec": round(time.monotonic() - started, 3),
                "request_id": response.headers.get("x-request-id") or response.headers.get("request-id"),
            })
            return response

        def record_attempt(outcome, result=None, response=None, json_error=None):
            finish = chat_finish_reason(result)
            response_headers = getattr(response, "headers", {}) or {}
            response_id = result.get("id") if isinstance(result, dict) else None
            record_ai_usage({
                "role": role, "call_id": call_id, "decision_id": context.get("decision_id"),
                "trigger": context.get("trigger", "unspecified"), "bar_time": context.get("bar_time"),
                "attempt": attempt, "request_budget": AI_HTTP_RETRIES, "channel": channel,
                "host": urlsplit(active["url"]).hostname, "model": active["model"],
                "response_model": result.get("model") if isinstance(result, dict) else None,
                "request_id": response_headers.get("x-request-id") or response_headers.get("request-id") or response_progress.get("request_id"),
                "response_id": response_id, "http_status": getattr(response, "status_code", None) or response_progress.get("http_status"),
                "outcome": outcome, "elapsed_sec": round(time.monotonic() - started, 3),
                "read_timeout_sec": request_timeout,
                "headers_elapsed_sec": response_progress.get("headers_elapsed_sec"),
                "body_complete": response is not None,
                "prompt_chars": len(request_prompt), "instructions_chars": len(request_instructions), "profile_rules": snapshot.get("profile_rules"),
                "snapshot_id": snapshot.get("snapshot_id"), "request_quote": snapshot.get("quote"),
                "protocol": protocol, "response_contract_version": RESPONSE_CONTRACT_VERSION,
                "json_mode_sent": json_mode, "json_error": json_error,
                "reasoning_effort_sent": (data.get("output_config", {}).get("effort") if protocol == "anthropic"
                                          else data.get("reasoning_effort")),
                "token_limit": data[token_field], "token_limit_field": token_field,
                "finish_reason": finish, "usage": normalized_chat_usage(result),
                "cost": None,  # Rates and timeout billing belong to the provider, not guesses.
            })

        try:
            response = requests.post(active["url"], headers=headers, json=data, timeout=request_timeout,
                                     hooks={"response": response_headers_received})
        except (requests.Timeout, requests.ConnectionError) as exc:
            transport_failures += 1
            last_problem = "%s: %s" % (type(exc).__name__, exc)
            base.logger.warning("%s 请求超时或连接失败 (%s/%s, %s): %s",
                                role, attempt, AI_HTTP_RETRIES, label, last_problem)
            is_read_timeout = (isinstance(exc, requests.Timeout) and not isinstance(exc, requests.ConnectTimeout)
                               or "read timed out" in str(exc).lower())
            if is_read_timeout and response_progress:
                base.logger.warning("%s 已收到HTTP %s响应头(%.2fs)，正文读取超时，等待=%ss (%s)",
                                    role, response_progress["http_status"], response_progress["headers_elapsed_sec"],
                                    request_timeout, label)
            record_attempt("read_timeout" if is_read_timeout else "connection_failure")
            if not task_alive():
                return ""
            if attempt >= AI_HTTP_RETRIES:
                break
            if is_read_timeout:
                read_failures[channel] += 1
            switched = switch_to_backup()
            if is_read_timeout and not switched and read_failures[channel] > AI_READ_TIMEOUT_RETRIES:
                last_problem += "；读超时后上游可能继续生成，停止本通道重复补发（用量/费用未知）"
                break
            time.sleep(AI_HTTP_RETRY_PAUSE)
            continue
        response.encoding = "utf-8"
        try:
            result = response.json()
        except ValueError:
            result = None
        finish_reason = chat_finish_reason(result)
        if not task_alive():
            record_attempt("stale_response", result, response)
            base.logger.info("%s 请求返回时任务已过期，丢弃 (%s)", role, label)
            return ""
        if finish_reason in ("length", "max_tokens"):
            record_attempt("truncated", result, response)
            raise AIRequestError(role, "输出达到token上限，拒绝不完整决策且不重复付费请求；查看ai_usage调整本角色MAX_TOKENS")
        error_object = result.get("error") if isinstance(result, dict) else None
        error_text = (str(error_object.get("message", "")) + " " + str(error_object.get("param", ""))
                      if isinstance(error_object, dict) else response.text).lower()
        unsupported_parameter = any(word in error_text for word in (
            "unsupported", "unknown", "unrecognized", "not supported", "unexpected", "not allowed", "not permitted", "extra_forbidden"))
        if response.status_code == 400 and unsupported_parameter:
            format_rejected = (json_mode != "prompt" and any(name in error_text for name in (
                "response_format", "json_schema", "output_config.format")))
            whole_config_rejected = (protocol == "anthropic" and "output_config" in error_text
                                     and ".format" not in error_text and ".effort" not in error_text)
            if format_rejected or whole_config_rejected:
                record_attempt("unsupported_json_format", result, response)
                next_mode = "object" if (protocol == "chat" and json_mode == "schema"
                                        and "json_schema" in error_text and "response_format" not in error_text) else "prompt"
                with _CHAT_OPTIONS_LOCK:
                    route_options = _CHAT_OPTIONS.setdefault(option_key, {})
                    route_options["json_mode"] = next_mode
                    if whole_config_rejected:
                        route_options["omit_reasoning"] = True
                        omit_reasoning = True
                last_problem = "接口不支持结构化参数，降级为%s，继续保留本地契约校验和生成上限" % next_mode
                base.logger.warning("%s %s (%s)", role, last_problem, label)
                continue
        effort_rejected = (("reasoning" in error_text and "reasoning_effort" in data)
                           or (protocol == "anthropic" and "effort" in error_text
                               and "effort" in data.get("output_config", {})))
        if response.status_code == 400 and unsupported_parameter and effort_rejected:
            record_attempt("unsupported_reasoning", result, response)
            omit_reasoning = True
            with _CHAT_OPTIONS_LOCK:
                _CHAT_OPTIONS.setdefault(option_key, {})["omit_reasoning"] = True
            last_problem = "接口拒绝推理档位参数"
            base.logger.warning("%s 拒绝推理档位参数，去掉后重试 (%s)", role, label)
            continue
        if response.status_code == 400 and unsupported_parameter and token_field in error_text:
            record_attempt("unsupported_token_limit", result, response)
            if protocol == "anthropic":
                if switch_to_backup():
                    continue
                raise AIRequestError(role, "Messages接口拒绝必填max_tokens，停止请求，不移除上限或改用Chat字段")
            if token_fallback_used:
                raise AIRequestError(role, "接口拒绝两种token上限参数，停止请求，不能移除费用上限后无限生成")
            token_fallback_used = True
            alternate = "max_tokens" if token_field == "max_completion_tokens" else "max_completion_tokens"
            with _CHAT_OPTIONS_LOCK:
                _CHAT_OPTIONS.setdefault(option_key, {})["token_field"] = alternate
            last_problem = "接口拒绝%s，改用%s保留生成上限" % (token_field, alternate)
            base.logger.warning("%s %s (%s)", role, last_problem, label)
            continue
        if response.status_code != 200:
            last_problem = "HTTP %s" % response.status_code
            record_attempt("http_error", result, response)
        elif result is None:
            last_problem = "接口返回非JSON响应"
            record_attempt("non_json", result, response)
        else:
            usage = normalized_chat_usage(result) or {}
            reported_output = usage.get("output_tokens")
            if reported_output is not None and reported_output > data[token_field]:
                record_attempt("token_limit_exceeded", result, response)
                raise AIRequestError(role, "上游报告输出%s token超过发送上限%s，停止本轮；需核查中转参数映射/计量，已发生费用不能由客户端撤回"
                                     % (reported_output, data[token_field]))
            if finish_reason in ("refusal", "content_filter") or (protocol == "anthropic" and finish_reason not in ("end_turn", "stop_sequence")):
                record_attempt("non_final_decision", result, response)
                raise AIRequestError(role, "接口未返回可执行的最终决策，结束标记=%s，不重复生成" % finish_reason)
            text = extract_chat_text(result)
            json_error = parse_json_object(text)[1] if text else None
            record_attempt("invalid_decision_json" if text and json_error else "success" if text else "empty_output",
                           result, response, json_error)
            if text:
                if json_error:
                    base.logger.warning("%s 正文格式拒绝: %s (%s)，保留原文快照，不补括号或付费重审", role, json_error, label)
                if attempt > 1 and not json_error:
                    base.logger.info("%s 第 %s/%s 次请求收到可解析JSON (%s)，仍需策略校验", role, attempt, AI_HTTP_RETRIES, label)
                return text
            last_problem = "接口可见输出为空"
        base.logger.warning("%s 调用失败 (%s/%s, %s): %s", role, attempt, AI_HTTP_RETRIES, label, last_problem)
        if attempt >= AI_HTTP_RETRIES:
            break
        if switch_to_backup():
            continue
        if not low_retry:
            low_retry = True
            continue
        break

    if transport_failures == attempted:
        detail = "连续 %s 次超时或连接失败 (%s): %s" % (attempted, label, last_problem)
    else:
        detail = "%s次请求后失败 (%s): %s" % (attempted, label, last_problem)
    base.logger.error("%s %s", role, detail[:300])
    raise AIRequestError(role, detail[:300])


def prices_are_valid(action, entry, stop_loss, take_profit):
    if action == "buy":
        return stop_loss > 0 and stop_loss < entry and take_profit > entry
    if action == "sell":
        return stop_loss > entry and take_profit > 0 and take_profit < entry
    return False


def clamp_proposal(action, entry, stop_loss, take_profit, atr, target_r=1.8):
    if not math.isfinite(target_r) or not base.MIN_REWARD_RISK <= target_r <= 6:
        return 0., 0.
    min_risk, max_risk = base.entry_risk_limits(atr)
    if action == "buy":
        risk = entry - stop_loss
        if risk > max_risk:
            return 0.0, 0.0
        if risk <= 0 or risk < min_risk:
            stop_loss = entry - min_risk
        stop_loss = base.round_protective_stop(action, stop_loss)
        risk = entry - stop_loss
        if risk <= 0 or risk > max_risk + 0.001:
            return 0.0, 0.0
        far_tp = entry + target_r * risk
        if take_profit <= entry or take_profit > far_tp:
            take_profit = far_tp
        return stop_loss, round(float(take_profit), 2)

    risk = stop_loss - entry
    if risk > max_risk:
        return 0.0, 0.0
    if risk <= 0 or risk < min_risk:
        stop_loss = entry + min_risk
    stop_loss = base.round_protective_stop(action, stop_loss)
    risk = stop_loss - entry
    if risk <= 0 or risk > max_risk + 0.001:
        return 0.0, 0.0
    far_tp = entry - target_r * risk
    if take_profit >= entry or take_profit < far_tp:
        take_profit = far_tp
    return stop_loss, round(float(take_profit), 2)
def finalize_ai_proposal(payload, entry, atr, h1_trend, m15_trend, fib, min_confidence=None, df_m15=None, df_m5=None, regime=None, regime_location=None, pa_score=0, local_stop=0.0, local_target=0.0, setup="pullback", strategy_contract=None, strategy_evidence=None):
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
    target_r = (strategy_contract or {}).get("target_max_reference_r", 1.8)
    blockers = (strategy_evidence or {}).get(action, {}).get("blockers", [])
    if blockers:
        return None, "Python拒绝策略硬条件: " + "；".join(blockers)
    regime_filter = strategy_contract.get("regime_filter") if strategy_contract is not None else ("pullback" if setup == "pullback" else "none")
    if regime_filter == "pullback" and regime == "tight_range":
        return None, "Python拒绝: 窄震荡或方向不清，不交易"
    if regime_filter == "pullback" and regime == "wide_range" and regime_location is not None:
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

    analysis_mode = (strategy_contract or {}).get("analysis_mode", False)
    if analysis_mode and (not all(math.isfinite(v) for v in (entry, atr, stop_loss, take_profit))
                          or not prices_are_valid(action, entry, stop_loss, take_profit)
                          or abs(entry-stop_loss) > base.MAX_SL_ABS):
        return None, "Python拒绝: 自主AI止损止盈无效或止损超过8美元，不能补造方案"
    requested_take_profit = take_profit
    region_evidence = (strategy_evidence or {}).get(action, {}).get("evidence", {})
    is_region = (strategy_contract or {}).get("stop_reference") == "region"
    if is_region:
        try:
            required_sl = base.required_region_stop(action, entry, atr, df_m5, region_evidence,
                                                     (strategy_contract or {}).get("parameters"))
            if not math.isfinite(stop_loss) or stop_loss <= 0 or (action == "buy" and stop_loss > required_sl) or (action == "sell" and stop_loss < required_sl):
                stop_loss = required_sl
            stop_loss = base.round_protective_stop(action, stop_loss)
            risk = abs(entry - stop_loss)
            limit = base.region_target_limit(action, entry, atr, region_evidence)
            cap = entry + risk * target_r if action == "buy" else entry - risk * target_r
            if limit is not None:
                cap = min(cap, limit) if action == "buy" else max(cap, limit)
            if not math.isfinite(take_profit) or take_profit <= 0:
                take_profit = cap
            take_profit = min(take_profit, cap) if action == "buy" else max(take_profit, cap)
            take_profit = math.floor(take_profit * 100) / 100 if action == "buy" else math.ceil(take_profit * 100) / 100
            error = base.region_plan_error(action, entry, stop_loss, take_profit, atr, region_evidence)
            if error:
                return None, "Python拒绝: " + error
        except (base.StrategyError, TypeError, ValueError) as exc:
            return None, "Python拒绝区域保护: " + str(exc)
    risk = (entry - stop_loss) if action == "buy" else (stop_loss - entry)
    if is_region:
        pass  # Region math above is authoritative; no Fib or fallback reconstruction.
    elif prices_are_valid(action, entry, stop_loss, take_profit) and 0 < risk <= base.MAX_SL_ABS:
        stop_loss, take_profit = clamp_proposal(action, entry, stop_loss, take_profit, atr, target_r)
    else:
        stop_loss, take_profit = 0.0, 0.0

    entry_price_source = "confirmed_closed_m5_close" if (strategy_contract or {}).get("entry_reference") == "closed_m5" else "previous_closed_m15_close"
    if (strategy_contract or {}).get("stop_reference") == "m5_extreme":
        fib = None
        extreme = base.recent_m5_extreme(df_m5, action)
        if not base.m5_extreme_on_stop_side(action, entry, extreme):
            return None, "Python拒绝: 研究策略缺少最近三根M5保护极值"
        required_sl, _ = base.calculate_targets(action, entry, atr, None, extreme, extreme, target_r=target_r)
        if required_sl <= 0:
            return None, "Python拒绝: 研究策略结构止损无法放入8美元"
        if stop_loss <= 0 or (action == "buy" and stop_loss > required_sl) or (action == "sell" and stop_loss < required_sl):
            stop_loss = required_sl
        stop_loss, take_profit = clamp_proposal(action, entry, stop_loss, requested_take_profit, atr, target_r)
    if analysis_mode and not prices_are_valid(action, entry, stop_loss, take_profit):
        return None, "Python拒绝: 自主AI保护无法定稿"
    if not prices_are_valid(action, entry, stop_loss, take_profit):
        replaced = False
        if prices_are_valid(action, entry, local_stop, local_target):
            local_risk = (entry - local_stop) if action == "buy" else (local_stop - entry)
            if 0 < local_risk <= base.MAX_SL_ABS:
                stop_loss, take_profit = float(local_stop), float(local_target)
                replaced = True
                reasoning = (reasoning + "；AI止损放不下，改用本地结构止损").strip("；")
        if not replaced:
            timed = base.m5_rejection_anchor(df_m5, action, atr) if df_m5 is not None and (strategy_contract or {}).get("stop_reference") != "m5_extreme" else None
            if timed is None:
                return None, "Python拒绝: 结构止损超过小资金上限 %.2f" % base.MAX_SL_ABS
            entry, setup_price = timed
            entry_price_source = "m5_rejection_fallback_close"
            stop_loss, take_profit = base.calculate_targets(action, entry, atr, fib, setup_price, target_r=target_r)
            if not prices_are_valid(action, entry, stop_loss, take_profit):
                return None, "Python拒绝: 结构止损超过小资金上限 %.2f" % base.MAX_SL_ABS
            reasoning = (reasoning + "；AI止损放不下，改用仍在%g美元内的M5极值" % base.MAX_SL_ABS).strip("；")

    if not prices_are_valid(action, entry, stop_loss, take_profit):
        return None, "Python拒绝: 止损止盈方向非法或超过 %.2f 美元上限" % base.MAX_SL_ABS
    guarded_sl, guarded_tp, guard_note = (stop_loss, take_profit, "") if is_region else base.reject_inside_m5_wick(
        action, entry, stop_loss, take_profit, atr, fib, df_m5, target_r=target_r
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
    if df_m5 is not None and not analysis_mode:
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
            "opportunity_id": details.get("opportunity_id"),
            "event_id": details.get("event_id"),
            "setup": details.get("setup"),
            "session": details.get("session"),
            "strategy_contract": copy.deepcopy(details.get("strategy_contract")),
            "strategy_fingerprint": details.get("strategy_fingerprint"),
            "strategy_evidence": copy.deepcopy(details.get("strategy_evidence", details.get("strategy_assessments"))),
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
            "reference_price_metadata": copy.deepcopy(details.get("reference_price_metadata")),
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

    def get_live_quote(self):
        with self._mt5_lock:
            return quote_from_tick(base.mt5.symbol_info_tick(base.SYMBOL))

    def ensure_entry_account_ready(self, contract):
        if not contract.get("demo_only"):
            return
        account = base.mt5.account_info()
        if account is None or getattr(account, "trade_mode", None) != base.mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise MarketSnapshotError("研究策略仅允许模拟账户，不请求AI或下单")
        if contract.get("analysis_mode"):
            positions = base.mt5.positions_get(symbol=base.SYMBOL)
            if positions is None or len(positions) >= base.MAX_POSITIONS:
                raise MarketSnapshotError("自主分析持仓状态未知或已达到单仓上限，AI not called")

    def collect_entry_snapshot(self, decision_id):
        df_m15, df_h1, df_m5 = self.get_context()
        local_result = self.evaluate_context(df_m15, df_h1, df_m5)
        if (local_result.details or {}).get("strategy_error"):
            raise MarketSnapshotError(local_result.details["strategy_error"])
        self.ensure_entry_account_ready((local_result.details or {}).get("strategy_contract", {}))
        assessments = (local_result.details or {}).get("strategy_assessments", {})
        if ("opportunities" not in (local_result.details or {}) and (local_result.details or {}).get("strategy_contract", {}).get("one_entry_per_event")
                and assessments and all(item.get("blockers") for item in assessments.values())):
            raise MarketSnapshotError("研究策略本轮无合法首次事件，不请求AI: " + local_result.reasoning)
        pack, fib, atr, price = build_feature_pack(
            df_m15, df_h1, df_m5, local_result, quote=self.get_live_quote(), decision_id=decision_id)
        pack["snapshot_id"] = request_trace_id("%s-ai1" % decision_id)
        return df_m15, df_h1, df_m5, local_result, pack, fib, atr, price

    def ensure_strategy_current(self, pack):
        fingerprint = pack.get("strategy_fingerprint")
        if fingerprint:
            try:
                base.STRATEGIES.require_current(fingerprint)
            except base.StrategyError as exc:
                raise MarketSnapshotError(str(exc)) from exc

    def ensure_review_bars_current(self, expected):
        current = closed_context_times(*self.get_context())
        if any(current[name] != expected.get(name) for name in current):
            raise MarketSnapshotError("已有新的已收盘H1/M15/M5，旧提案停止审核/执行，等待下一轮重新分析")

    def prepare_review_request(self, proposal, ai1_pack, parsed, reason, decision_id, trigger):
        self.ensure_strategy_current(ai1_pack)
        contract = (proposal.details or {}).get("strategy_contract", {})
        if contract.get("analysis_mode"):
            self.ensure_entry_account_ready(contract)
        self.ensure_review_bars_current(ai1_pack["bar_closure"])
        strategy_error = self.validate_strategy_entry(proposal)
        if strategy_error:
            raise MarketSnapshotError("AI-2请求前策略复核拒绝: " + strategy_error)
        market = copy.deepcopy(ai1_pack)
        market["live_quote"] = self.get_live_quote()
        market["snapshot_id"] = request_trace_id("%s-ai2" % decision_id)
        reference = float(proposal.details.get("reference_entry_price") or ai1_pack["reference_price"]["price"])
        entry = float(proposal.details.get("entry_price") or reference)
        atr = proposal.details.get("execution_guard", {}).get("atr", market["atr"])
        facts = plan_feasibility(proposal.action, entry, proposal.stop_loss, proposal.take_profit,
                                 market["live_quote"], atr, market.get("m15_ema21"))
        error = execution_rejection(facts)
        if error:
            append_decision_snapshot(decision_id, "execution_rejected", {"reason": error, "feasibility": facts})
            raise MarketSnapshotError(error)
        local_reference = market.get("local_plan_feasibility", {}).get("reference_entry", market["reference_price"]["price"])
        market["local_plan_feasibility"] = plan_feasibility(
            market["local_action"], local_reference, market["local_stop_loss"],
            market["local_take_profit"], market["live_quote"], atr, market.get("m15_ema21"))
        review = {
            "proposal": proposal.to_decision(), "ai1_raw_proposal": parsed,
            "python_finalization": {"reference_entry_price": reference,
                                    "final_entry_price": entry, "final_stop_loss": proposal.stop_loss,
                                    "final_take_profit": proposal.take_profit, "reason": reason},
            "decision_id": decision_id, "reference_entry_price": reference,
            "entry_price": entry, "final_plan_feasibility": facts,
            "ai1_snapshot": {"snapshot_id": ai1_pack.get("snapshot_id"), "quote": ai1_pack["live_quote"],
                             "bar_closure": ai1_pack["bar_closure"]},
            "atr": atr, "h1_trend": market["h1_trend"], "m15_trend": market["m15_trend"],
            "regime": market.get("regime"), "regime_text": market.get("regime_text"),
            "session": market.get("session"), "setup": market.get("setup"),
            "max_stop_abs": base.MAX_SL_ABS, "min_rr": base.MIN_REWARD_RISK,
            "local_action": market["local_action"], "local_reasoning": market["local_reasoning"],
            "market_evidence": market,
        }
        review = compact_review_pack(review)
        proposal.details["ai2_snapshot_id"] = market["snapshot_id"]
        append_decision_snapshot(decision_id, "ai2_input", {"review": review, "trigger": trigger})
        return {"prompt": prompt_json("审核当前可执行的最终订单，只输出契约JSON。", review),
                "snapshot_id": market["snapshot_id"], "quote": market["live_quote"],
                "instructions": role_instructions("AI-2", [proposal.details.get("strategy_contract", {})]),
                "profile_rules": role_rules("AI-2", [proposal.details.get("strategy_contract", {})])}

    def validate_execution_prices(self, result, tick):
        research_error = base.research_execution_error(result, tick)
        if research_error:
            return research_error
        guard = (result.details or {}).get("execution_guard")
        if guard is None:
            return ""
        try:
            quote = quote_from_tick(tick)
            entry = float(result.details.get("entry_price") or quote["ask" if result.action == "buy" else "bid"])
            facts = plan_feasibility(result.action, entry, result.stop_loss, result.take_profit,
                                     quote, guard["atr"], guard.get("ema21"))
            result.details["last_execution_check"] = {"quote": quote, "feasibility": facts}
            return execution_rejection(facts)
        except (MarketSnapshotError, KeyError, TypeError, ValueError) as exc:
            return "执行数据无效: %s" % exc

    def place_order(self, result):
        with self._mt5_lock:
            guard = (result.details or {}).get("execution_guard", {})
            if guard.get("bar_closure"):
                try:
                    self.ensure_review_bars_current(guard["bar_closure"])
                except (MarketSnapshotError, RuntimeError) as exc:
                    base.logger.info("执行复核拒绝下单: %s", exc)
                    return False
            placed = super().place_order(result)
            if not placed:
                return False
            positions = self.get_positions()
            ticket = int(getattr(self, "last_order_ticket", 0) or 0)
            position = next(
                (item for item in positions if int(getattr(item, "ticket", 0) or 0) == ticket),
                None,
            )
            evidence = (result.details or {}).get("strategy_evidence", {}).get(result.action, {}).get("evidence", {})
            event_id = evidence.get("event_id")
            if event_id:
                token = base.execution_token(event_id, base.SYMBOL, base.MAGIC, result.action)
                def exact_identity(item):
                    return (getattr(item, "comment", "") in (token, "_SR_" + token)
                            and getattr(item, "symbol", None) == base.SYMBOL
                            and getattr(item, "magic", None) == base.MAGIC
                            and getattr(item, "type", None) == (base.mt5.POSITION_TYPE_BUY if result.action == "buy" else base.mt5.POSITION_TYPE_SELL))
                if position is not None and not exact_identity(position):
                    position = None
                if position is None:
                    state = self.strategy_ledger.snapshot()
                    matches = [item for item in positions if exact_identity(item)
                               and (base.recovery_event(item, state, base.SYMBOL, base.MAGIC) or (None,))[0] == event_id]
                    position = matches[0] if len(matches) == 1 else None
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
            local_result = self.evaluate_context(df_m15, df_h1, df_m5)
        except Exception as exc:
            base.logger.error("M5时机评估异常: %s: %s" % (type(exc).__name__, exc))
            return
        opportunities = (getattr(local_result, "details", None) or {}).get("opportunities")
        if opportunities is not None:
            append_decision_snapshot("%s-m5-local" % bar_time, "local_evaluation", {
                "local_result": local_result.to_decision(), "trigger": "M5"})
        adaptive = any(base.opportunity_eligible(item) and item["strategy_contract"].get("analysis_mode")
                       for item in opportunities or [])
        if not adaptive and (local_result.action not in ("buy", "sell") or local_result.confidence < base.CONFIDENCE_THRESHOLD):
            reject = ""
            if "拒绝:" in (local_result.reasoning or ""):
                reject = " 拒绝:" + local_result.reasoning.split("拒绝:", 1)[1].strip().replace("\n", " ")[:160]
            assessments = (local_result.details or {}).get("strategy_assessments", {})
            blockers = {side: assessments.get(side, {}).get("blockers", []) for side in ("buy", "sell")}
            base.logger.info(
                "AI not called M5本地粗筛拒绝 action=%s confidence=%s BUY=%s SELL=%s%s 策略阻断BUY=%s SELL=%s 理由=%s"
                % (
                    local_result.action,
                    local_result.confidence,
                    local_result.score_buy,
                    local_result.score_sell,
                    reject,
                    blockers["buy"],
                    blockers["sell"],
                    (local_result.reasoning or "").replace("\n", " ")[:160],
                )
            )
            return
        if adaptive:
            base.logger.info("%s M5自主粗筛派发 profile=%s local_action=%s score=%s，不要求本地方案可执行",
                             base.ADAPTIVE_SESSION_VERSION, local_result.details.get("setup"),
                             local_result.action, local_result.confidence)
        else:
            base.logger.info(
                "M5拒绝仍在%g美元内，派发开仓 action=%s score=%s SL=%s"
                % (base.MAX_SL_ABS, local_result.action, local_result.confidence, local_result.stop_loss)
            )
        self.dispatch_entry(bar_time, trigger="M5")

    def dispatch_entry(self, bar_time, trigger="M15"):
        seq = self.claim_task("entry", bar_time, ENTRY_TAKEOVER_SEC)
        if seq is None:
            base.logger.info("AI-1/2 仍在进行，跳过本根K线")
            return
        base.logger.info("AI-1 已派发分析线程 bar=%s seq=%s trigger=%s", bar_time, seq, trigger)
        threading.Thread(target=self.entry_task, args=(bar_time, seq, trigger), daemon=True).start()

    def claim_analysis_scope(self, pack):
        """Ordinary dispatch only; compatibility/failover refresh never claims again."""
        scopes = analysis_scope_keys(pack)
        if not scopes:
            return True
        with self._entry_lock:
            previous = getattr(self, "_analysis_request_scopes", set())
            if scopes & previous:
                base.logger.info("AI not called 同分析检查点已经派发 profiles=%s", sorted(scopes))
                return False
            # Keep only current source scopes; no persistent prompt/history growth.
            self._analysis_request_scopes = scopes
        base.logger.info("%s 实际分析派发 profiles/source=%s", base.ADAPTIVE_SESSION_VERSION, sorted(scopes))
        return True

    def entry_task(self, bar_time, seq, trigger="M15"):
        ai_started = False
        try:
            if self._stop.is_set():
                return
            decision_id = "%s-%s" % (bar_time, seq)
            df_m15, df_h1, df_m5, local_result, pack, fib, atr, price = self.collect_entry_snapshot(decision_id)
            append_decision_snapshot(
                decision_id,
                "entry_candidate",
                {
                    "market_evidence": pack,
                    "local_result": local_result.to_decision(),
                    "trigger": trigger,
                    "cost_control_version": COST_CONTROL_VERSION,
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
            if self._stop.is_set() or self.current_position_count() >= base.MAX_POSITIONS:
                return
            if "opportunities" in pack and not pack["opportunities"]:
                base.logger.info("AI not called 本地粗筛无合法候选 reason=%s blockers=%s", local_result.reasoning, pack.get("opportunity_blockers"))
                append_decision_snapshot(decision_id, "local_prefilter", {"reason": "no_eligible_structural_opportunity",
                    "status": "AI not called", "local_reasoning": local_result.reasoning, "blockers": pack.get("opportunity_blockers")})
                return
            if not self.entry_is_current(seq, bar_time):
                base.logger.info("AI-1 任务已过期，取消请求 seq=%s" % seq)
                return

            request_pack = compact_market_pack(pack)
            scope_claimed = False
            def refresh_ai1_request():
                nonlocal df_m15, df_h1, df_m5, local_result, pack, fib, atr, price, request_pack, scope_claimed
                df_m15, df_h1, df_m5, local_result, pack, fib, atr, price = self.collect_entry_snapshot(decision_id)
                if "opportunities" in pack and not pack["opportunities"]:
                    append_decision_snapshot(decision_id, "local_prefilter", {"reason": "no_eligible_structural_opportunity",
                        "status": "AI not called for this refreshed scope", "local_reasoning": local_result.reasoning,
                        "blockers": pack.get("opportunity_blockers")})
                    raise MarketSnapshotError("无合法结构候选，本轮本地观望，不请求AI")
                request_pack = compact_market_pack(pack)
                snapshot = {"prompt": prompt_json("分析本次已收盘行情及当前报价，只输出契约JSON。", request_pack),
                            "snapshot_id": pack["snapshot_id"], "quote": pack["live_quote"],
                            "instructions": role_instructions("AI-1", request_contracts(pack)),
                            "profile_rules": role_rules("AI-1", request_contracts(pack))}
                if not scope_claimed:
                    if not self.claim_analysis_scope(pack):
                        raise MarketSnapshotError("同分析检查点已经派发，AI not called")
                    scope_claimed = True
                else:
                    scopes = analysis_scope_keys(pack)
                    if scopes:
                        with self._entry_lock:
                            self._analysis_request_scopes = scopes
                append_decision_snapshot(decision_id, "ai1_input", {
                    "market_evidence": pack, "local_result": local_result.to_decision(), "trigger": trigger,
                    "cost_control_version": COST_CONTROL_VERSION, "freshness_version": MARKET_FRESHNESS_VERSION})
                return snapshot

            ai_started = True
            raw = call_chat(
                "AI-1",
                AI1_MODEL,
                AI1_EFFORT,
                AI1_INSTRUCTIONS,
                prompt_json(
                    "分析本轮已收盘行情，只输出契约JSON。",
                    pack,
                ),
                AI1_TIMEOUT,
                AI1_URL,
                AI1_KEY,
                still_current=lambda: self.entry_is_current(seq, bar_time) and not self._stop.is_set(),
                request_context={"decision_id": decision_id, "bar_time": bar_time, "trigger": trigger},
                refresh_request=refresh_ai1_request,
            )
            if not self.entry_is_current(seq, bar_time) or self._stop.is_set():
                base.logger.info("AI-1 返回已过期，丢弃 seq=%s" % seq)
                return
            if not raw:
                append_decision_snapshot(decision_id, "ai1_empty", {"reason": "no_visible_json"})
                self.alert_api_failure("AI-1", "没有可见 JSON")
                return
            self.ensure_strategy_current(pack)
            entry_price = float((local_result.details or {}).get("entry_price") or price)
            parsed_raw, json_error = parse_json_object(raw)
            parsed, quality_error = (None, "AI-1输出格式错误: " + json_error) if json_error else normalize_ai1_quality(parsed_raw, request_pack)
            if not quality_error and "opportunities" in pack:
                selection, selection_error = opportunity_selection(parsed, pack)
                if selection_error:
                    quality_error = selection_error
                else:
                    pack = selected_opportunity_pack(pack, selection)
                    price = entry_price = float(pack["reference_price"]["price"])
                    fib = None if pack["strategy_contract"].get("stop_reference") in ("region", "m5_extreme") else base.entry_fib_structure(df_m15, pack["h1_trend"], atr)
            proposal, reason = None, quality_error
            if not quality_error:
                proposal, reason = finalize_ai_proposal(
                    parsed, entry_price, atr, pack["h1_trend"], pack["m15_trend"], fib,
                    df_m15=df_m15, df_m5=df_m5, regime=pack.get("regime"),
                    regime_location=pack.get("details", {}).get("location"),
                    pa_score=int(pack.get("details", {}).get("pa_score") or 0),
                    local_stop=pack.get("local_stop_loss", local_result.stop_loss), local_target=pack.get("local_take_profit", local_result.take_profit),
                    setup=pack.get("setup", "pullback"),
                    strategy_contract=pack.get("strategy_contract") or None,
                    strategy_evidence=pack.get("strategy_evidence"),
                )
            ai_confidence = None
            if isinstance(parsed, dict) and not quality_error:
                ai_confidence = normalize_confidence(parsed.get("confidence", 0))
            result_status = ai1_result_status(parsed, quality_error, proposal)
            append_decision_snapshot(
                decision_id,
                "ai1_result",
                {
                    "raw": raw,
                    "parsed": parsed,
                    "quality_error": quality_error,
                    "json_error": json_error or None,
                    "status": result_status,
                    "quality_version": parsed.get("quality_version") if isinstance(parsed, dict) else None,
                    "proposal": proposal.to_decision() if proposal is not None else None,
                    "python_reason": reason,
                },
            )
            action_line = ("AI-1输出无效（原动作: %s）" if quality_error else "AI-1动作: %s") % format_ai_action(parsed)
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
                "AI-1 结果 decision=%s status=%s local_action=%s %s: %s | %s"
                % (decision_id, result_status, local_result.action, action_line, reason, confidence_line)
            )
            if not quality_error:
                card = parsed["quality_assessment"]
                base.logger.info("AI-1质量明细 decision=%s version=%s grades=%s reservations=%s wait_basis=%s",
                                 decision_id, parsed["quality_version"],
                                 {key: item["level"] for key, item in card["dimensions"].items()},
                                 card["reservations"], parsed.get("wait_basis", []))
            if proposal is None:
                if "观望" not in reason and "未给出" not in reason:
                    self.notify_lines(
                        ("⚠️ %s AI-1输出契约错误" if quality_error else "✋ %s AI-1/Python 未放行") % base.SYMBOL,
                        [confidence_line, reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                    )
                return
            proposal.score_buy = local_result.score_buy
            proposal.score_sell = local_result.score_sell
            if pack.get("opportunity_id"):
                proposal.details["entry_price_source"] = pack["reference_price"].get("source")
            requested_sl = parsed.get("stop_loss") if isinstance(parsed, dict) else None
            requested_tp = parsed.get("take_profit") if isinstance(parsed, dict) else None
            proposal.details.update(
                {
                    "source": "ai1",
                    "opportunity_id": pack.get("opportunity_id"),
                    "event_id": pack.get("strategy_evidence", {}).get(proposal.action, {}).get("evidence", {}).get("event_id"),
                    "reference_price": entry_price,
                    "reference_price_metadata": copy.deepcopy(pack.get("reference_price")),
                    "decision_id": decision_id,
                    "local_action_before_ai": local_result.action,
                    "local_confidence": local_result.confidence,
                    "session": pack.get("session"),
                    "setup": pack.get("setup"),
                    "strategy_fingerprint": pack.get("strategy_fingerprint"),
                    "strategy_contract": copy.deepcopy(pack.get("strategy_contract")),
                    "strategy_evidence": copy.deepcopy(pack.get("strategy_evidence")),
                    "entry_guard_atr": atr,
                    "entry_guard_ema21": pack.get("m15_ema21"),
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
            if pack.get("strategy_contract", {}).get("analysis_mode"):
                proposal.details["ai1_source_binding"] = {
                    "checkpoint": copy.deepcopy(pack["strategy_evidence"][proposal.action]["evidence"].get("host_verified_analysis_checkpoint")),
                    "snapshot_id": pack.get("snapshot_id"), "fingerprint": pack.get("strategy_fingerprint"),
                    "reference_price": copy.deepcopy(pack["reference_price"])}
            final_entry_price = float(proposal.details.get("entry_price") or entry_price)
            proposal.details["execution_guard"] = {
                "atr": atr, "ema21": pack.get("m15_ema21"), "bar_closure": pack["bar_closure"],
                "ai1_snapshot_id": pack.get("snapshot_id"), "reference_entry_price": final_entry_price,
            }
            review_raw = call_chat(
                "AI-2",
                AI2_MODEL,
                AI2_EFFORT,
                AI2_INSTRUCTIONS,
                "",
                AI2_TIMEOUT,
                AI2_URL,
                AI2_KEY,
                still_current=lambda: self.entry_is_current(seq, bar_time) and not self._stop.is_set(),
                request_context={"decision_id": decision_id, "bar_time": bar_time, "trigger": trigger},
                refresh_request=lambda: self.prepare_review_request(proposal, pack, parsed, reason, decision_id, trigger),
            )
            if not self.entry_is_current(seq, bar_time) or self._stop.is_set():
                base.logger.info("AI-2 返回已过期，丢弃 seq=%s" % seq)
                return
            if not review_raw:
                append_decision_snapshot(decision_id, "ai2_empty", {"proposal": proposal.to_decision()})
                self.alert_api_failure("AI-2", "没有可见 JSON，拒绝开仓")
                return
            self.ensure_strategy_current(pack)
            review_payload, review_json_error = parse_json_object(review_raw)
            allowed, review_reason = (False, "AI-2输出格式错误: " + review_json_error) if review_json_error else ai2_allows(review_payload)
            append_decision_snapshot(
                decision_id,
                "ai2_result",
                {
                    "raw": review_raw,
                    "parsed": review_payload,
                    "allowed": allowed,
                    "reason": review_reason,
                    "json_error": review_json_error or None,
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
        except MarketSnapshotError as exc:
            append_decision_snapshot(decision_id, "entry_stopped", {"reason": str(exc)})
            base.logger.info("%s: %s", "行情刷新或执行复核停止本轮" if ai_started else "AI not called 本地粗筛/来源阻断", exc)
        except AIRequestError as exc:
            append_decision_snapshot(decision_id, "ai_request_failed", {"role": exc.role, "reason": exc.detail})
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

    def collect_ai3_snapshot(self, expected_tickets=None):
        positions = self.get_positions()
        if expected_tickets is not None:
            positions = [p for p in positions if int(p.ticket) in expected_tickets]
            if {int(p.ticket) for p in positions} != expected_tickets:
                raise MarketSnapshotError("原持仓已消失，停止旧AI-3任务，不接管新的持仓")
        if not positions:
            return None
        df_m1 = self.get_rates(base.mt5.TIMEFRAME_M1, 40, shift=1)
        df_m5 = self.get_rates(base.mt5.TIMEFRAME_M5, 40, shift=1)
        df_m15 = self.get_rates(base.mt5.TIMEFRAME_M15, 40, shift=1)
        df_h1 = self.get_rates(base.mt5.TIMEFRAME_H1, 40, shift=1)
        tick = None
        with self._mt5_lock:
            tick = base.mt5.symbol_info_tick(base.SYMBOL)
        quote = quote_from_tick(tick)
        payload = {
            "symbol": base.SYMBOL,
            "snapshot_id": request_trace_id("ai3"),
            "live_quote": quote,
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
        payload = compact_market_pack(payload)
        for item in payload["positions"]:
            # The complete card remains persisted; guardian uses the entry thesis.
            item["entry_context"].pop("quality_assessment", None)
        return {"prompt": prompt_json(
            '请只输出 JSON: {"action":"hold|tighten_sl|close","ticket":0,"new_stop_loss":0,"reasoning":"..."}', payload),
                "snapshot_id": payload["snapshot_id"], "quote": quote,
                "tickets": {int(item["ticket"]) for item in payload["positions"]},
                "instructions": role_instructions("AI-3", [item["entry_context"].get("strategy_contract") or {} for item in payload["positions"]]),
                "profile_rules": role_rules("AI-3", [item["entry_context"].get("strategy_contract") or {} for item in payload["positions"]])}

    def ai3_task(self, bar_time, seq):
        try:
            if self._stop.is_set():
                return
            initial = self.collect_ai3_snapshot()
            if initial is None:
                return
            expected_tickets = initial["tickets"]
            raw = call_chat(
                "AI-3",
                AI3_MODEL,
                AI3_EFFORT,
                AI3_INSTRUCTIONS,
                initial["prompt"],
                AI3_TIMEOUT,
                AI3_URL,
                AI3_KEY,
                still_current=lambda: self.ai3_is_current(seq, bar_time) and not self._stop.is_set(),
                request_context={"decision_id": "ai3-%s-%s" % (bar_time, seq), "bar_time": bar_time, "trigger": "M1"},
                refresh_request=lambda: self.collect_ai3_snapshot(expected_tickets),
            )
            if not self.ai3_is_current(seq, bar_time) or self._stop.is_set():
                base.logger.info("AI-3 返回已过期，丢弃 seq=%s" % seq)
                return
            if not raw:
                self.alert_api_failure("AI-3", "没有可见 JSON，本地护盘继续")
                return
            if {int(p.ticket) for p in self.get_positions()} != expected_tickets:
                base.logger.info("AI-3返回时原持仓已改变，丢弃旧回复")
                return
            payload, json_error = parse_json_object(raw)
            if json_error:
                base.logger.warning("AI-3输出格式错误: %s，本地护盘继续", json_error)
                return
            self.execute_ai3(payload)
        except MarketSnapshotError as exc:
            base.logger.info("AI-3行情刷新停止本轮: %s", exc)
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
        strategies = base.STRATEGIES.snapshot()
        base.logger.info("%s 本地分不作自主AI派发门槛", base.ADAPTIVE_SESSION_VERSION)
        base.logger.info("策略框架=%s fingerprint=%s loaded=%s error=%s", base.STRATEGY_FRAMEWORK_VERSION,
                         strategies.fingerprint, [s.id for s in strategies.strategies if s.enabled], strategies.error or "无")
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
            "AI证据契约=%s，决策快照目录=%s，参考入场按启用策略契约=%s"
            % ("entry-evidence-v5", AI_DECISION_SNAPSHOT_DIR,
               {s.id: s.entry_reference for s in strategies.strategies if s.enabled})
        )
        base.logger.info("AI质量评分=%s，等级分值=%s，门槛=%s（非胜率）", QUALITY_VERSION, QUALITY_LEVEL_POINTS, AI_CONFIDENCE)
        base.logger.info("止损价格=%s BUY向下/SELL向上量化，严格保留M5极值缓冲与止损上限", base.STOP_PRICE_VERSION)
        base.logger.info("AI行情刷新=%s 每次POST采集新行情，AI-2及下单前复核现价，旧提案不跨收盘K线执行",
                         MARKET_FRESHNESS_VERSION)
        base.logger.info("入场时钟=%s 本地时钟到达M15/M5收盘边界前等待，同根K线下轮重查，护盘继续", base.ENTRY_CLOCK_VERSION)
        base.logger.info("AI协议与JSON=%s 按主备URL分别适配；支持时优先Schema，DeepSeek官方Messages仅发送effort；格式错误不执行",
                         RESPONSE_CONTRACT_VERSION)
        base.logger.info("AI成本控制=%s token上限=%s 同通道读超时补发=%s 用量目录=%s",
                         COST_CONTROL_VERSION, AI_MAX_TOKENS, AI_READ_TIMEOUT_RETRIES, AI_USAGE_DIR)
        base.logger.info("AI URLs: AI1=%s AI2=%s AI3=%s" % (AI1_URL, AI2_URL, AI3_URL))
        base.logger.info(
            "AI keys: AI1=%s AI2=%s AI3=%s"
            % ("set" if AI1_KEY else "missing", "set" if AI2_KEY else "missing", "set" if AI3_KEY else "missing")
        )
        for role, model, effort, url, key, timeout in (
            ("AI-1", AI1_MODEL, AI1_EFFORT, AI1_URL, AI1_KEY, AI1_TIMEOUT),
            ("AI-2", AI2_MODEL, AI2_EFFORT, AI2_URL, AI2_KEY, AI2_TIMEOUT),
            ("AI-3", AI3_MODEL, AI3_EFFORT, AI3_URL, AI3_KEY, AI3_TIMEOUT),
        ):
            backup = backup_chat_config(role, model, effort, url, key)
            primary = {"model": model, "effort": effort, "url": url, "key": key}
            base.logger.info("%s 协议: 主路=%s 备用=%s；DeepSeek官方Messages使用提示词JSON及Python契约校验", role,
                             request_protocol(url), request_protocol(backup["url"]))
            base.logger.info("%s 备用配置: %s host=%s model=%s effort=%s key=%s timeout=%ss 主路timeout=%ss", role,
                             "独立备用" if backup != primary else "沿用主配置",
                             urlsplit(backup["url"]).hostname, backup["model"], backup["effort"],
                             "set" if backup["key"] else "missing",
                             chat_request_timeout(role, timeout, True), timeout)
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
                entry_ready = False
                if (m15_time and m15_time != last_m15) or (m5_time and m5_time != last_m5):
                    with self._mt5_lock:
                        entry_ready = self.entry_clock_ready(m15_time, m5_time)
                if entry_ready and m15_time and m15_time != last_m15:
                    trigger = "startup" if not last_m15 else "M15"
                    last_m15 = m15_time
                    if m5_time == m15_time:
                        last_m5 = m5_time  # This closed M5 is already covered by the M15 evaluation.
                    if once:
                        seq = self.claim_task("entry", m15_time, ENTRY_TAKEOVER_SEC)
                        if seq is not None:
                            self.entry_task(m15_time, seq, trigger)
                        return 0
                    self.dispatch_entry(m15_time, trigger)
                if entry_ready and m5_time and m5_time != last_m5 and m5_time != m15_time and not once:
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
    global AI_HTTP_RETRIES, AI_HTTP_RETRY_PAUSE, AI_READ_TIMEOUT_RETRIES, AI_USAGE_DIR, backup_chat_config
    old_post = requests.post
    old_backup_config = backup_chat_config
    old_logger = base.logger
    old_retries = AI_HTTP_RETRIES
    old_pause = AI_HTTP_RETRY_PAUSE
    old_read_retries, old_usage_dir = AI_READ_TIMEOUT_RETRIES, AI_USAGE_DIR
    usage_temp = tempfile.TemporaryDirectory(prefix="myq-ai-self-test-")
    AI_USAGE_DIR = usage_temp.name
    AI_READ_TIMEOUT_RETRIES = 2  # Exercise the explicitly configurable retry path.
    AI_HTTP_RETRIES = 5
    AI_HTTP_RETRY_PAUSE = 0

    class OkResponse:
        status_code = 200
        text = "ok"
        encoding = "utf-8"

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": "{\"action\":\"no\"}"}}]}

    try:
        # Mock failures must never look like live API failures in trading logs.
        self_test_logger = logging.Logger("xauusd_fib_volume_pa.http_retry_self_test")
        self_test_logger.addHandler(logging.NullHandler())
        base.logger = self_test_logger
        # The synthetic Chat transport must not inherit configured Messages routes.
        def self_test_backup(role, model, effort, url, api_key):
            return {"model": model, "effort": effort, "url": str(url or "").strip(),
                    "key": str(api_key or "").strip()}
        backup_chat_config = self_test_backup
        state = {"n": 0}

        def flaky(url, headers=None, json=None, timeout=None, **kwargs):
            state["n"] += 1
            if state["n"] < 3:
                raise requests.ReadTimeout("read timed out")
            return OkResponse()

        requests.post = flaky
        text = call_chat("AI-1", "m", "low", "sys", "hi", 1, "http://example.test", "key")
        assert "action" in text and state["n"] == 3, (text, state["n"])

        state["n"] = 0

        def always_down(url, headers=None, json=None, timeout=None, **kwargs):
            state["n"] += 1
            raise requests.ConnectionError("connection refused")

        requests.post = always_down
        try:
            call_chat("AI-2", "m", "low", "sys", "hi", 1, "http://example.test", "key")
            raise AssertionError("timeout retries should fail")
        except AIRequestError as exc:
            assert exc.role == "AI-2" and state["n"] == 5 and "没有可见 JSON" not in exc.detail, exc.detail

        state["n"] = 0

        def timeout_once(url, headers=None, json=None, timeout=None, **kwargs):
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
        backup_chat_config = old_backup_config
        base.logger = old_logger
        AI_HTTP_RETRIES = old_retries
        AI_HTTP_RETRY_PAUSE = old_pause
        AI_READ_TIMEOUT_RETRIES, AI_USAGE_DIR = old_read_retries, old_usage_dir
        usage_temp.cleanup()


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
