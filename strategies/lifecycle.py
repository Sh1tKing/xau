"""Durable event consumption and saved position lifetimes; no trading API calls.

Use one ledger instance per process. The lock serializes threads, not independent
processes. An event is consumed before order submission and is never released.
"""
import hashlib
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import threading


HOLD_REVIEW_VERSION = "holding-review-v1"


def holding_policy(contract):
    """Explicit host policy, including saved autonomous contracts from older builds.

    The original clock/contract is never rewritten. Following the user's policy
    change, autonomous analysis positions review that clock instead of closing
    solely on time; historical fixed-event strategies keep their frozen exit.
    """
    return "ai_review" if isinstance(contract, dict) and contract.get("analysis_mode") is True else "hard_exit"


def holding_status(state, ticket, now):
    """Read the original position clock, without inferring it from current rules."""
    if not _time(now) or not _integer(ticket, 1, 2**64 - 1) or not isinstance(state, dict):
        return None
    position = state.get("positions", {}).get(str(ticket))
    if not isinstance(position, dict):
        return None
    opened = position.get("opened_at_utc")
    minutes = position.get("max_hold_minutes")
    event = state.get("events", {}).get(position.get("event_id"), {})
    metadata = event.get("metadata", {})
    if (not _time(opened) or not _integer(minutes, 1, 10080)
            or not isinstance(metadata, dict)
            or not _lifecycle_matches(metadata, minutes, position.get("cooldown_seconds"))):
        return None
    threshold = opened + minutes * 60
    if not _time(threshold):
        return None
    policy = holding_policy(metadata.get("contract"))
    reached = now >= threshold
    return {"version": HOLD_REVIEW_VERSION, "ticket": ticket, "policy": policy,
            "opened_at_utc": opened, "threshold_minutes": minutes,
            "threshold_at_utc": threshold, "held_minutes": round(max(0., now - opened) / 60., 2),
            "threshold_reached": reached, "review_due": policy == "ai_review" and reached,
            "time_alone_can_close": policy == "hard_exit"}


def execution_token(event_id, symbol, magic, action):
    """Compact deterministic key; the ledger also rejects hash collisions."""
    key = json.dumps([event_id, symbol, magic, action], separators=(",", ":"))
    return "EV" + hashlib.sha256(key.encode()).hexdigest()[:20] + ("B" if action == "buy" else "S")


def recovery_event(position, state, symbol, magic):
    """Return only an exact token match or one unambiguous original SR receipt."""
    if getattr(position, "symbol", None) != symbol or getattr(position, "magic", None) != magic:
        return None
    action = "buy" if getattr(position, "type", None) == 0 else "sell" if getattr(position, "type", None) == 1 else None
    if action is None:
        return None
    comment = str(getattr(position, "comment", ""))
    bound = {p["event_id"] for p in state.get("positions", {}).values()}
    candidates = []
    for event_id, event in state.get("events", {}).items():
        meta = event["metadata"]
        if event_id in bound or not meta.get("max_hold_minutes") or meta.get("server_utc_offset") is None:
            continue
        opened = float(getattr(position, "time", 0))-meta["server_utc_offset"]*3600
        token = meta.get("execution_token")
        if token:
            if (comment in (token, "_SR_"+token) and meta.get("symbol") == symbol and meta.get("magic") == magic
                    and meta.get("action") == action):
                candidates.append((event_id, opened, meta))
        elif "_SR_" in comment and comment.endswith(action.upper()):
            original_action = meta.get("action") or (event_id.rsplit("/", 1)[-1] if event_id.endswith(("/buy", "/sell")) else None)
            # Historical session IDs omit direction: the original evidence must prove it.
            evidence = meta.get("evidence", {})
            original_action = original_action or evidence.get("closed_trigger", {}).get("action")
            if original_action == action and abs(opened-event["reserved_at_utc"]) <= 120:
                candidates.append((event_id, opened, meta))
    return candidates[0] if len(candidates) == 1 else None


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
    if "analysis_order_scope" in value and not _event_id(value["analysis_order_scope"]):
        return False
    if "execution_token" in value:
        token = value["execution_token"]
        if (type(token) is not str or len(token) != 23 or not token.startswith("EV")
                or token[-1] not in "BS" or any(c not in "0123456789abcdef" for c in token[2:-1])
                or value.get("action") not in ("buy", "sell")
                or token[-1] != ("B" if value.get("action") == "buy" else "S")
                or type(value.get("symbol")) is not str
                or type(value.get("magic")) is not int):
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
    tokens = set()
    for event_id, event in state["events"].items():
        if (not _event_id(event_id) or type(event) is not dict
                or set(event) != {"reserved_at_utc", "metadata"}
                or not _time(event["reserved_at_utc"]) or not _metadata(event["metadata"])):
            raise ValueError(f"invalid saved event: {event_id!r}")
        meta = event["metadata"]
        token = meta.get("execution_token")
        if token:
            if token in tokens or token != execution_token(event_id, meta["symbol"], meta["magic"], meta["action"]):
                raise ValueError("duplicate or mismatched execution token")
            if meta.get("event_id", event_id) != event_id:
                raise ValueError("execution metadata event identity mismatch")
            tokens.add(token)
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

    def analysis_scope_error(self, scope: str, now: float) -> str:
        with self._lock:
            if self.error:
                return self.error
            if not _event_id(scope) or not _time(now):
                return "invalid analysis order scope"
            if any(event["metadata"].get("analysis_order_scope") == scope for event in self._state["events"].values()):
                return "analysis checkpoint order budget already consumed across directions"
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
            token = frozen_metadata.get("execution_token")
            scope = frozen_metadata.get("analysis_order_scope")
            if scope is not None and self.analysis_scope_error(scope, now):
                return False
            if token and any(e["metadata"].get("execution_token") == token for e in self._state["events"].values()):
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
