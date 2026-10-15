"""Pure DEMO analysis profiles and closed-source checkpoints, never entry events.

No broker, environment, external calendar or wall clock access. A checkpoint
scopes one directional analysis/order attempt; it does not prove a trade trigger.
The host must retain and revalidate its immutable original copy at submission.
"""
from datetime import datetime
import hashlib
import json
import math

from strategies.evidence import candle_parts
from strategies.framework import Assessment
from strategies.patterns import patterns_facts

PROFILE_SESSIONS = {"asia_adaptive": ("asia",), "london_adaptive": ("london",),
                    "us_adaptive": ("overlap", "late_us")}
PROFILE_PRIORS = {
    "asia_adaptive": "亚盘参考区间边缘回收、扫回和小趋势整理；趋势明显仍可顺势回撤或续势，不能假定永远震荡。",
    "london_adaptive": "欧盘参考隔夜区间扩张、支撑阻力角色转换及突破后的整理回踩，也可判断失败突破回收。",
    "us_adaptive": "美盘参考趋势续势、双向扫动后的确认和区间反转；没有实际宏观事件信息时标未知。",
}
STYLE_MENUS = {
    "asia_adaptive": ["区间边缘回收", "流动性扫回", "小趋势整理", "趋势回撤", "顺势续势", "自主打法", "观望"],
    "london_adaptive": ["区间扩张", "支撑阻力角色转换", "整理延续", "突破回踩", "失败突破回收", "自主打法", "观望"],
    "us_adaptive": ["趋势续势", "双向扫动后确认", "区间反转", "趋势回撤", "自主打法", "观望"],
}
_STEPS = {"M5": 300, "M15": 900, "H1": 3600}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid/nonfinite numeric data")
    return value


def _clock(observed_utc, offset):
    if (not isinstance(observed_utc, datetime) or observed_utc.tzinfo is None
            or observed_utc.utcoffset() is None):
        raise ValueError("aware decision clock required")
    if abs(_number(offset)) > 14:
        raise ValueError("invalid server UTC offset")
    return observed_utc.timestamp()


def _raw(value, timeframe):
    value = _number(value)
    if value != int(value) or value < 0 or value % _STEPS[timeframe]:
        raise ValueError("invalid/alignment " + timeframe + " bar time")
    return int(value)


def _latest(now, offset, timeframe):
    step = _STEPS[timeframe]
    return math.floor((now + offset * 3600) / step) * step - step


def _rows(frame, timeframe, now, offset):
    if frame is None or not hasattr(frame, "columns") or any(
            key not in frame.columns for key in ("time", "open", "high", "low", "close")):
        raise ValueError("missing " + timeframe + " source data")
    rows = frame.to_dict("records")
    if len(rows) < (3 if timeframe == "M5" else 1):
        raise ValueError("insufficient " + timeframe + " source data")
    previous = None
    for row in rows:
        row["time"] = _raw(row["time"], timeframe)
        for key in ("open", "high", "low", "close"):
            row[key] = _number(row[key])
            if row[key] <= 0:
                raise ValueError("nonpositive " + timeframe + " price")
        if row["high"] < max(row["open"], row["close"], row["low"]) or row["low"] > min(row["open"], row["close"]):
            raise ValueError("invalid " + timeframe + " OHLC")
        if "tick_volume" in row and _number(row["tick_volume"]) < 0:
            raise ValueError("negative " + timeframe + " tick volume")
        if previous is not None and row["time"] <= previous:
            raise ValueError("duplicate or out of order " + timeframe + " source data")
        if row["time"] + _STEPS[timeframe] - offset * 3600 > now:
            raise ValueError("unclosed/future " + timeframe + " source data")
        previous = row["time"]
    if rows[-1]["time"] != _latest(now, offset, timeframe):
        raise ValueError("stale/latest " + timeframe + " source mismatch")
    # Older weekend gaps can exist, but the protective window must be complete.
    if timeframe == "M5" and any(b["time"]-a["time"] != 300 for a, b in zip(rows[-3:-1], rows[-2:])):
        raise ValueError("missing/gapped recent3 M5 source data")
    return rows


