"""Advisory weakening facts and final SL review; no I/O or trading decisions."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math

VERSION = "guardian-weakening-review-v1"
FRAMES = {"M1": 60, "M5": 300}
MAX_AGE = 90.
MAX_MOVE_R = .25


def number(value, name, *, positive=True):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (value <= 0 if positive else value < 0)):
        raise ValueError("invalid " + name)
    return value


def closed_bars(bars, seconds, server_now):
    if not isinstance(bars, list) or len(bars) < 15:
        raise ValueError("closed candle ATR/history unavailable")
    selected = bars[-15:]
    for i, bar in enumerate(selected):
        stamp = bar.get("time")
        if (type(stamp) is not int or stamp <= 0 or stamp % seconds
                or i and stamp-selected[i-1]["time"] != seconds):
            raise ValueError("closed candle times unaligned or discontinuous")
        for key in ("open", "high", "low", "close"):
            number(bar.get(key), "candle " + key)
        if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
            raise ValueError("invalid candle OHLC")
    if not 0 <= server_now-selected[-1]["time"]-seconds < seconds:
        raise ValueError("latest closed candle future or stale")
    return selected


def atr14(bars):
    values = [max(b["high"]-b["low"], abs(b["high"]-p["close"]), abs(b["low"]-p["close"]))
              for p, b in zip(bars[-15:-1], bars[-14:])]
    return number(sum(values)/14, "ATR14")


def rejection(bar, direction, atr):
    body = abs(bar["close"]-bar["open"])
    span = max(bar["high"]-bar["low"], 1e-9)
    wick = (min(bar["open"], bar["close"])-bar["low"] if direction == "BUY"
            else bar["high"]-max(bar["open"], bar["close"]))
    placement = ((bar["close"]-bar["low"])/span if direction == "BUY"
                 else (bar["high"]-bar["close"])/span)
    return wick >= max(1.6*body, .18*atr) and placement >= .55


def build_review(*, direction, entry_time, entry, current_sl, bid, ask, server_now,
                 m1, m5, atr_floor, room_atr, room_spread, room_abs, broker_gap,
                 tick_size, cash_per_price=None, costs=None):
    """A reference stop is never an order or an assertion that the thesis failed."""
    result = dict(version=VERSION, available=False, automatic_action="none",
                  m1_warning=None, m5_weakening=None, review_sl=False,
                  candidate_sl=None, candidate_estimated_net=None, candidate_phase="net_unknown")
    try:
        if direction not in ("BUY", "SELL"):
            raise ValueError("unknown direction")
        for value in (entry_time, entry, bid, ask, server_now, room_atr, room_spread,
                      room_abs, broker_gap, tick_size):
            number(value, "risk review facts")
        number(current_sl, "current SL", positive=False)
        number(atr_floor, "frozen ATR", positive=False)
        if ask < bid:
            raise ValueError("crossed quote")
        b1, b5 = closed_bars(m1, 60, server_now), closed_bars(m5, 300, server_now)
        a1, a5 = atr14(b1), atr14(b5)
        a, b, c = b1[-3:]
        p, q = b5[-2:]
        sign = 1 if direction == "BUY" else -1
        one = lambda bar: sign*(bar["close"]-bar["open"]) < 0
        reject1, reject5 = rejection(c, direction, a1), rejection(q, direction, a5)
        push = (a["time"] >= entry_time and one(b) and one(c)
                and sign*(c["close"]-b["close"]) < 0 and sign*(b["close"]-a["close"]) < 0
                and sign*(c["high"]-b["high"]) < 0 and sign*(c["low"]-b["low"]) < 0
                and not rejection(b, direction, a1) and not reject1)
        extreme = "high" if sign > 0 else "low"
        soft = (entry_time < q["time"]+300 and one(q)
                and sign*(q["close"]-p["close"]) < 0
                and sign*(q[extreme]-p[extreme]) < 0 and not reject5)
        level = p["low"] if sign > 0 else p["high"]
        delta = Decimal(str(q["close"]))-Decimal(str(level))
        room = max(room_atr*max(atr_floor, a5), room_spread*(ask-bid), room_abs, broker_gap)
        result.update(available=True, reason="closed_candles_valid", m1_warning=bool(push),
            m5_weakening=bool(soft), m1_favorable_rejection=bool(reject1),
            m5_favorable_rejection=bool(reject5), m5_event_time=q["time"],
            m5_close_vs_previous_extreme=dict(close=q["close"], level=level,
                previous_time=p["time"], relation="above" if delta > 0 else "below" if delta < 0 else "equal"),
            atr_m1=a1, atr_m5=a5, minimum_room=room,
            review_sl=bool(soft and b["time"] >= entry_time and not reject1))
        if not result["review_sl"]:
            return result
        price = bid if sign > 0 else ask
        anchor = (min(b["low"], c["low"])-.15*a5 if sign > 0
                  else max(b["high"], c["high"])+(ask-bid)+.15*a5)
        raw = min(anchor, price-room) if sign > 0 else max(anchor, price+room)
        grid = Decimal(str(tick_size))
        stop = float((Decimal(str(raw))/grid).to_integral_value(
            rounding=ROUND_FLOOR if sign > 0 else ROUND_CEILING)*grid)
        if stop <= 0 or current_sl > 0 and sign*(stop-current_sl) < tick_size-1e-8:
            result["candidate_reason"] = "no_tighter_stop_with_full_room"
            return result
        result.update(candidate_sl=stop, candidate_reason="reference_only_ai_decides")
        if (type(cash_per_price) in (int,float) and math.isfinite(cash_per_price) and cash_per_price > 0
                and type(costs) in (int,float) and math.isfinite(costs) and costs >= 0):
            net = sign*(stop-entry)*cash_per_price-costs
            result.update(candidate_estimated_net=round(net, 4),
                candidate_phase="profit_lock" if net > 1e-8 else "loss_reduction" if net < -1e-8 else "breakeven")
        return result
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        result["reason"] = str(exc)
        return result


def validate_stop(payload, proof, *, identity, account_namespace, now, bid, ask,
                  current_sl, current_tp, risk, planned_sl, server_offset,
                  server_now, minimum_room, fresh_bars):
    """Bind an SL to the actual POST, then reject stale/crossed or changed sources."""
    if (not isinstance(proof,dict) or proof.get("risk_review_version") != VERSION
            or not isinstance(payload,dict) or not proof.get("snapshot_id")
            or payload.get("snapshot_id") != proof["snapshot_id"] or not account_namespace
            or proof.get("account_namespace") != account_namespace):
        raise ValueError("SL requires original POST snapshot and demo account")
    ticket=payload.get("ticket")
    if type(ticket) is not int or ticket != identity["ticket"]:
        raise ValueError("SL requires exact original ticket")
    record=proof.get("positions",{}).get(str(ticket),{})
    if (record.get("identity") != identity or record.get("risk") != risk
            or record.get("tp") != current_tp or proof.get("server_offset") != server_offset):
        raise ValueError("SL identity, original risk, target or source clock changed")
    for value in (risk, planned_sl, bid, ask, minimum_room, server_now):
        number(value, "current SL facts")
    number(current_sl, "latest actual SL", positive=False)
    observed=number(proof.get("observed_at"), "POST observation time")
    if not 0 <= now-observed <= MAX_AGE or ask < bid:
        raise ValueError("SL review expired, future or crossed")
    if (type(server_offset) not in (int,float) or not math.isfinite(server_offset)
            or not -12 <= server_offset <= 14):
        raise ValueError("SL source UTC offset unknown")
    if not isinstance(payload.get("reasoning"),str) or not payload["reasoning"].strip():
        raise ValueError("SL requires an explanation")
    sign=1 if identity["direction"] == "BUY" else -1
    price=bid if sign > 0 else ask
    original=number(proof.get("bid" if sign > 0 else "ask"), "POST close quote")
    if abs(price-original) > MAX_MOVE_R*risk+1e-8:
        raise ValueError("SL price moved beyond the original 0.25R budget")
    posted_clock=number(proof.get("risk_server_now"), "POST source clock")
    if abs(posted_clock-(observed+server_offset*3600)) > 5.:
        raise ValueError("SL POST candle clock is inconsistent with the observed quote")
    for frame, seconds in FRAMES.items():
        old=closed_bars(proof.get("risk_bars",{}).get(frame), seconds, posted_clock)
        fresh=closed_bars(fresh_bars.get(frame), seconds, server_now)
        if old != fresh:
            raise ValueError("SL closed candle source changed or a new candle closed")
    if ((current_sl > 0 and sign*(planned_sl-current_sl) <= 0)
            or sign*(price-planned_sl)+1e-8 < minimum_room):
        raise ValueError("SL would widen protection or violate full M5 pullback room")
    return planned_sl
