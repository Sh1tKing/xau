"""Read saved AI1 snapshots only: direction/protection diagnostics, never AI profit replay.

No trader import, environment, broker, network, calendar fetch, or trading state.
The tick grid is an explicit diagnostic assumption; live code uses broker metadata.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from strategies.direction import DEFAULT_POLICY, ENGINE_DIGEST, evaluate_context, public_context


def rows_from_snapshots(paths, tick_size):
    rows, executions, hashes = [], [], {}
    for path in paths:
        hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                item = json.loads(line)
                if item.get("stage") == "execution_result" and item["payload"].get("placed"):
                    executions.append((item["timestamp"], item["decision_id"]))
                if item.get("stage") != "ai1_input":
                    continue
                pack = item["payload"].get("market_evidence", item["payload"])
                quote = pack["live_quote"]
                evidence = pack.get("strategy_evidence", {})
                offsets = [v.get("evidence", {}).get("server_offset_hours") for v in evidence.values()]
                offset = next((x for x in offsets if isinstance(x, (float, int))), None)
                if offset is None:
                    raise ValueError("snapshot has no original server UTC offset: " + item["decision_id"])
                context = evaluate_context(pack["m15_bars"], pack["m5_bars"], pack["h1_trend"], DEFAULT_POLICY,
                    now=quote["observed_at"], server_utc_offset=offset, tick_size=tick_size, bid=quote["bid"])
                for side in ("buy", "sell"):
                    permission = context["permissions"][side]
                    entry = quote["ask" if side == "buy" else "bid"]
                    atr = pack["atr"]
                    buffer = min(max(.6, .15 * atr), 1.)
                    minimum = pack["rules"]["price_finalization"]["generation_min_risk"]
                    anchor = permission.get("anchor")
                    extreme = (min if side == "buy" else max)(b["low" if side == "buy" else "high"] for b in pack["m5_bars"][-3:])
                    protection = extreme if not anchor else (min if side == "buy" else max)(extreme, anchor["price"])
                    boundary = min(protection-buffer, entry-minimum) if side == "buy" else max(protection+buffer, entry+minimum)
                    rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
                    stop = float(Decimal(str(boundary)).quantize(Decimal(".01"), rounding=rounding))
                    risk = entry-stop if side == "buy" else stop-entry
                    trigger = quote["bid" if side == "buy" else "ask"]
                    clearance = trigger-stop if side == "buy" else stop-trigger
                    chase = abs(entry-pack["m15_ema21"])/atr
                    reasons = []
                    if not permission["allowed"]:
                        reasons.append(permission["reason"])
                    if not 0 < risk <= 8.001:
                        reasons.append("canonical protection exceeds SL8")
                    if clearance < 2*buffer:
                        reasons.append("trigger-side clearance below 2buffer")
                    if chase > 1.5:
                        reasons.append("EMA21 chase exceeds 1.5ATR")
                    rows.append({"timestamp": item["timestamp"], "decision_id": item["decision_id"],
                        "source_file": path.name, "side": side, "h1_background": pack["h1_trend"],
                        "m15_state": context["m15_leg"]["state"], "route": permission["route"],
                        "direction_allowed": permission["allowed"], "direction_reason": permission["reason"],
                        "structure_event_id": permission.get("structure_event_id"),
                        "protection_kind": anchor["kind"] if anchor else "recent3_m5",
                        "protection_price": protection, "entry": entry, "minimum_generated_stop": stop,
                        "risk": round(risk, 6), "ema_distance_atr": round(chase, 6),
                        "protection_and_quote_feasible": not reasons, "blockers": " | ".join(reasons),
                        "public_context_chars": len(json.dumps(public_context(context), ensure_ascii=False))})
    return rows, executions, hashes


def diagnose(paths, trades_path, tick_size, output):
    rows, executions, hashes = rows_from_snapshots(paths, tick_size)
    summaries = {}
    for day in sorted({r["timestamp"][:10] for r in rows}):
        dayrows = [r for r in rows if r["timestamp"].startswith(day)]
        summaries[day] = {"snapshots": len(dayrows)//2,
            "h1_backgrounds": dict(Counter(r["h1_background"] for r in dayrows if r["side"] == "buy")),
            "allowed_routes": dict(Counter(r["side"]+"/"+r["route"] for r in dayrows if r["direction_allowed"])),
            "feasible_routes": dict(Counter(r["side"]+"/"+r["route"] for r in dayrows if r["protection_and_quote_feasible"])),
            "unknown_snapshots": sum(r["m15_state"] == "unknown" for r in dayrows)//2}
    matched = []
    with trades_path.open(encoding="utf-8-sig", newline="") as handle:
        for trade in csv.DictReader(handle):
            if not trade["opened_at"].startswith("2026-10-15"):
                continue
            opened = datetime.fromisoformat(trade["opened_at"])
            candidates = [(abs((datetime.fromisoformat(t)-opened).total_seconds()), decision) for t, decision in executions]
            distance, decision = min(candidates, default=(float("inf"), None))
            if distance > 15:
                raise ValueError("no precise execution decision for ticket " + trade["ticket"])
            ownrows = [r for r in rows if r["decision_id"] == decision]
            # Retried requests are distinct observations. Match the last saved
            # successful analysis input, never a later market snapshot.
            ownrows = ownrows[-2:]
            matched.append({"ticket": trade["ticket"], "opened_at": trade["opened_at"],
                "actual_direction": trade["direction"], "actual_profit": trade["profit"],
                "decision_id": decision, "execution_match_seconds": distance,
                "new_mechanism": ownrows})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    summary = {"policy": DEFAULT_POLICY, "engine_digest": ENGINE_DIGEST, "source_sha256": hashes,
        "tick_size_assumption": tick_size, "days": summaries, "all_oct15_trades": matched,
        "limitations": ["saved request snapshots only; not all market checkpoints",
                        "historical prompt OHLC can be rounded; .01 grid is an explicit assumption",
                        "feasible means direction/canonical protection/SL8/EMA/clearance only",
                        "does not recreate AI1 trigger/target/quality, AI2, fees, latency, or new profit",
                        "existing eight trade outcomes are facts, not counterfactual BUY/SELL profits"]}
    output.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"days": summaries, "matched_trades": len(matched), "rows": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, nargs="+", required=True)
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--tick-size", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    diagnose(args.logs, args.trades, args.tick_size, args.output)
