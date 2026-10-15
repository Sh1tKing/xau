"""Deterministic, closed-source direction permission; never an entry signal.

MT5 bar times are server-clock open epochs. ``now`` is UTC and the explicit
offset is in hours. Only OHLC/time participate in source identities. The host
must bind H1 source identity, strategy fingerprint, checkpoint and account.
"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path


LEGACY_VERSION = "h1-only-v1"
VERSION = "h1-or-m15-structure-v1"
DEFAULT_POLICY = {
    "version": VERSION,
    "break_lookback_m15": 2,
    "leg_ttl_m15": 6,
    "break_margin_atr": 0.10,
    "break_margin_ticks": 2,
    "failure_closes_m5": 2,
    "anchor_pivot_left_m5": 1,
    "anchor_pivot_right_m5": 1,
}
# Fingerprint the code loaded by this process, not a later on-disk replacement.
ENGINE_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _number(value):
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("invalid/nonfinite numeric data")
    return float(value) + 0.0


def _decimal(value):
    """Preserve quoted decimal price equality across arithmetic and mirrors."""
    return Decimal(str(value))


def _level(price, epsilon, sign):
    return float(_decimal(price) + sign * _decimal(epsilon))


def normalize_policy(policy=None):
    """Validate the registered v1 parameters; absence retains H1-only behavior.

    V1 is deliberately fixed to the preregistered experiment. A different rule
    requires a new version, rather than silently changing an existing proof.
    """
    if policy is None:
        return {"version": LEGACY_VERSION}
    if not isinstance(policy, dict):
        raise ValueError("direction_policy must be a mapping")
    if policy == {"version": LEGACY_VERSION}:
        return {"version": LEGACY_VERSION}
    if policy.get("version") != VERSION or set(policy) != set(DEFAULT_POLICY):
        raise ValueError("unknown/incomplete direction policy")
    normalized = {"version": VERSION}
    for key, expected in DEFAULT_POLICY.items():
        if key == "version":
            continue
        actual = _number(policy[key])
        if actual != expected:
            raise ValueError("unregistered direction parameter: " + key)
        normalized[key] = float(actual) if isinstance(expected, float) else int(actual)
    return normalized


def _clock(now, offset):
    if isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("aware UTC decision clock required")
        now = now.timestamp()
    now, offset = _number(now), _number(offset)
    if now < 0 or abs(offset) > 14 or offset * 3600 % 300:
        raise ValueError("invalid decision clock/server UTC offset")
    return now, offset


def _rows(source, step, limit, now, offset):
    if hasattr(source, "to_dict"):
        source = source.to_dict("records")
    if not isinstance(source, (list, tuple)):
        raise ValueError("missing closed source bars")
    rows, previous = [], None
    for item in source:
        if not isinstance(item, dict):
            raise ValueError("invalid source bar")
        stamp = _number(item.get("time"))
        if stamp < 0 or stamp != int(stamp) or stamp % step:
            raise ValueError("invalid/alignment source bar time")
        if previous is not None and stamp <= previous:
            raise ValueError("duplicate/unordered source bar time")
        previous = stamp
        # Future/actively forming rows never enter any historical calculation.
        if stamp + step - offset * 3600 > now:
            continue
        bar = {"time": int(stamp)}
        for key in ("open", "high", "low", "close"):
            bar[key] = _number(item.get(key))
            if bar[key] <= 0:
                raise ValueError("nonpositive source price")
        if (bar["high"] < max(bar["open"], bar["close"], bar["low"])
                or bar["low"] > min(bar["open"], bar["close"])):
            raise ValueError("invalid source OHLC")
        rows.append(bar)
    rows = rows[-limit:]
    latest = math.floor((now + offset * 3600) / step) * step - step
    if not rows or rows[-1]["time"] != latest:
        raise ValueError("stale/latest closed source mismatch")
    return rows


def _continuous(rows, step):
    if any(b["time"] - a["time"] != step for a, b in zip(rows, rows[1:])):
        raise ValueError("gapped relevant closed source")


def _permission(allowed, reason, route="h1_trend", anchor=None, event=None):
    return {"allowed": allowed, "reason": reason, "route": route,
            "anchor": anchor, "structure_event_id": event, "proof_digest": None}


def _empty(policy):
    return {"version": policy["version"], "parameters_digest": _digest(policy),
            "engine_digest": ENGINE_DIGEST, "source_digest": None,
            "source_bar_ids": {"m15": [], "m5": []}, "h1_background": "unknown",
            "m15_leg": {"state": "unknown", "reason": "unverified sources", "event_id": None,
                        "epsilon": None, "boundaries": None, "confirmation": None},
            "permissions": {side: _permission(False, "unverified sources") for side in ("buy", "sell")}}


def _anchor(event, m5, bid, direction):
    """Select the latest valid confirmed pivot, without consulting stop limits."""
    up = direction == "up"
    field = "low" if up else "high"
    origin = event["origin_price"]
    window = [b for b in m5 if b["time"] >= event["formation_start"]]
    chosen = None
    for i in range(1, len(window) - 1):
        left, pivot, right = window[i-1:i+2]
        price = pivot[field]
        strict_pivot = (price < left[field] and price < right[field]) if up else (
            price > left[field] and price > right[field])
        higher = (_decimal(price) >= _decimal(origin) + _decimal(event["epsilon"]) if up else
                  _decimal(price) <= _decimal(origin) - _decimal(event["epsilon"]))
        preserved = all(b[field] >= price if up else b[field] <= price for b in window[i+1:])
        quote_preserved = bid >= price if up else bid <= price
        if strict_pivot and higher and preserved and quote_preserved:
            chosen = {"kind": "local_pullback", "price": price,
                      "bar_times": [left["time"], pivot["time"], right["time"]],
                      "confirmed_at": right["time"] + 300 - event["server_offset_hours"] * 3600,
                      "origin_price": origin}
    if chosen is not None:
        return chosen
    return {"kind": "origin", "price": origin, "bar_times": event["formation_bar_times"],
            "confirmed_at": event["confirmation"]["closed_at_utc"], "origin_price": origin}


def _event(m15, policy, tick, offset):
    # At the earliest eligible event, ATR needs fourteen TRs with a preceding
    # close (not a first-row high-low substitute). Rolling mean matches host.
    if len(m15) < 20:
        raise ValueError("insufficient M15 ATR14/last6 event coverage")
    _continuous(m15[-20:], 900)
    tr = [None]
    for prev, bar in zip(m15, m15[1:]):
        high, low, close = (_decimal(bar["high"]), _decimal(bar["low"]), _decimal(prev["close"]))
        tr.append(max(high - low, abs(high - close), abs(low - close)))
    last = None
    for i in range(len(m15)-6, len(m15)):
        bar, prior = m15[i], m15[i-2:i]
        atr = _number(float(sum(tr[i-13:i+1]) / 14))
        if atr <= 0:
            raise ValueError("nonpositive historical M15 ATR14")
        epsilon = _number(float(max(_decimal(policy["break_margin_atr"]) * _decimal(atr),
                                    _decimal(policy["break_margin_ticks"]) * _decimal(tick))))
        upper, lower = max(b["high"] for b in prior), min(b["low"] for b in prior)
        direction = "up" if _decimal(bar["close"]) > _decimal(upper) + _decimal(epsilon) else (
            "down" if _decimal(bar["close"]) < _decimal(lower) - _decimal(epsilon) else None)
        if direction is None:
            continue
        formation = prior + [bar]
        confirmation = {"bar_time": bar["time"], "closed_at_utc": bar["time"] + 900 - offset*3600,
                        "close": bar["close"], "atr14": atr}
        last = {"direction": direction, "epsilon": epsilon,
                "boundaries": {"upper": upper, "lower": lower}, "confirmation": confirmation,
                "formation_start": prior[0]["time"], "formation_bar_times": [b["time"] for b in formation],
                "origin_price": min(b["low"] for b in formation) if direction == "up" else max(b["high"] for b in formation),
                "server_offset_hours": offset, "index": i}
        last["event_id"] = "m15-structure-v1/" + _digest({
            "policy": policy, "formation": formation, "confirmation": confirmation,
            "direction": direction, "epsilon": epsilon, "offset": offset})[:32]
    return last


def evaluate_context(m15_bars, m5_bars, h1_trend, policy=None, *, now,
                     server_utc_offset, tick_size, bid):
    """Return JSON-safe shared facts and independently verifiable permissions.

    Invalid market data fails closed. Invalid policy raises ``ValueError`` so
    definition loaders can reject it. Identical sources and chosen permission
    retain proof identity while the quote moves on the valid side of a level.
    """
    policy = normalize_policy(policy)
    out = _empty(policy)
    try:
        clock, offset = _clock(now, server_utc_offset)
        tick, bid = _number(tick_size), _number(bid)
        if tick <= 0 or bid <= 0:
            raise ValueError("invalid tick size/current Bid")
        if h1_trend not in ("bull", "bear"):
            raise ValueError("unknown H1 background")
        m15 = _rows(m15_bars, 900, 32, clock, offset)
        m5 = _rows(m5_bars, 300, 80, clock, offset)
        if len(m5) < 3:
            raise ValueError("insufficient M5 source coverage")
        _continuous(m5[-3:], 300)
        out["h1_background"] = h1_trend
        out["source_bar_ids"] = {"m15": [b["time"] for b in m15], "m5": [b["time"] for b in m5]}
        out["source_digest"] = _digest({"m15": m15, "m5": m5, "offset": offset, "tick_size": tick})
        event = None if policy["version"] == LEGACY_VERSION else _event(m15, policy, tick, offset)
        leg = {"state": "none", "reason": "no qualifying M15 event", "event_id": None,
               "epsilon": None, "boundaries": None, "confirmation": None}
        failure = None
        if event:
            # Cover the earliest possible formation window, not only the last
            # two displayed M5s; failures remain sticky until a new event.
            relevant_start = m15[-6]["time"] - 1800
            relevant = [b for b in m5 if b["time"] >= relevant_start]
            if not relevant or relevant[0]["time"] != relevant_start:
                raise ValueError("insufficient M5 formation/failure history")
            _continuous(relevant, 300)
            leg.update({key: deepcopy(event[key]) for key in (
                "event_id", "epsilon", "boundaries", "confirmation", "formation_start", "origin_price")})
            leg["direction"] = event["direction"]
            expires = event["confirmation"]["closed_at_utc"] + 5400
            leg["expires_at_utc"] = expires
            up = event["direction"] == "up"
            level = (_level(event["boundaries"]["upper"], event["epsilon"], -1) if up else
                     _level(event["boundaries"]["lower"], event["epsilon"], 1))
            leg["failure_level"] = level
            invalid = [b for b in m15[event["index"]+1:]
                       if (b["close"] < level if up else b["close"] > level)]
            post = [b for b in m5 if b["time"] >= event["confirmation"]["bar_time"] + 900]
            for a, b in zip(post, post[1:]):
                if b["time"] - a["time"] == 300 and all(
                        row["close"] < level if up else row["close"] > level for row in (a, b)):
                    failure = {"bar_times": [a["time"], b["time"]], "level": level,
                               "confirmed_at": b["time"]+300-offset*3600,
                               "low": min(a["low"], b["low"]), "high": max(a["high"], b["high"])}
                    break
            leg["failure"] = failure
            if clock >= expires:
                leg["reason"] = "M15 event expired"
            elif invalid:
                leg["reason"] = "M15 closing structure failure"
            elif failure:
                leg["reason"] = "persistent two-close M5 failure"
            else:
                leg.update(state=event["direction"], reason="valid M15 structure")
            leg["m15_invalidated"] = bool(invalid)
        out["m15_leg"] = leg
        h1_side = "buy" if h1_trend == "bull" else "sell"
        permissions = {side: _permission(side == h1_side, "H1 trend permission" if side == h1_side else "opposes H1 background")
                       for side in ("buy", "sell")}
        if event and leg.get("direction") != ("up" if h1_side == "buy" else "down"):
            tactical = "buy" if event["direction"] == "up" else "sell"
            if leg["state"] in ("up", "down"):
                permissions[h1_side] = _permission(False, "opposes valid M15 structure", event=event["event_id"])
                level = leg["failure_level"]
                valid_quote = bid >= level if tactical == "buy" else bid <= level
                anchor = _anchor(event, m5, bid, event["direction"])
                permissions[tactical] = _permission(valid_quote,
                    "valid opposite-H1 M15 structure" if valid_quote else "current Bid crossed structure boundary",
                    "m15_tactical", anchor, event["event_id"])
            elif failure and not leg["m15_invalidated"] and clock < leg["expires_at_utc"]:
                level = leg["failure_level"]
                valid_quote = bid > level if h1_side == "buy" else bid < level
                anchor = {"kind": "failure_window", "price": failure["low"] if h1_side == "buy" else failure["high"],
                          "bar_times": failure["bar_times"], "confirmed_at": failure["confirmed_at"],
                          "origin_price": event["origin_price"]}
                permissions[h1_side] = _permission(valid_quote,
                    "confirmed M5 failure restores H1 direction" if valid_quote else "Bid no longer supports H1 recovery",
                    "h1_recovery", anchor, event["event_id"])
                permissions[tactical] = _permission(False, "event previously failed on two M5 closes",
                                                    "m15_tactical", event=event["event_id"])
        for action, permission in permissions.items():
            permission["proof_digest"] = _digest({
                "version": out["version"], "parameters_digest": out["parameters_digest"],
                "engine_digest": ENGINE_DIGEST, "source_digest": out["source_digest"],
                "h1_background": h1_trend, "action": action,
                "permission": {k: v for k, v in permission.items() if k != "proof_digest"}})
        out["permissions"] = permissions
    except (ValueError, TypeError, OverflowError) as exc:
        out["m15_leg"] = {"state": "unknown", "reason": str(exc), "event_id": None,
                          "epsilon": None, "boundaries": None, "confirmation": None}
        out["permissions"] = {side: _permission(False, str(exc)) for side in ("buy", "sell")}
    return out


def public_context(context):
    """Only shared facts: never expose host proof payloads or candidate plans."""
    public = deepcopy({key: context[key] for key in (
        "version", "parameters_digest", "engine_digest", "source_digest", "source_bar_ids",
        "h1_background") if key in context})
    leg = context.get("m15_leg", {})
    public["m15_leg"] = {key: deepcopy(leg[key]) for key in (
        "state", "reason", "event_id", "epsilon", "formation_start", "origin_price", "direction",
        "expires_at_utc", "failure_level", "m15_invalidated") if key in leg}
    for key, fields in (("boundaries", ("upper", "lower")),
                        ("confirmation", ("bar_time", "closed_at_utc", "close", "atr14")),
                        ("failure", ("bar_times", "level", "confirmed_at", "low", "high"))):
        value = leg.get(key)
        public["m15_leg"][key] = ({field: deepcopy(value[field]) for field in fields if field in value}
                                  if isinstance(value, dict) else None)
    public["source_bar_ids"] = {key: deepcopy(context.get("source_bar_ids", {}).get(key, []))
                                for key in ("m15", "m5")}
    return public
