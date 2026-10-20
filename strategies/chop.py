"""Closed-M5 repetitive chop recognition and symmetric new-entry confirmation.

No broker, state, AI calls, profit scoring, or position management. The host
supplies an aware clock, explicit server offset and broker tick size.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

VERSION = "repeated-chop-v1"
DEFAULT_POLICY = dict(version=VERSION, long_bars=24, night_bars=12, confirmation_bars=2,
                      efficiency_max=.35, min_reversals=5, min_midpoint_crosses=3,
                      midpoint_ticks=2, edge_fraction=.25, buffer_atr=.1, buffer_ticks=2,
                      directional_efficiency_min=.35, directional_crosses_max=1,
                      night_start_hour=1, night_end_hour=7)
ENGINE_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
BEIJING = timezone(timedelta(hours=8))
FIELDS = ("time", "open", "high", "low", "close")
WINDOW_FIELDS = {"bars", "first_time", "last_time", "low", "high", "net_change", "path_length",
                 "efficiency", "reversals", "midpoint_crosses", "choppy"}
ROOT_FIELDS = {"version", "state", "night_guard", "selected_window", "source_digest", "buffer", "atr14"}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("震荡识别数值/时钟未知")
    return float(value)


def _d(value):
    return Decimal(str(value))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def policy_for(contract):
    """Absence keeps legacy behavior; invalid declarations never silently disable."""
    parameters = (contract or {}).get("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError("反复震荡策略参数格式错误")
    if "chop_policy" not in parameters:
        return None
    declared = parameters["chop_policy"]
    if not isinstance(declared, dict) or set(declared) != set(DEFAULT_POLICY):
        raise ValueError("反复震荡策略声明缺失或含未知参数")
    if any(type(declared[k]) is not type(v) or declared[k] != v for k, v in DEFAULT_POLICY.items()):
        raise ValueError("反复震荡策略含未登记版本/参数")
    if contract.get("analysis_mode") is not True or contract.get("demo_only") is not True:
        raise ValueError("反复震荡入场政策只用于显式模拟自主契约")
    return deepcopy(DEFAULT_POLICY)


def _closed_rows(bars, clock, offset):
    if hasattr(bars, "to_dict"):
        bars = bars.to_dict("records")
    if not isinstance(bars, (list, tuple)):
        raise ValueError("反复震荡缺少M5闭合来源")
    rows, previous = [], None
    for row in bars:
        if not isinstance(row, dict):
            raise ValueError("反复震荡M5格式错误")
        stamp = _number(row.get("time"))
        if stamp < 0 or stamp != int(stamp) or stamp % 300 or previous is not None and stamp <= previous:
            raise ValueError("反复震荡M5时间错序/重复/未对齐")
        previous = stamp
        if stamp + 300 - offset * 3600 > clock:
            continue
        value = {key: _number(row.get(key)) for key in FIELDS}
        value["time"] = int(stamp)
        if (min(value[k] for k in FIELDS[1:]) <= 0
                or value["high"] < max(value["open"], value["close"], value["low"])
                or value["low"] > min(value["open"], value["close"])):
            raise ValueError("反复震荡M5 OHLC错误")
        rows.append(value)
    rows = rows[-26:]
    latest = int((clock + offset * 3600) // 300) * 300 - 300
    if len(rows) != 26 or rows[-1]["time"] != latest or any(
            b["time"]-a["time"] != 300 for a, b in zip(rows, rows[1:])):
        raise ValueError("反复震荡M5未预热/缺口/最新闭合来源过期")
    return rows


def _switches(signs):
    nonzero = [s for s in signs if s]
    return sum(a != b for a, b in zip(nonzero, nonzero[1:]))


def _sign(value):
    return 1 if value > 0 else -1 if value < 0 else 0


def _window(rows, tick):
    closes = [_d(r["close"]) for r in rows]
    changes = [b-a for a, b in zip(closes, closes[1:])]
    path, net = sum(abs(v) for v in changes), closes[-1]-closes[0]
    low, high = min(_d(r["low"]) for r in rows), max(_d(r["high"]) for r in rows)
    middle, neutral = (low+high)/2, 2*_d(tick)
    sides = [_sign(c-middle) if abs(c-middle) > neutral else 0 for c in closes]
    efficiency = abs(net)/path if path else Decimal(0)
    reversals, crosses = _switches([_sign(v) for v in changes]), _switches(sides)
    choppy = (efficiency <= _d(DEFAULT_POLICY["efficiency_max"])
              and reversals >= DEFAULT_POLICY["min_reversals"] and crosses >= DEFAULT_POLICY["min_midpoint_crosses"])
    return dict(bars=len(rows), first_time=rows[0]["time"], last_time=rows[-1]["time"],
                low=float(low), high=float(high), net_change=float(net), path_length=float(path),
                efficiency=float(efficiency), reversals=reversals, midpoint_crosses=crosses, choppy=choppy)


def analyze(bars, *, now, server_utc_offset, tick_size):
    if isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("反复震荡必须使用明确UTC时钟")
        now = now.timestamp()
    clock, offset, tick = _number(now), _number(server_utc_offset), _number(tick_size)
    if clock < 0 or abs(offset) > 14 or offset*3600 % 300 or tick <= 0:
        raise ValueError("反复震荡时钟偏移/tick无效")
    rows = _closed_rows(bars, clock, offset)
    history = rows[:-2]
    windows = {str(n): _window(history[-n:], tick) for n in (12, 24)}
    night = 1 <= datetime.fromtimestamp(clock, BEIJING).hour < 7
    selected = next((key for key in ("24", "12") if windows[key]["choppy"] and (key == "24" or night)), None)
    tr = [max(_d(b["high"])-_d(b["low"]), abs(_d(b["high"])-_d(a["close"])),
              abs(_d(b["low"])-_d(a["close"]))) for a, b in zip(history, history[1:])][-14:]
    atr = sum(tr)/14
    return dict(version=VERSION, engine_digest=ENGINE_DIGEST, state="chop" if selected else "unconfirmed",
                night_guard=night, selected_window=selected, windows=windows,
                source_digest=_digest(dict(rows=rows, offset=offset, tick=tick)),
                atr14=float(atr), buffer=float(max(atr*Decimal("0.1"), 2*_d(tick))),
                confirmation=deepcopy(rows[-2:]))


def decide(context, action, *, bid, ask):
    def result(allowed, route, reason):
        return dict(version=VERSION, action=action, allowed=allowed, route=route, reason=reason)
    try:
        bid, ask = _number(bid), _number(ask)
        if bid <= 0 or ask < bid or action not in ("buy", "sell"):
            raise ValueError("报价/方向无效")
        if context["version"] != VERSION or context["engine_digest"] != ENGINE_DIGEST:
            raise ValueError("识别引擎/版本改变")
        if context["state"] == "unconfirmed":
            return result(True, "unrestricted", "未确认反复震荡，沿用原自主入场检查")
        if context["state"] != "chop":
            raise ValueError("反复震荡状态未知")
        box = context["windows"][context["selected_window"]]
        low, high, margin = _d(box["low"]), _d(box["high"]), _d(context["buffer"])
        a, b = ({k: _d(v) for k, v in row.items()} for row in context["confirmation"])
        buy = action == "buy"
        boundary = high+margin if buy else low-margin
        beyond = lambda price: price > boundary if buy else price < boundary
        if all(beyond(row["close"]) for row in (a, b)) and beyond(_d(bid if buy else ask)):
            return result(True, "breakout_confirmed", "反复震荡后连续两M5收在原箱外且当前触发侧仍在外")
        recent = context["windows"]["12"]
        middle = (_d(recent["high"])+_d(recent["low"]))/2
        directional_boundary = middle+margin if buy else middle-margin
        directional_side = lambda price: price > directional_boundary if buy else price < directional_boundary
        progressing = (_d(recent["net_change"]) > 0 if buy else _d(recent["net_change"]) < 0)
        if (context["selected_window"] == "24" and progressing
                and _d(recent["efficiency"]) > _d(DEFAULT_POLICY["directional_efficiency_min"])
                and recent["midpoint_crosses"] <= DEFAULT_POLICY["directional_crosses_max"]
                and all(directional_side(row["close"]) for row in (a,b))
                and directional_side(_d(bid if buy else ask))):
            return result(True, "directional_leg_confirmed", "旧长窗震荡但近一小时已有同向推进，两M5及当前报价保持推进侧，交AI审核")
        edge = low + (high-low)*Decimal(".25") if buy else high-(high-low)*Decimal(".25")
        body = abs(a["close"]-a["open"])
        full = a["high"]-a["low"]
        wick = min(a["open"],a["close"])-a["low"] if buy else a["high"]-max(a["open"],a["close"])
        rejection = (a["close"] > a["open"] and a["low"] <= edge and a["close"] >= a["low"]+full*Decimal(".65") if buy else
                     a["close"] < a["open"] and a["high"] >= edge and a["close"] <= a["high"]-full*Decimal(".65"))
        follow = b["close"] > a["high"]+margin and b["close"] > b["open"] if buy else (
                 b["close"] < a["low"]-margin and b["close"] < b["open"])
        entry = _d(ask if buy else bid)
        space = low <= entry <= edge and _d(bid) > a["low"] if buy else (
                edge <= entry <= high and _d(ask) < a["high"])
        if rejection and body >= margin and wick >= body and follow and space and low <= b["close"] <= high:
            return result(True, "edge_rejection_confirmed", "反复震荡边缘拒绝获下一M5收盘确认，当前入场仍在外侧25%")
        return result(False, "wait", "反复震荡：区间中部/边缘未确认或突破未站稳，等待新闭合确认")
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return result(False, "unknown", "反复震荡入场复核失败: "+str(exc))


def bind(context, decision):
    return dict(version=VERSION, engine_digest=ENGINE_DIGEST, source_digest=context["source_digest"],
                night_guard=context["night_guard"], selected_window=context["selected_window"], route=decision["route"])


def binding_error(original, context, decision):
    if not decision.get("allowed"):
        return decision.get("reason", "反复震荡未许可")
    return "" if original == bind(context, decision) else "反复震荡原POST来源/时段/窗口/路线已改变或缺失"


def citation_allowed(parts):
    return (len(parts) == 2 and parts[1] in ROOT_FIELDS or len(parts) == 4
            and parts[1] == "windows" and parts[2] in ("12", "24") and parts[3] in WINDOW_FIELDS)