def checkpoint_id(profile, action, m5_bar_time, m15_bar_time, server_offset_hours):
    """Deterministic directional identity; normalize signed numeric offsets."""
    if profile not in PROFILE_SESSIONS or action not in ("buy", "sell"):
        raise ValueError("invalid checkpoint profile/action")
    identity = [profile, action, _raw(m5_bar_time, "M5"), _raw(m15_bar_time, "M15"),
                float(_number(server_offset_hours)) + 0.0]
    return "analysis-checkpoint-v1/" + hashlib.sha256(
        json.dumps(identity, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()[:32]


def checkpoint_order_scope(evidence):
    """One order budget across directions/routes/anchors at the same actual closed sources."""
    cp = evidence["analysis_checkpoint"]
    identity = [cp["profile"], cp["m5_bar_time"], cp["m15_bar_time"], float(cp["server_offset_hours"]) + 0.0]
    return "analysis-order-scope-v1/" + hashlib.sha256(
        json.dumps(identity, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()[:32]


def validate_analysis_checkpoint(evidence, observed_utc, server_offset_hours,
                                 df_m5=None, df_m15=None, profile=None, action=None):
    """Return empty string or specific error; never infer a pattern or missing clock.

    Without frames checks shape, latest closure, exact references and identity.
    With frames also proves ordering, valid OHLC, membership and latest sources.
    observed_utc is the current aware host clock, not model-supplied evidence.
    """
    try:
        now = _clock(observed_utc, server_offset_hours)
        if not isinstance(evidence, dict) or not isinstance(evidence.get("analysis_checkpoint"), dict):
            raise ValueError("missing analysis checkpoint")
        cp = evidence["analysis_checkpoint"]
        if cp.get("kind") != "analysis-checkpoint-v1":
            raise ValueError("unsupported analysis checkpoint")
        selected_profile, selected_action = cp.get("profile"), cp.get("action")
        if selected_profile not in PROFILE_SESSIONS or selected_action not in ("buy", "sell"):
            raise ValueError("invalid checkpoint profile/action")
        if profile is not None and profile != selected_profile or action is not None and action != selected_action:
            raise ValueError("checkpoint selected profile/action mismatch")
        if evidence.get("profile") != selected_profile or evidence.get("action") != selected_action:
            raise ValueError("checkpoint evidence profile/action mismatch")
        if ("entry_event" in evidence or evidence.get("closed_trigger", {}).get("confirmed") is True
                or evidence.get("trade_trigger_confirmed") is not False):
            raise ValueError("analysis checkpoint is not a confirmed trade trigger")
        if _number(cp.get("server_offset_hours")) != server_offset_hours or _number(evidence.get("server_offset_hours")) != server_offset_hours:
            raise ValueError("checkpoint server offset mismatch")
        m5, m15 = _raw(cp.get("m5_bar_time"), "M5"), _raw(cp.get("m15_bar_time"), "M15")
        expected_id = checkpoint_id(selected_profile, selected_action, m5, m15, server_offset_hours)
        if cp.get("checkpoint_id") != expected_id or evidence.get("event_id") != expected_id:
            raise ValueError("checkpoint identity mismatch")
        origin = _number(cp.get("observed_utc"))
        end = _number(cp.get("valid_until_utc"))
        expected_end = m5 + 600 - server_offset_hours * 3600
        if (end != expected_end or not m5 + 300 - server_offset_hours*3600 <= origin < end
                or not origin <= now < end):
            raise ValueError("future/expired analysis checkpoint or invalid TTL")
        for tf, stamp in (("M5", m5), ("M15", m15)):
            if stamp + _STEPS[tf] - server_offset_hours*3600 > origin:
                raise ValueError("unclosed checkpoint source")
            if stamp != _latest(origin, server_offset_hours, tf) or stamp != _latest(now, server_offset_hours, tf):
                raise ValueError("stale/latest checkpoint source mismatch")
        expected_refs = [{"timeframe": "M5", "bar_time": m5-600+i*300} for i in range(3)] + [
            {"timeframe": "M15", "bar_time": m15}]
        if cp.get("source_refs") != expected_refs:
            raise ValueError("missing/reordered checkpoint source references")
        for tf, frame, stamp in (("M5", df_m5, m5), ("M15", df_m15, m15)):
            if frame is None:
                continue
            rows = _rows(frame, tf, now, server_offset_hours)
            if rows[-1]["time"] != stamp or any(ref["bar_time"] not in {r["time"] for r in rows}
                                                  for ref in expected_refs if ref["timeframe"] == tf):
                raise ValueError("checkpoint source membership/latest mismatch")
        return ""
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError) as exc:
        return str(exc) or "invalid analysis checkpoint"


def _bar(row):
    return {key: row[key] for key in ("time", "open", "high", "low", "close")}


def _ema(rows, period=21):
    # Simple existing EMA convention; factual context only, never an extra veto.
    ema = rows[0]["close"]
    for row in rows[1:]:
        ema += (row["close"]-ema) * (2/(period+1))
    return ema


def _soft_score(action, m5, m15, location, delta, atr, config):
    """Session weights apply to named source facts; absent facts never veto."""
    direction = 1 if action == "buy" else -1
    latest5, previous5 = m5[-1], m5[-2]
    latest15 = m15[-1]
    previous15 = m15[-2] if len(m15) > 1 else None
    edge = _number(config["range_edge_fraction"])
    small = _number(config["small_trend_max_atr"])
    reaction = _number(config["reaction_range_atr"])
    if not 0 < edge < .5 or small <= 0 or reaction <= 0:
        raise ValueError("invalid coarse context thresholds")
    same_m5 = direction*(latest5["close"]-latest5["open"]) > 0
    boundary = previous15["high" if action == "buy" else "low"] if previous15 else None
    components = {
        "directional_change": direction*delta > 0,
        "same_direction_m5": same_m5,
        "range_edge": None if location is None else (location <= edge if action == "buy" else location >= 1-edge),
        "m5_reclaim": (latest5["low"] < previous5["low"] <= latest5["close"] if action == "buy"
                       else latest5["high"] > previous5["high"] >= latest5["close"]),
        "small_trend": 0 < direction*delta <= small,
        "directional_expansion": None if boundary is None else direction*(latest15["close"]-boundary) > 0,
        "prior_extreme_cross_hold": None if boundary is None else (
            latest5["low"] <= boundary <= latest5["high"] and direction*(latest5["close"]-boundary) > 0),
        "volatility_reaction": (latest5["high"]-latest5["low"])/atr >= reaction and same_m5,
    }
    if set(config["weights"]) != set(components):
        raise ValueError("coarse context weights must name each actual fact")
    result = {}
    for name, fact in components.items():
        weight = config["weights"][name]
        if type(weight) is not int or not 0 <= weight <= 20:
            raise ValueError("invalid coarse context weight")
        result[name] = {"fact": fact, "weight": weight, "contribution": weight if fact else 0}
    raw_score = 40 + sum(item["contribution"] for item in result.values())
    return min(100, raw_score), {"base_score": 40, "raw_score": raw_score,
        "score": min(100, raw_score), "components": result,
        "thresholds": {"range_edge_fraction": edge, "small_trend_max_atr": small, "reaction_range_atr": reaction},
        "meaning": "soft contextual facts, not mandatory patterns or confirmed triggers"}


def evaluate_profile(c, profile, soft_scoring, direction_policy=None):
    """Soft coarse facts; the host's explicit direction permission is binding."""
    facts = {"profile": profile, "action": c.action, "trade_trigger_confirmed": False,
             "profile_prior": PROFILE_PRIORS[profile], "style_menu": list(STYLE_MENUS[profile]),
             "uncertainty": ["宏观事件/新闻日历未知：没有外部实际信息", "tick_volume仅为经纪商成交量代理",
                             "分盘为北京时间固定策略分段，非交易所开盘钟", "分析检查点不证明交易触发",
                             "学习材料只作位置/角色转换/整理参考，不是完整作者算法或胜率证明"]}
    blockers = []
    permitted = c.action in ("buy", "sell") and c.h1_trend == ("bull" if c.action == "buy" else "bear")
    facts["h1_permitted"] = permitted
    direction = c.direction_context
    if direction is not None or direction_policy is not None:
        facts["h1_same_direction"] = facts.pop("h1_permitted")
    if direction_policy is not None and direction is None:
        blockers.append("方向许可缺少宿主重建的闭合来源")
    if direction is not None:
        permission = direction.get("permissions", {}).get(c.action, {})
        permitted = (direction.get("h1_background") == c.h1_trend and permission.get("allowed") is True)
        if not permitted:
            blockers.append("方向许可拒绝: " + str(permission.get("reason", "方向事实不可用")))
    elif not permitted:
        blockers.append("自主分析仍要求H1同向")
    score = 0
    try:
        now = _clock(c.now, c.server_utc_offset)
        atr = _number(c.atr)
        if atr <= 0:
            raise ValueError("invalid/nonpositive M15 ATR")
        m5 = _rows(c.df_m5, "M5", now, c.server_utc_offset)
        m15 = _rows(c.df_m15, "M15", now, c.server_utc_offset)
        h1 = _rows(c.df_h1, "H1", now, c.server_utc_offset)
        latest5, latest15 = m5[-1]["time"], m15[-1]["time"]
        identity = checkpoint_id(profile, c.action, latest5, latest15, c.server_utc_offset)
        cp = {"kind": "analysis-checkpoint-v1", "checkpoint_id": identity, "profile": profile,
              "action": c.action, "m5_bar_time": latest5, "m15_bar_time": latest15,
              "server_offset_hours": c.server_utc_offset, "observed_utc": now,
              "valid_until_utc": latest5 + 600 - c.server_utc_offset*3600,
              "source_refs": [{"timeframe": "M5", "bar_time": r["time"]} for r in m5[-3:]] + [
                  {"timeframe": "M15", "bar_time": latest15}]}
        facts.update(analysis_checkpoint=cp, event_id=identity, server_offset_hours=c.server_utc_offset)
        error = validate_analysis_checkpoint(facts, c.now, c.server_utc_offset, c.df_m5, c.df_m15, profile, c.action)
        if error:
            raise ValueError(error)
        recent = m15[-24:]
        low, high = min(r["low"] for r in recent), max(r["high"] for r in recent)
        width = high-low
        close = m15[-1]["close"]
        location = (close-low)/width if width else None
        ema = _ema(m15)
        delta = (m15[-1]["close"]-m15[max(0, len(m15)-6)]["close"])/atr
        parts = candle_parts(m5[-1])
        volumes = [r["tick_volume"] for r in m15[-20:] if "tick_volume" in r]
        mean_volume = sum(volumes)/len(volumes) if volumes else None
        volume = m15[-1].get("tick_volume")
        direction = 1 if c.action == "buy" else -1
        facts.update(
            regime={"host_m15_trend": c.m15_trend, "h1_trend": c.h1_trend,
                    "adx": _number(c.adx), "recent_width_atr": width/atr,
                    "recent_close_change_atr": delta, "classification": "context_only"},
            position={"reference_close": close, "reference_bar_time": latest15,
                      "ema21": ema, "ema_distance_atr": (close-ema)/atr,
                      "recent_range_location": location, "lookback_bars": len(recent)},
            recent_extremes={"m15_low": low, "m15_high": high, "m15_source_bars": [_bar(r) for r in recent],
                             "m5_protection_low": min(r["low"] for r in m5[-3:]),
                             "m5_protection_high": max(r["high"] for r in m5[-3:]),
                             "m5_protection_bars": [_bar(r) for r in m5[-3:]]},
            atr={"timeframe": "M15", "value": atr, "source": "host_calculate_atr",
                 "latest_bar_time": latest15},
            bar_behavior={"latest_m5": _bar(m5[-1]), "latest_m15": _bar(m15[-1]),
                          "latest_h1": _bar(h1[-1]), "m5_candle_parts": parts,
                          "m5_opposes_action": direction*(m5[-1]["close"]-m5[-1]["open"]) < 0,
                          "m5_patterns": patterns_facts(c.df_m5, "M5"),
                          "m15_patterns": patterns_facts(c.df_m15, "M15")},
            volume={"latest_m15_tick_volume": volume, "recent_mean_tick_volume": mean_volume,
                    "relative_tick_volume": volume/mean_volume if volume is not None and mean_volume else None,
                    "source": "broker_tick_volume_proxy"})
        # Every component is a soft observed fact. Session weighting is owned by
        # the selected definition, with no pattern-completion or score blocker.
        score, facts["soft_scoring"] = _soft_score(c.action, m5, m15, location, delta, atr, soft_scoring)
        json.dumps(facts, allow_nan=False)
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError) as exc:
        blockers.append("analysis_source_invalid: " + (str(exc) or "unknown"))
        # Never publish partial invalid prices/checkpoint as reusable evidence.
        facts = {key: value for key, value in facts.items() if key in (
            "profile", "action", "trade_trigger_confirmed", "profile_prior", "style_menu", "uncertainty", "h1_permitted", "h1_same_direction")}
        score = 0
    return Assessment(score, {"fib": "自主分析粗筛，不强制Fib回撤或固定形态",
                              "volume": "量能代理仅作参考", "price_action": "真实收盘数据供AI自主判断",
                              "trend": "H1=" + str(c.h1_trend), "fib_score": "0", "pa_score": "0"},
                      evidence=facts, blockers=tuple(blockers))
