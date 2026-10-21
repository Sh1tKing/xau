"""Offline location sensitivity of linked ORIGINAL trades; no strategy returns.

Uses the last saved AI1 POST facts, original execution and a unique AI3
decision-to-ticket link plus complete broker settlement. No env/MT5/AI/HTTP.
Missing links or obstacle facts are disclosed, never matched by similar prices.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def location_facts(pack, side, entry, stop):
    """Symmetric distances to saved price references, not judgments of strength."""
    if side not in ("buy", "sell") or not all(finite(v) for v in (entry, stop)):
        raise ValueError("invalid side or prices")
    sign = 1 if side == "buy" else -1
    risk = sign*(entry-stop)
    if risk <= 0:
        raise ValueError("invalid original risk")
    computed = pack.get("computed_evidence") or {}
    raw = (computed.get("obstacles") or {}).get(side)
    if not isinstance(raw, list):
        raise ValueError("saved obstacle facts unavailable")
    observed = (pack.get("live_quote") or {}).get("observed_at")
    bars = pack.get("m15_bars", [])
    if not bars or not finite(observed):
        raise ValueError("saved closed bars or observation unavailable")
    closed_times = {"M15": {bar["time"] for bar in bars},
                    "M5": {bar["time"] for bar in pack.get("m5_bars", [])}}
    references = []
    for row in raw:
        if not isinstance(row, dict) or not finite(row.get("price")):
            continue
        time = row.get("bar_time")
        if not finite(time) or time not in closed_times.get(row.get("timeframe"), set()):
            raise ValueError("obstacle source is outside saved closed bars")
        # 'observed' breakthrough is explicitly different from an unbroken
        # reference. Unknown classifications are not promoted to strong levels.
        status = (row.get("closed_breakthrough") or {}).get("status")
        if status != "not_observed_in_window":
            continue
        distance = sign*(row["price"]-entry)
        if distance <= 0:
            continue
        origins = [row, *row.get("observed", [])]
        m15 = any(origin.get("timeframe") == "M15" and origin.get("source") in
                  ("recent_m15_extreme", "confirmed_m15_pivot") for origin in origins)
        references.append({"price": row["price"], "source": row.get("source"),
                           "bar_time": time, "timeframe": row.get("timeframe"),
                           "classification": row.get("classification"),
                           "distance_r": distance/risk, "m15_reference": m15})
    references.sort(key=lambda row: row["distance_r"])
    chosen = [row for row in references if row["m15_reference"]]
    m5 = pack.get("m5_bars", [])[-12:]
    location = None
    if len(m5) == 12:
        low, high = min(b["low"] for b in m5), max(b["high"] for b in m5)
        if high > low:
            location = (entry-low)/(high-low)
    return {"risk": risk, "nearest_any_r": references[0]["distance_r"] if references else None,
            "nearest_m15_r": chosen[0]["distance_r"] if chosen else None,
            "unbroken_references": references, "m5_12_location": location,
            "obstacle_coverage": "saved_references_only"}


def metrics(rows):
    nets = [row["settlement"]["net"] for row in rows]
    return {"n": len(rows), "wins": sum(n > 0 for n in nets), "losses": sum(n < 0 for n in nets),
            "flat": sum(n == 0 for n in nets), "original_net": round(math.fsum(nets), 2),
            "original_wins_amount": round(math.fsum(n for n in nets if n > 0), 2),
            "original_losses_amount": round(math.fsum(n for n in nets if n < 0), 2)}


def audit(folder, start, through):
    inputs, executions, tickets, settlements = {}, {}, {}, {}
    sources, skipped = [], Counter()
    for source in sorted((folder/"ai_decisions").glob("*.jsonl")):
        if not start <= source.stem <= through:
            continue
        cap = source.stat().st_size
        digest, hashed, last = hashlib.sha256(), 0, None
        with source.open("rb") as handle:
            for number, line in enumerate(handle, 1):
                if handle.tell() > cap:
                    skipped["partial_at_read_boundary"] += 1
                    break
                digest.update(line)
                hashed += len(line)
                try:
                    record = json.loads(line)
                except ValueError:
                    skipped["unreadable_jsonl"] += 1
                    continue
                stage, did = record.get("stage"), record.get("decision_id")
                payload, last = record.get("payload") or {}, record.get("timestamp")
                if stage == "ai1_input":
                    pack = payload.get("market_evidence") or {}
                    if pack.get("symbol") == "XAUUSD":
                        inputs[did] = {"source": source.name, "line": number, "timestamp": last,
                            "pack": {key: pack.get(key) for key in
                                     ("computed_evidence", "m5_bars", "m15_bars", "live_quote", "snapshot_id")}}
                elif stage == "execution_result" and payload.get("placed"):
                    executions[did] = {"timestamp": last, "proposal": payload.get("proposal") or {}}
                elif stage == "ai3_input":
                    for position in (payload.get("market_evidence") or {}).get("positions", []):
                        origin = (position.get("entry_context") or {}).get("decision_id")
                        if origin and position.get("ticket"):
                            tickets.setdefault(origin, set()).add(int(position["ticket"]))
        sources.append({"path": str(source), "bytes_limit": cap, "bytes_hashed": hashed,
                        "sha256": digest.hexdigest(), "last_timestamp": last})
    for source in sorted(folder.glob("Fib_PA_AI3_*.log")):
        date = source.stem.removeprefix("Fib_PA_AI3_")
        if not start <= date <= through:
            continue
        data = source.read_bytes()
        for number, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
            if "source=broker_history" not in line or not any(s in line for s in ("平仓结算 ticket=", "平仓记录 ticket=")):
                continue
            ticket = re.search(r"ticket=(\d+)", line)
            prices = re.search(r"price_open=([\d.]+) price_close=([\d.]+)", line)
            net = re.search(r"net_profit=([-\d.]+)", line)
            if ticket and prices and net:
                settlements[int(ticket[1])] = {"net": float(net[1]), "entry": float(prices[1]),
                    "exit": float(prices[2]), "source": source.name, "line": number}
        sources.append({"path": str(source), "bytes_limit": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    rows = []
    for did, execution in executions.items():
        if len(tickets.get(did, set())) != 1:
            skipped["execution_ticket_unlinked"] += 1
            continue
        ticket = next(iter(tickets[did]))
        if ticket not in settlements or did not in inputs:
            skipped["missing_settlement_or_input"] += 1
            continue
        inp, settled, proposal = inputs[did], settlements[ticket], execution["proposal"]
        row = {"decision_id": did, "ticket": ticket, "entry_time": execution["timestamp"],
               "side": proposal.get("action"), "settlement": settled,
               "input": {k: v for k, v in inp.items() if k != "pack"}, "stop": proposal.get("stop_loss")}
        try:
            row["facts"] = location_facts(inp["pack"], row["side"], settled["entry"], row["stop"])
        except (ValueError, TypeError, KeyError) as error:
            row["unavailable"] = str(error)
        rows.append(row)
    rows.sort(key=lambda row: row["entry_time"])
    groups = {}
    for label, group in (("all", rows), ("after_chop", [r for r in rows if r["entry_time"] >= "2026-10-21T04:46:34"]),
                         ("beijing_01_07", [r for r in rows if 1 <= datetime.fromisoformat(r["entry_time"]).hour < 7])):
        variations = {}
        for field in ("nearest_any_r", "nearest_m15_r"):
            for threshold in (.5, .75, 1., 1.5):
                blocked = [r for r in group if r.get("facts", {}).get(field) is not None and r["facts"][field] < threshold]
                variations[f"{field}<{threshold:g}"] = {"flagged": metrics(blocked),
                    "tickets": [r["ticket"] for r in blocked],
                    "unavailable": sum("facts" not in r for r in group),
                    "no_saved_ahead_reference": sum(r.get("facts", {}).get(field) is None and "facts" in r for r in group)}
        groups[label] = {"all_original": metrics(group), "sensitivity": variations}
    return {"interpretation": "Original trade selection sensitivity, not backtest returns or an estimated new AI strategy.",
            "method": "Original filled entry/original submitted SL; saved AI1 unbroken references; no obstacle strength inferred.",
            "range": [start, through], "sources": sources, "skipped": dict(skipped), "summary": groups, "trades": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=ROOT/"server_deploy/xau_fib_pa_logs")
    parser.add_argument("--start", default="2026-10-12")
    parser.add_argument("--through", default="2026-10-21")
    parser.add_argument("--output", type=Path, default=ROOT/".superpowers/sdd/settlement-reliability-20261022/location-audit.json")
    args = parser.parse_args()
    result = audit(args.log_dir, args.start, args.through)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"summary": result["summary"], "skipped": result["skipped"]}, ensure_ascii=False))
