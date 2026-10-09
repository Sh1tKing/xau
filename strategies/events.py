"""Shared closed-bar event contract. No strategy fingerprint enters identity.

Time/identity checks always run. Supplied frames additionally prove source
membership, ordering, continuity and that every context bar has closed. Hosts
must bind this source check at assessment and retain that original evidence.
"""
from dataclasses import dataclass
from datetime import datetime
import math
from zoneinfo import ZoneInfo

SECONDS = {"M5": 300, "M15": 900}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("unsupported numeric type")
    if not math.isfinite(value):
        raise ValueError("nonfinite number")
    return value


def _stamp(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("unknown UTC time")
        return value.timestamp()
    return _number(value)


def _raw(value, timeframe):
    value = _number(value)
    if value != int(value) or value % SECONDS[timeframe]:
        raise ValueError("invalid raw bar time")
    return int(value)


@dataclass(frozen=True)
class EntryEvent:
    event_id: str
    region_id: str
    episode_start_bar_time: int
    action: str
    trigger_timeframe: str
    trigger_bar_time: int
    trigger_close_utc: float
    valid_until_utc: float
    source_refs: tuple
    version: str = "entry-event-v1"

    def to_dict(self):
        return {**{key: getattr(self, key) for key in self.__dataclass_fields__
                   if key != "source_refs"},
                "source_refs": [{"timeframe": tf, "bar_time": raw}
                                for tf, raw in self.source_refs]}


def make_entry_event(*, event_id, region_id, episode_start_bar_time, action,
                     trigger_timeframe, trigger_bar_time, trigger_close_utc,
                     valid_until_utc, source_refs):
    """Create immutable event; caller supplies identity from actual source."""
    event = EntryEvent(event_id, region_id, episode_start_bar_time, action,
                       trigger_timeframe, trigger_bar_time, _stamp(trigger_close_utc),
                       _stamp(valid_until_utc),
                       tuple((ref["timeframe"], ref["bar_time"]) for ref in source_refs))
    _shape(event.to_dict())
    return event


def _shape(event):
    if not isinstance(event, dict) or event.get("version") != "entry-event-v1":
        raise ValueError("missing or unsupported entry event")
    for key in ("event_id", "region_id"):
        if not isinstance(event.get(key), str) or not event[key].strip():
            raise ValueError("missing " + key)
    tf = event.get("trigger_timeframe")
    if tf not in SECONDS or event.get("action") not in ("buy", "sell"):
        raise ValueError("invalid event action or timeframe")
    trigger = _raw(event.get("trigger_bar_time"), tf)
    episode = _raw(event.get("episode_start_bar_time"), tf)
    if episode > trigger:
        raise ValueError("episode after trigger")
    close, end = _number(event.get("trigger_close_utc")), _number(event.get("valid_until_utc"))
    if not close < end <= close + 300:
        raise ValueError("invalid event TTL")
    refs = event.get("source_refs")
    if not isinstance(refs, list) or not refs:
        raise ValueError("missing source references")
    seen = set()
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"timeframe", "bar_time"} or ref["timeframe"] not in SECONDS:
            raise ValueError("invalid source reference")
        pair = (ref["timeframe"], _raw(ref["bar_time"], ref["timeframe"]))
        if pair in seen:
            raise ValueError("duplicate source reference")
        seen.add(pair)
    if (tf, trigger) not in seen:
        raise ValueError("trigger missing from sources")


