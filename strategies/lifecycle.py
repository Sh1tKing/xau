"""Durable event consumption and saved position lifetimes; no trading API calls.

Use one ledger instance per process. The lock serializes threads, not independent
processes. An event is consumed before order submission and is never released.
"""
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import threading


def _empty_state():
    return {"version": 1, "events": {}, "positions": {}, "cooldown_until": 0.0}


def _time(value, *, zero=False):
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and (value >= 0 if zero else value > 0))
    except OverflowError:
        return False


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _event_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 512
            and value == value.strip() and not any(ord(c) < 32 for c in value))


def _json_safe(value):
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_safe(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json_safe(item) for key, item in value.items())
    return False


def _metadata(value):
    if type(value) is not dict or not _json_safe(value):
        return False
    if "max_hold_minutes" in value and not _integer(value["max_hold_minutes"], 1, 10080):
        return False
    if "cooldown_seconds" in value and not _integer(value["cooldown_seconds"], 0, 604800):
        return False
    return True


def _lifecycle_matches(metadata, max_hold_minutes, cooldown_seconds):
    return (("max_hold_minutes" not in metadata or metadata["max_hold_minutes"] == max_hold_minutes)
            and ("cooldown_seconds" not in metadata or metadata["cooldown_seconds"] == cooldown_seconds))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate(state):
    if (type(state) is not dict or set(state) != {"version", "events", "positions", "cooldown_until"}
            or type(state["version"]) is not int or state["version"] != 1
            or type(state["events"]) is not dict or type(state["positions"]) is not dict
            or not _time(state["cooldown_until"], zero=True)):
        raise ValueError("invalid lifecycle state header or cooldown")
    for event_id, event in state["events"].items():
        if (not _event_id(event_id) or type(event) is not dict
                or set(event) != {"reserved_at_utc", "metadata"}
                or not _time(event["reserved_at_utc"]) or not _metadata(event["metadata"])):
            raise ValueError(f"invalid saved event: {event_id!r}")
    for ticket, position in state["positions"].items():
        if (type(ticket) is not str or not ticket.isascii() or not ticket.isdecimal()
                or len(ticket) > 20 or str(int(ticket)) != ticket
                or not _integer(int(ticket), 1, 2**64 - 1)
                or type(position) is not dict
                or set(position) != {"event_id", "opened_at_utc", "max_hold_minutes", "cooldown_seconds"}
                or position["event_id"] not in state["events"]
                or not _time(position["opened_at_utc"])
                or not _integer(position["max_hold_minutes"], 1, 10080)
                or not _integer(position["cooldown_seconds"], 0, 604800)
                or not _time(position["opened_at_utc"] + position["max_hold_minutes"] * 60)):
            raise ValueError(f"invalid saved position: {ticket!r}")
        metadata = state["events"][position["event_id"]]["metadata"]
        if not _lifecycle_matches(metadata, position["max_hold_minutes"], position["cooldown_seconds"]):
            raise ValueError(f"saved position lifecycle differs from reserved event: {ticket!r}")


class EventLedger:
    """Fail closed for new entries while retaining readable saved deadlines.

    ``error`` is sticky for this instance. A repaired ledger requires reopening;
    failures never overwrite the last successfully persisted in-memory state.
    Missing files are initialized in memory and created on the first mutation.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = _empty_state()
        self._loaded = False
        self.error = ""
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                state = json.load(handle, object_pairs_hook=_unique_object)
            _validate(state)
            self._state = state
            self._loaded = True
        except FileNotFoundError:
            self._loaded = True
        except Exception as exc:
            self.error = f"lifecycle state load failed: {type(exc).__name__}: {exc}"

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def entry_error(self, event_id: str, now: float) -> str:
        with self._lock:
            if self.error:
                return self.error
            if not _event_id(event_id):
                return "invalid lifecycle event id"
            if not _time(now):
                return "invalid lifecycle UTC time"
            if event_id in self._state["events"]:
                return "lifecycle event already consumed"
            if self._state["cooldown_until"] > now:
                return f"lifecycle cooldown active until {self._state['cooldown_until']}"
            return ""

    def _persist(self, state):
        temporary = None
        try:
            _validate(state)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp",
                                             delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(state, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._state = state
            return True
        except Exception as exc:
            self.error = f"lifecycle state write failed: {type(exc).__name__}: {exc}"
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def reserve(self, event_id: str, now: float, metadata: dict | None = None) -> bool:
        with self._lock:
            if self.entry_error(event_id, now):
                return False
            metadata = {} if metadata is None else metadata
            try:
                if not _metadata(metadata):
                    return False
                frozen_metadata = copy.deepcopy(metadata)
            except (RecursionError, TypeError, ValueError):
                return False
            state = copy.deepcopy(self._state)
            state["events"][event_id] = {"reserved_at_utc": now, "metadata": frozen_metadata}
            return self._persist(state)

    def record_position(self, ticket: int, event_id: str, opened_at_utc: float,
                        max_hold_minutes: int, cooldown_seconds: int) -> bool:
        with self._lock:
            if (not self._loaded or not _integer(ticket, 1, 2**64 - 1) or not _event_id(event_id)
                    or event_id not in self._state["events"] or not _time(opened_at_utc)
                    or not _integer(max_hold_minutes, 1, 10080)
                    or not _integer(cooldown_seconds, 0, 604800)
                    or not _time(opened_at_utc + max_hold_minutes * 60)):
                return False
            metadata = self._state["events"][event_id]["metadata"]
            if not _lifecycle_matches(metadata, max_hold_minutes, cooldown_seconds):
                return False
            saved = self._state["positions"].get(str(ticket))
            if saved is not None:
                return saved["event_id"] == event_id
            state = copy.deepcopy(self._state)
            state["positions"][str(ticket)] = {
                "event_id": event_id, "opened_at_utc": opened_at_utc,
                "max_hold_minutes": max_hold_minutes, "cooldown_seconds": cooldown_seconds,
            }
            return self._persist(state)

    def closed_positions(self, active_tickets: set[int], now: float) -> list[int]:
        with self._lock:
            if (not self._loaded or not _time(now) or type(active_tickets) is not set
                    or any(not _integer(ticket, 1, 2**64 - 1) for ticket in active_tickets)):
                return []
            closed = sorted(int(ticket) for ticket in self._state["positions"]
                            if int(ticket) not in active_tickets)
            if not closed:
                return []
            state = copy.deepcopy(self._state)
            for ticket in closed:
                position = state["positions"].pop(str(ticket))
                state["cooldown_until"] = max(state["cooldown_until"], now + position["cooldown_seconds"])
            return closed if self._persist(state) else []

    def due_tickets(self, now: float) -> tuple[int, ...]:
        with self._lock:
            if not _time(now):
                return ()
            return tuple(sorted(int(ticket) for ticket, position in self._state["positions"].items()
                                if now >= position["opened_at_utc"] + position["max_hold_minutes"] * 60))
