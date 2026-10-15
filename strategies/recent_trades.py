"""Pure, optional facts from complete broker settlements, never trading signals.

The host owns atomic persistence and reliable broker-to-UTC conversion. Coverage
describes this cache's observation period, not a complete historical broker scan.
No filesystem, clock, MT5, AI, or notification operations belong in this module.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

from .settlement import PositionSettlement

VERSION = "recent-settled-trades-v1"
MAX_RECORDS = 64
MAX_PARTITIONS = 64
WINDOW_SECONDS = 6 * 60 * 60
CONTEXT_RECORDS = 6
ENTRY_KEYS = frozenset(("direction_policy_version", "route", "h1_background",
                        "structure_event_id", "protection_kind",
                        "invalidation_reason", "strategy_id"))
_MONEY = ("profit", "commission", "swap", "fee")
_RECORD_KEYS = frozenset(("position_id", "deal_ids", "direction", *_MONEY,
                          "net_profit", "volume", "price_open", "price_close",
                          "reason_code", "broker_close_time", "close_utc",
                          "observed_utc", "entry_context"))


class RecentTradesError(ValueError):
    """Invalid or conflicting optional facts; callers may degrade this context."""


def _number(value, name, *, positive=False):
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value) and (not positive or value > 0))
    except OverflowError:
        valid = False
    if not valid:
        raise RecentTradesError("invalid " + name)
    return value


def _integer(value, name, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, int)
            or not (1 if positive else 0) <= value <= 2**64 - 1):
        raise RecentTradesError("invalid " + name)
    return value


def _epoch(value, name):
    return _number(value, name, positive=True)


def _namespace(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RecentTradesError("invalid namespace")
    return value


def namespace(account_identity, server, symbol, magic):
    """Return a stable opaque partition key, never retain raw account identity."""
    if isinstance(account_identity, bool) or not isinstance(account_identity, (str, int)):
        raise RecentTradesError("invalid account identity")
    account = str(account_identity)
    if not account.strip() or (isinstance(account_identity, int) and account_identity <= 0):
        raise RecentTradesError("invalid account identity")
    if any(not isinstance(value, str) or not value.strip() for value in (server, symbol)):
        raise RecentTradesError("invalid server or symbol")
    _integer(magic, "magic")
    payload = json.dumps([account, server, symbol, magic], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_state(now):
    """Start a new observation period; reload this state to preserve coverage."""
    return {"version": VERSION, "coverage_started_utc": _epoch(now, "now"), "partitions": {}}


def unavailable_context(reason="cache_unavailable"):
    """The host uses this after a soft cache read/write or conversion failure."""
    allowed = {"cache_unavailable", "cache_missing", "cache_corrupt", "cache_read_failed",
               "cache_write_failed", "cache_from_future", "namespace_unavailable"}
    return {"version": VERSION, "available": False,
            "coverage": {"source": "complete_broker_settlements", "scope": "observed_only",
                         "status": reason if isinstance(reason, str) and reason in allowed else "cache_unavailable"},
            "records": []}


def _entry_context(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - ENTRY_KEYS:
        raise RecentTradesError("invalid public entry context")
    for key, item in value.items():
        if not isinstance(item, str) or not item.strip() or len(item) > 512:
            raise RecentTradesError("invalid entry context " + key)
    return deepcopy(value)


def _validate_record(record):
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        raise RecentTradesError("invalid record schema")
    _integer(record["position_id"], "position_id", positive=True)
    ids = record["deal_ids"]
    if not isinstance(ids, list) or len(ids) < 2 or len(ids) > 2048:
        raise RecentTradesError("invalid complete deal IDs")
    for item in ids:
        _integer(item, "deal_id", positive=True)
    if ids != sorted(set(ids)):
        raise RecentTradesError("duplicate or unordered deal IDs")
    if record["direction"] not in ("BUY", "SELL"):
        raise RecentTradesError("invalid direction")
    for field in (*_MONEY, "net_profit"):
        _number(record[field], field)
    try:
        total = math.fsum(record[field] for field in _MONEY)
    except OverflowError as error:
        raise RecentTradesError("nonfinite net amount") from error
    if not math.isfinite(total) or not math.isclose(total, record["net_profit"], rel_tol=1e-12, abs_tol=1e-8):
        raise RecentTradesError("inconsistent net amount")
    for field in ("volume", "price_open", "price_close"):
        _number(record[field], field, positive=True)
    _integer(record["reason_code"], "reason_code")
    _integer(record["broker_close_time"], "broker_close_time")
    observed = _epoch(record["observed_utc"], "observed_utc")
    closed = record["close_utc"]
    if closed is not None and _epoch(closed, "close_utc") > observed:
        raise RecentTradesError("close UTC is after observation")
    _entry_context(record["entry_context"])


def _validated_copy(state):
    if (not isinstance(state, dict) or set(state) != {"version", "coverage_started_utc", "partitions"}
            or state["version"] != VERSION):
        raise RecentTradesError("invalid cache schema")
    started = _epoch(state["coverage_started_utc"], "coverage_started_utc")
    partitions = state["partitions"]
    if not isinstance(partitions, dict) or len(partitions) > MAX_PARTITIONS:
        raise RecentTradesError("invalid partitions")
    for key, part in partitions.items():
        _namespace(key)
        if not isinstance(part, dict) or set(part) != {"records", "truncated_before_utc"}:
            raise RecentTradesError("invalid partition schema")
        cutoff = part["truncated_before_utc"]
        if cutoff is not None:
            _epoch(cutoff, "truncated_before_utc")
        records = part["records"]
        if not isinstance(records, list) or len(records) > MAX_RECORDS:
            raise RecentTradesError("invalid record limit")
        positions, deals = set(), set()
        for record in records:
            _validate_record(record)
            if record["observed_utc"] < started:
                raise RecentTradesError("observation precedes cache inception")
            if record["position_id"] in positions or deals.intersection(record["deal_ids"]):
                raise RecentTradesError("conflicting settlement identities")
            positions.add(record["position_id"])
            deals.update(record["deal_ids"])
    return deepcopy(state)


def _sort_key(record):
    stamp = record["close_utc"] if record["close_utc"] is not None else record["observed_utc"]
    return stamp, record["position_id"], tuple(record["deal_ids"])


def record_settlement(state, namespace_id, settlement, *, direction, observed_utc,
                      close_utc, entry_context=None):
    """Return a bounded copy after a complete settlement, or raise without mutation.

    ``close_utc`` is host-confirmed UTC or None, never substituted with observation
    time. A later duplicate may enrich missing UTC/context, but cannot change facts.
    Successful repeated observations preserve the first observation timestamp.
    """
    result = _validated_copy(state)
    _namespace(namespace_id)
    if not isinstance(settlement, PositionSettlement):
        raise RecentTradesError("complete PositionSettlement required")
    if not isinstance(direction, str) or direction.upper() not in ("BUY", "SELL"):
        raise RecentTradesError("invalid direction")
    try:
        ids = list(settlement.deal_ids)
        for item in ids:
            _integer(item, "deal_id", positive=True)
        if len(ids) != len(set(ids)):
            raise RecentTradesError("duplicate deal IDs")
        record = {"position_id": settlement.position_id, "deal_ids": sorted(ids),
                  "direction": direction.upper(),
                  **{field: getattr(settlement, field) for field in _MONEY},
                  "net_profit": settlement.net_profit, "volume": settlement.volume,
                  "price_open": settlement.price_open, "price_close": settlement.price_close,
                  "reason_code": settlement.reason, "broker_close_time": settlement.time,
                  "observed_utc": observed_utc, "close_utc": close_utc,
                  "entry_context": _entry_context(entry_context)}
    except (TypeError, OverflowError) as error:
        raise RecentTradesError("malformed settlement") from error
    _validate_record(record)
    if record["observed_utc"] < result["coverage_started_utc"]:
        raise RecentTradesError("observation precedes cache inception")
    partitions = result["partitions"]
    if namespace_id not in partitions and len(partitions) >= MAX_PARTITIONS:
        raise RecentTradesError("partition limit reached")
    part = partitions.setdefault(namespace_id, {"records": [], "truncated_before_utc": None})
    for index, existing in enumerate(part["records"]):
        if existing["position_id"] != record["position_id"]:
            if set(existing["deal_ids"]).intersection(record["deal_ids"]):
                raise RecentTradesError("deal belongs to another position")
            continue
        immutable = _RECORD_KEYS - {"observed_utc", "close_utc", "entry_context"}
        if any(existing[key] != record[key] for key in immutable):
            raise RecentTradesError("conflicting settled position")
        for field in ("close_utc", "entry_context"):
            if existing[field] is not None and record[field] is not None and existing[field] != record[field]:
                raise RecentTradesError("conflicting settlement " + field)
        # New trustworthy timing must not make a fact visible before it was known.
        enriched = any(existing[field] is None and record[field] is not None
                       for field in ("close_utc", "entry_context"))
        merged = dict(existing)
        if enriched:
            merged["observed_utc"] = max(existing["observed_utc"], observed_utc)
            for field in ("close_utc", "entry_context"):
                if merged[field] is None:
                    merged[field] = record[field]
        part["records"][index] = merged
        break
    else:
        part["records"].append(record)
    part["records"].sort(key=_sort_key)
    removed = part["records"][:-MAX_RECORDS]
    part["records"] = part["records"][-MAX_RECORDS:]
    if removed:
        part["truncated_before_utc"] = max([_sort_key(row)[0] for row in removed]
                                           + [part["truncated_before_utc"] or 0])
    return result


def recent_context(state, namespace_id, now):
    """Last six confirmed closes in six hours, including both sides and all P/L.

    Invalid caches are explicitly unavailable, never a fabricated empty history.
    Public output contains neither account identity nor raw partition keys.
    """
    try:
        state = _validated_copy(state)
        _namespace(namespace_id)
        _epoch(now, "now")
    except (ValueError, TypeError, OverflowError):
        return unavailable_context("cache_corrupt")
    if state["coverage_started_utc"] > now:
        return unavailable_context("cache_from_future")
    part = state["partitions"].get(namespace_id, {"records": [], "truncated_before_utc": None})
    known = [row for row in part["records"] if row["observed_utc"] <= now]
    eligible = [row for row in known if row["close_utc"] is not None
                and now - WINDOW_SECONDS <= row["close_utc"] <= now]
    eligible.sort(key=_sort_key, reverse=True)
    unknown = sum(row["close_utc"] is None for row in known)
    status = "partial" if unknown or part["truncated_before_utc"] is not None else "observed_only"
    return {"version": VERSION, "available": True,
            "coverage": {"source": "complete_broker_settlements", "scope": "observed_only",
                         "status": status, "started_utc": state["coverage_started_utc"],
                         "window_start_utc": now - WINDOW_SECONDS, "window_end_utc": now,
                         "max_records": MAX_RECORDS, "returned_limit": CONTEXT_RECORDS,
                         "cached_records": len(known), "unknown_close_utc_records": unknown,
                         "truncated_before_utc": part["truncated_before_utc"],
                         "eligible_records": len(eligible)},
            "records": deepcopy(eligible[:CONTEXT_RECORDS])}