def _window(evidence, now, close):
    windows = evidence.get("windows")
    if not isinstance(windows, list):
        raise ValueError("missing session window")
    matches = [w for w in windows if isinstance(w, dict) and w.get("active") is True]
    if len(matches) != 1:
        raise ValueError("missing or ambiguous active session window")
    w = matches[0]
    if w.get("zone") != evidence.get("zone") or w.get("local_date") != evidence.get("local_date"):
        raise ValueError("session identity mismatch")
    if w.get("zone") not in ("Europe/London", "America/New_York"):
        raise ValueError("unsupported legacy session zone")
    if not evidence.get("event_id", "").startswith(w["zone"] + "/" + w["local_date"] + "/"):
        raise ValueError("legacy session event identity mismatch")
    start, end = (datetime.fromisoformat(w[key]) for key in ("entry_start_utc", "entry_end_utc"))
    start, end = _stamp(start), _stamp(end)
    if datetime.fromtimestamp(close, ZoneInfo(w["zone"])).date().isoformat() != w["local_date"]:
        raise ValueError("legacy session date mismatch")
    if not start <= close < end or not start <= now < end:
        raise ValueError("outside original session window")
    return end


def validate_entry_event(evidence, observed_utc, server_utc_offset=None,
                         df_m5=None, df_m15=None, *, legacy_session=False):
    """Return a specific error or empty string; never guess UTC/window/source."""
    try:
        now = _stamp(observed_utc)
        offset = _number(server_utc_offset)
        if abs(offset) > 14:
            raise ValueError("invalid server UTC offset")
        if not isinstance(evidence, dict):
            raise ValueError("invalid evidence")
        trigger = evidence.get("closed_trigger")
        if not isinstance(trigger, dict) or trigger.get("confirmed") is not True:
            raise ValueError("missing closed trigger")
        tf = trigger.get("timeframe")
        if tf not in SECONDS:
            raise ValueError("invalid trigger timeframe")
        raw = _raw(trigger.get("bar_time"), tf)
        close = raw + SECONDS[tf] - offset * 3600
        event = evidence.get("entry_event")
        if event is None and legacy_session:
            if tf != "M5" or not isinstance(evidence.get("event_id"), str) or not evidence["event_id"]:
                raise ValueError("invalid legacy session identity")
            end = _window(evidence, now, close)
            retest = evidence.get("retest_bar")
            if retest is not None and (_number(retest["close_utc"]) != close or _raw(retest["time"], tf) != raw):
                raise ValueError("legacy retest mismatch")
            expires = min(close + 300, end)
        else:
            _shape(event)
            if (evidence.get("event_id") != event["event_id"]
                    or (evidence.get("action") is not None and evidence["action"] != event["action"])
                    or (trigger.get("action") is not None and trigger["action"] != event["action"])):
                raise ValueError("event identity or action mismatch")
            if event["trigger_timeframe"] != tf or event["trigger_bar_time"] != raw or event["trigger_close_utc"] != close:
                raise ValueError("trigger UTC or source mismatch")
            expires = event["valid_until_utc"]
            if legacy_session:
                end = _window(evidence, now, close)
                if expires > end:
                    raise ValueError("event exceeds original session window")
        if not close <= now < expires:
            raise ValueError("future or expired closed trigger")
        latest = math.floor((now + offset * 3600) / SECONDS[tf]) * SECONDS[tf] - SECONDS[tf]
        if raw != latest:
            raise ValueError("stale closed trigger")
        for frame_tf, frame in (("M5", df_m5), ("M15", df_m15)):
            if frame is None:
                continue
            if len(frame) == 0 or "time" not in frame.columns:
                raise ValueError("missing source frame")
            times = [_raw(value, frame_tf) for value in frame["time"].tolist()]
            for column in ("open", "high", "low", "close"):
                if column in frame.columns:
                    for value in frame[column].tolist():
                        _number(value)
            if any(b-a != SECONDS[frame_tf] for a, b in zip(times, times[1:])):
                raise ValueError("duplicate, out of order or gapped source data")
            if any(t + SECONDS[frame_tf] - offset*3600 > now for t in times):
                raise ValueError("future or unclosed source context")
            if frame_tf == tf and times[-1] != raw:
                raise ValueError("trigger is not latest source bar")
            if event is not None:
                for ref in event["source_refs"]:
                    if ref["timeframe"] == frame_tf and ref["bar_time"] not in times:
                        raise ValueError("source reference absent from input")
        return ""
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError) as exc:
        return str(exc) or "invalid entry event"
