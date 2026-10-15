"""Pure validation of TP changes tied to a successful guardian POST and closed bars."""
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math

VERSION = "guardian-target-review-v1"
FRAMES = {"M5": 300, "M15": 900, "H1": 3600}


def number(value, name, *, positive=True):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or positive and value <= 0):
        raise ValueError("invalid " + name)
    return value


def validate_target(payload, proof, *, identity, account_namespace, now, bid, ask,
                    current_sl, current_tp, risk, cash_per_price, costs, tick_size,
                    broker_gap, atr_floor, rules, fresh_bars):
    """No I/O, clamping, strategy guesses, or inferred fills; host supplies fresh facts."""
    if (not isinstance(proof, dict) or proof.get("version") != VERSION
            or not isinstance(rules, dict) or rules.get("version") != VERSION
            or not isinstance(payload, dict) or payload.get("snapshot_id") != proof.get("snapshot_id")
            or not proof.get("snapshot_id") or not account_namespace
            or proof.get("account_namespace") != account_namespace):
        raise ValueError("TP requires the original successful POST and demo account namespace")
    ticket = payload.get("ticket")
    if type(ticket) is not int or ticket != identity["ticket"]:
        raise ValueError("TP requires an exact original ticket")
    record = proof.get("positions", {}).get(str(ticket))
    if (not isinstance(record, dict) or record.get("identity") != identity
            or record.get("risk") != risk or record.get("tp") != current_tp):
        raise ValueError("TP position identity, original risk or current target changed")
    observed = number(proof.get("observed_at"), "review time")
    if not 0 <= now-observed <= rules["max_age_seconds"]:
        raise ValueError("TP review expired or from the future")
    values = (bid, ask, current_sl, current_tp, risk, cash_per_price, tick_size, broker_gap, atr_floor)
    for value in values:
        number(value, "current TP execution facts")
    if ask < bid or not math.isfinite(costs) or costs < 0:
        raise ValueError("invalid TP quote or fees")
    direction = identity["direction"]
    sign = 1 if direction == "BUY" else -1
    entry = number(identity["entry"], "original entry")
    price = bid if sign > 0 else ask
    original_price = proof.get("bid") if sign > 0 else proof.get("ask")
    if abs(price-number(original_price,"original close quote")) > risk*rules["max_move_r"] + 1e-8:
        raise ValueError("TP quote moved beyond the original 0.25R review budget")
    refs = payload.get("take_profit_evidence")
    if (not isinstance(refs, list) or not 1 <= len(refs) <= 4
            or not isinstance(payload.get("reasoning"), str) or not payload["reasoning"].strip()):
        raise ValueError("TP needs a closed candle basis and explanation")
    offset = number(proof.get("server_offset"), "saved server offset", positive=False)
    if not -12 <= offset <= 14:
        raise ValueError("invalid TP source clock")
    seen = set()
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {"timeframe", "time"}
                or ref.get("timeframe") not in FRAMES or type(ref.get("time")) is not int):
            raise ValueError("invalid closed candle reference")
        frame, stamp = ref["timeframe"], ref["time"]
        key = (frame, stamp)
        if key in seen or stamp <= 0 or stamp % FRAMES[frame]:
            raise ValueError("duplicate or unaligned closed candle reference")
        seen.add(key)
        original = [b for b in proof.get("bars", {}).get(frame, []) if b.get("time") == stamp]
        fresh = [b for b in fresh_bars.get(frame, []) if b.get("time") == stamp]
        if (len(original) != 1 or len(fresh) != 1
                or stamp+FRAMES[frame] > observed+offset*3600 + 1e-8):
            raise ValueError("TP candle is missing or was not closed at POST time")
        for name in ("open", "high", "low", "close", "tick_volume"):
            left, right = original[0].get(name), fresh[0].get(name)
            number(left,"original candle "+name,positive=name != "tick_volume")
            number(right,"current candle "+name,positive=name != "tick_volume")
            if left != right:
                raise ValueError("TP closed candle source changed")
        bar = original[0]
        if not bar["low"] <= min(bar["open"],bar["close"]) <= max(bar["open"],bar["close"]) <= bar["high"]:
            raise ValueError("invalid TP source candle OHLC")
    target = number(payload.get("new_take_profit"), "new take profit")
    # A target is rounded toward reachable profit, never farther than the AI asked.
    grid = Decimal(str(tick_size))
    target = float((Decimal(str(target))/grid).to_integral_value(
        rounding=ROUND_FLOOR if sign > 0 else ROUND_CEILING)*grid)
    if abs(target-current_tp) + 1e-8 < max(rules["min_change_abs"], rules["min_change_atr"]*atr_floor):
        raise ValueError("TP change is too small; do not chase noise")
    if (sign*(target-price) + 1e-8 < max(broker_gap,3*(ask-bid))
            or sign*(target-current_sl) <= 0
            or sign*(target-entry)*cash_per_price-costs < .20-1e-8
            or sign*(target-entry) > rules["max_r"]*risk + 1e-8):
        raise ValueError("TP fails executable distance, positive net target or original 6R bound")
    if sign*(target-current_tp) > 0 and sign*(current_sl-entry)*cash_per_price-costs < -1e-8:
        raise ValueError("extending TP requires the actual stop to cover costs first")
    return target
