"""Closed-M5 facts for the frozen research session-retest candidate.

Pure data inspection: no terminal, configuration, network, or order imports.
Raw MT5 timestamps remain in evidence; an explicit server offset alone maps
them to UTC. ATR matches the lab's trailing 14 True Range arithmetic mean.
H1, execution prices, protective buffers, and account gates belong to the host.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from crypto_demo.strategies.events import make_entry_event


DEFAULT_PARAMETERS = {
    "open_minute": 480, "range_minutes": 30, "entry_end_minute": 660,
    "min_width_atr": 1.0, "max_width_atr": 6.0,
    "break_atr": .05, "retest_atr": .25, "hold_atr": .05,
    "invalidation_atr": .30, "max_retest_bars": 6,
    "max_retest_seconds": 1800,
}
ZONES = ("Europe/London", "America/New_York")
BAR_SECONDS = 300
ATR_PERIOD = 14


def _blank():
    return {"available": False, "closed_trigger": {
        "confirmed": False, "timeframe": "M5", "bar_time": None},
        "blockers": [], "event_id": None, "zone": None, "local_date": None,
        "reference_close": None, "m5_atr": None, "protection_extreme": None,
        "protection_bars": [], "opening_bars": [], "opening_bar_times": [],
        "box_low": None, "box_high": None, "opening_atr": None,
        "breakout_bar": None, "retest_bar": None,
        "candidate_bars": [], "candidate_bar_times": [], "windows": []}


def _parameters(values):
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ValueError("parameters must be a dict")
    p = {**DEFAULT_PARAMETERS, **values}
    for key in DEFAULT_PARAMETERS:
        value = p[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("invalid parameter " + key)
        try:
            finite = math.isfinite(value)
        except OverflowError as exc:
            raise ValueError("invalid parameter " + key) from exc
        if not finite:
            raise ValueError("invalid parameter " + key)
    for key in ("open_minute", "range_minutes", "entry_end_minute", "max_retest_bars", "max_retest_seconds"):
        if p[key] != int(p[key]):
            raise ValueError("non-integer parameter " + key)
        p[key] = int(p[key])
    if not (0 <= p["open_minute"] < p["open_minute"] + p["range_minutes"] < p["entry_end_minute"] <= 1440):
        raise ValueError("invalid session window")
    if any(p[key] % 5 for key in ("open_minute", "range_minutes", "entry_end_minute")):
        raise ValueError("session boundaries must align with M5")
    if not 0 < p["min_width_atr"] <= p["max_width_atr"]:
        raise ValueError("invalid width bounds")
    if any(p[key] < 0 for key in ("break_atr", "retest_atr", "hold_atr", "invalidation_atr")):
        raise ValueError("negative ATR parameter")
    if p["max_retest_bars"] < 1 or p["max_retest_seconds"] < BAR_SECONDS:
        raise ValueError("invalid retest limit")
    return p


def _bar(row):
    return {key: row[key] for key in ("time", "open", "high", "low", "close", "atr", "open_utc", "close_utc")}


def session_retest_facts(df_m5, decision_utc: datetime,
                         server_offset_hours: float | None,
                         action: str, parameters: dict) -> dict:
    """Return facts/blockers for the current London or New York date window.

    Only the first breakout in either direction is eligible. Its first retest
    must be the latest supplied closed bar and still within its first 300s
    after close. Missing opening/candidate bars fail closed rather than being
    sorted, filled, or reinterpreted as another opportunity.
    """
    result = _blank()
    def blocked(reason):
        result["blockers"].append(reason)
        return result

    if action not in ("buy", "sell"):
        return blocked("invalid_action")
    if not isinstance(decision_utc, datetime) or decision_utc.tzinfo is None or decision_utc.utcoffset() is None:
        return blocked("decision_time_zone_missing")
    if server_offset_hours is None:
        return blocked("server_time_zone_missing")
    try:
        if isinstance(server_offset_hours, bool):
            raise ValueError
        offset = float(server_offset_hours)
        if not math.isfinite(offset) or abs(offset) > 14:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return blocked("invalid_server_offset")
    try:
        p = _parameters(parameters)
    except ValueError as exc:
        return blocked("invalid_parameters: " + str(exc))
    decision = decision_utc.astimezone(timezone.utc)
    now = decision.timestamp()
    active = None
    try:
        for name in ZONES:
            zone = ZoneInfo(name)
            local = decision.astimezone(zone)
            midnight = datetime.combine(local.date(), time(), zone)
            start = midnight + timedelta(minutes=p["open_minute"])
            entry = start + timedelta(minutes=p["range_minutes"])
            end = midnight + timedelta(minutes=p["entry_end_minute"])
            in_window = entry <= local < end
            window = {"zone": name, "local_date": local.date().isoformat(),
                      "opening_start_utc": start.astimezone(timezone.utc).isoformat(),
                      "entry_start_utc": entry.astimezone(timezone.utc).isoformat(),
                      "entry_end_utc": end.astimezone(timezone.utc).isoformat(),
                      "active": in_window}
            result["windows"].append(window)
            if in_window:
                if active is not None:
                    return blocked("overlapping_session_windows")
                active = (name, local.date().isoformat(), start.timestamp(), entry.timestamp(), end.timestamp())
    except ZoneInfoNotFoundError:
        return blocked("time_zone_missing")
    if active is None:
        return blocked("outside_session_window")
    name, day, start_ts, entry_ts, end_ts = active
    result.update(zone=name, local_date=day,
                  event_id=f"{name}/{day}/{p['open_minute']}-{p['entry_end_minute']}",
                  server_offset_hours=offset, parameters={key: p[key] for key in DEFAULT_PARAMETERS})
    required = ("time", "open", "high", "low", "close")
    if df_m5 is None or len(df_m5) == 0:
        return blocked("missing_m5_data")
    if not set(required).issubset(df_m5.columns):
        return blocked("missing_m5_columns")
    try:
        rows = []
        previous = None
        trs = []
        for source in df_m5[list(required)].itertuples(index=False, name=None):
            vals = [float(value) for value in source]
            if not all(math.isfinite(value) for value in vals):
                return blocked("nonfinite_m5_data")
            raw, o, h, l, c = vals
            if raw != int(raw) or raw % BAR_SECONDS != 0:
                return blocked("unaligned_m5_time")
            if previous is not None and raw <= previous["time"]:
                return blocked("duplicate_or_out_of_order_m5_time")
            if h < max(o, l, c) or l > min(o, h, c):
                return blocked("invalid_m5_ohlc")
            utc = raw - offset * 3600
            if utc + BAR_SECONDS > now:
                return blocked("unclosed_or_future_m5_evidence")
            tr = h - l if previous is None else max(h-l, abs(h-previous["close"]), abs(l-previous["close"]))
            if not math.isfinite(tr):
                return blocked("nonfinite_m5_true_range")
            trs.append(tr)
            atr = sum(trs[-ATR_PERIOD:]) / ATR_PERIOD if len(trs) >= ATR_PERIOD else None
            if atr is not None and not math.isfinite(atr):
                return blocked("nonfinite_m5_atr")
            row = {"time": int(raw), "open": o, "high": h, "low": l, "close": c,
                   "atr": atr, "open_utc": utc, "close_utc": utc + BAR_SECONDS}
            rows.append(row)
            previous = row
    except (TypeError, ValueError, OverflowError):
        return blocked("invalid_m5_data")
    opening = [r for r in rows if start_ts <= r["open_utc"] < entry_ts]
    result["opening_bars"] = [_bar(r) for r in opening]
    result["opening_bar_times"] = [r["time"] for r in opening]
    expected = list(range(int(start_ts), int(entry_ts), BAR_SECONDS))
    if [r["open_utc"] for r in opening] != expected:
        return blocked("incomplete_opening_window")
    a = opening[-1]["atr"]
    if a is None or a <= 0:
        return blocked("insufficient_m5_atr_history")
    lo, hi = min(r["low"] for r in opening), max(r["high"] for r in opening)
    result.update(box_low=lo, box_high=hi, opening_atr=a, available=True)
    if not p["min_width_atr"] * a <= hi-lo <= p["max_width_atr"] * a:
        return blocked("opening_width_outside_atr_bounds")
    candidates = [r for r in rows if entry_ts <= r["open_utc"] < end_ts]
    result["candidate_bars"] = [_bar(r) for r in candidates]
    result["candidate_bar_times"] = [r["time"] for r in candidates]
    latest_expected = math.floor(now / BAR_SECONDS) * BAR_SECONDS - BAR_SECONDS
    last_open = candidates[-1]["open_utc"] if candidates else entry_ts - BAR_SECONDS
    expected_candidates = list(range(int(entry_ts), int(last_open + BAR_SECONDS), BAR_SECONDS))
    if [r["open_utc"] for r in candidates] != expected_candidates:
        return blocked("missing_or_stale_candidate_sequence")
    stale = rows[-1]["open_utc"] != latest_expected
    breakout_index, side = None, None
    for j, row in enumerate(candidates):
        atr = row["atr"]
        if atr is None or atr <= 0:
            return blocked("insufficient_m5_atr_history")
        direction = "buy" if row["close"] > hi + p["break_atr"] * atr else (
                    "sell" if row["close"] < lo - p["break_atr"] * atr else None)
        if direction:
            breakout_index, side = j, direction
            result["breakout_bar"] = {**_bar(row), "action": side}
            break
    if breakout_index is None:
        return blocked("missing_or_stale_candidate_sequence" if stale else "no_first_breakout")
    if side != action:
        return blocked("first_breakout_opposite_action")
    breakout = candidates[breakout_index]
    boundary = hi if side == "buy" else lo
    following = candidates[breakout_index + 1:breakout_index + 1 + p["max_retest_bars"]]
    for row in following:
        elapsed = row["close_utc"] - breakout["close_utc"]
        if elapsed > p["max_retest_seconds"]:
            return blocked("first_retest_timeout")
        atr = row["atr"]
        if atr is None or atr <= 0:
            return blocked("insufficient_m5_atr_history")
        invalid = row["close"] < boundary - p["invalidation_atr"] * atr if side == "buy" else row["close"] > boundary + p["invalidation_atr"] * atr
        if invalid:
            result["invalidation_bar"] = _bar(row)
            return blocked("first_breakout_invalidated")
        touch = row["low"] <= boundary + p["retest_atr"] * atr if side == "buy" else row["high"] >= boundary - p["retest_atr"] * atr
        defended = row["close"] > boundary + p["hold_atr"] * atr if side == "buy" else row["close"] < boundary - p["hold_atr"] * atr
        if touch and defended:
            result["retest_bar"] = {**_bar(row), "touch": True, "defended": True}
            index = next(i for i, item in enumerate(rows) if item["time"] == row["time"])
            protection = rows[index-2:index+1]
            result.update(reference_close=row["close"], m5_atr=atr,
                          protection_extreme=min(r["low"] for r in protection) if side == "buy" else max(r["high"] for r in protection),
                          protection_bars=[_bar(r) for r in protection])
            if row["time"] != rows[-1]["time"]:
                return blocked("first_retest_missed")
            if not row["close_utc"] <= now < row["close_utc"] + BAR_SECONDS:
                return blocked("first_retest_expired")
            result["closed_trigger"] = {"confirmed": True, "timeframe": "M5", "bar_time": row["time"]}
            result["action"] = side
            sources = opening + [breakout, row]
            result["entry_event"] = make_entry_event(
                event_id=result["event_id"], region_id=result["event_id"],
                episode_start_bar_time=opening[0]["time"], action=side,
                trigger_timeframe="M5", trigger_bar_time=row["time"],
                trigger_close_utc=row["close_utc"],
                valid_until_utc=min(row["close_utc"] + BAR_SECONDS, end_ts),
                source_refs=[{"timeframe": "M5", "bar_time": r["time"]}
                             for r in sources]).to_dict()
            return result
    last_close = candidates[-1]["close_utc"] if candidates else breakout["close_utc"]
    if len(following) >= p["max_retest_bars"] or now >= breakout["close_utc"] + p["max_retest_seconds"] or last_close - breakout["close_utc"] >= p["max_retest_seconds"]:
        return blocked("first_retest_timeout")
    if stale:
        return blocked("missing_or_stale_candidate_sequence")
    return blocked("no_first_retest")
