"""Pre-registered loss-side SL control; no production strategy changes or paid AI.

Run only with the existing cached bars/entries. This is a mechanical hypothesis,
not an AI-3 replay or a single-position portfolio. See the dated protocol.
"""
import argparse
import bisect
import copy
import csv
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from functools import lru_cache
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch


VERSION = "weakening-risk-proxy-v1"


@dataclass(frozen=True)
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float


def rejection(bar, side, atr):
    body = abs(bar.close - bar.open)
    span = max(bar.high - bar.low, 1e-9)
    wick = (min(bar.open, bar.close) - bar.low if side == "buy"
            else bar.high - max(bar.open, bar.close))
    placement = ((bar.close - bar.low) / span if side == "buy"
                 else (bar.high - bar.close) / span)
    return wick >= max(1.6 * body, .18 * atr) and placement >= .55


def weakening(side, opened, m1, m5, atr1, now):
    """Closed, contiguous, post-entry evidence only; no account/P&L fitting."""
    if side not in ("buy", "sell") or len(m1) != 3 or len(m5) != 2 or atr1 <= 0:
        return False
    if any(b.time < opened for b in m1):
        return False
    if any(m1[i + 1].time - m1[i].time != 60 for i in range(2)):
        return False
    if m1[-1].time + 60 != now or m5[1].time - m5[0].time != 300:
        return False
    if not opened < m5[-1].time + 300 <= now < m5[-1].time + 600:
        return False
    a, b, c = m1
    p, q = m5
    if rejection(b, side, atr1) or rejection(c, side, atr1):
        return False
    if side == "buy":
        return (q.close < q.open and q.close < p.low and q.high < p.high
                and b.close < b.open and c.close < c.open
                and c.close < b.close < a.close and c.high < b.high and c.low < b.low)
    return (q.close > q.open and q.close > p.high and q.low > p.low
            and b.close > b.open and c.close > c.open
            and c.close > b.close > a.close and c.high > b.high and c.low > b.low)


def stop_proposal(side, m1, price, atr, room, spread, tick):
    if min(price, atr, room, tick) <= 0 or spread < 0:
        raise ValueError("invalid SL model input")
    if side == "buy":
        anchor = min(b.low for b in m1[-2:]) - .15 * atr
        raw, rounding = min(anchor, price - room), ROUND_FLOOR
    elif side == "sell":
        anchor = max(b.high for b in m1[-2:]) + spread + .15 * atr
        raw, rounding = max(anchor, price + room), ROUND_CEILING
    else:
        raise ValueError("invalid side")
    grid = Decimal(str(tick))
    return float((Decimal(str(raw)) / grid).to_integral_value(rounding=rounding) * grid)


def accept_stop(side, old, proposed, price, room, step):
    sign = 1 if side == "buy" else -1
    return (proposed > 0 and sign * (proposed - old) + 1e-8 >= step
            and sign * (price - proposed) + 1e-8 >= room)


def integrity(root):
    files = [root / ".env", root / "server_deploy/.env"]
    for area in (root / "xau_fib_pa_logs", root / "server_deploy/xau_fib_pa_logs"):
        if area.is_dir():
            files += list(area.rglob("*.log")) + list(area.rglob("*.jsonl"))
            files += list(area.glob("*.json"))
    files += [root / name for name in ("xauusd_fib_volume_pa_trader.py",
              "xauusd_fib_volume_pa_ai3.py", "xauusd_fib_pa_backtest.py")]
    files += list((root / "strategies").rglob("*.py"))
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files if p.is_file()}


@contextmanager
def offline(root):
    """Prevent terminal/network side effects and isolate imports' default paths."""
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="gold-weakening-") as folder, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "FIB_PA_GUARDIAN_STATE_FILE": str(Path(folder) / "guard.json"),
            "FIB_PA_STRATEGY_STATE_FILE": str(Path(folder) / "events.json"),
            "FIB_PA_AI3_STATE_FILE": str(Path(folder) / "entry.json"),
            "FIB_PA_NEWS_CALENDAR_CACHE": str(Path(folder) / "news.json"),
            "FIB_PA_NEWS_PAUSE_ENABLED": "false"}))
        def forbidden(*args, **kwargs):
            raise AssertionError("terminal/network access forbidden in historical control")
        import MetaTrader5 as mt5
        import requests
        for name in ("initialize", "login", "order_send"):
            stack.enter_context(patch.object(mt5, name, side_effect=forbidden))
        for name in ("shutdown", "account_info", "positions_get", "symbol_info",
                     "symbol_info_tick", "copy_rates_from_pos", "history_deals_get", "order_calc_profit"):
            stack.enter_context(patch.object(mt5, name, return_value=None))
        stack.enter_context(patch.object(requests.sessions.Session, "request", side_effect=forbidden))
        stack.enter_context(patch.object(socket.socket, "connect", side_effect=forbidden))
        stack.enter_context(patch.object(socket.socket, "connect_ex", side_effect=forbidden))
        log_open = logging.FileHandler._open
        def isolated_log(handler):
            path = Path(handler.baseFilename).resolve()
            return io.StringIO() if path.is_relative_to(root / "xau_fib_pa_logs") else log_open(handler)
        stack.enter_context(patch.object(logging.FileHandler, "_open", isolated_log))
        sys.path.insert(0, str(root))
        import xauusd_fib_pa_backtest as replay
        yield replay


def metrics(rows, key):
    vals = [r[key]["pnl"] for r in rows]
    positive = sum(x for x in vals if x > 0)
    negative = -sum(x for x in vals if x < 0)
    wins, losses = sum(x > 0 for x in vals), sum(x < 0 for x in vals)
    return dict(n=len(vals), wins=wins, losses=losses,
                win_rate=round(100 * wins / len(vals), 3) if vals else None,
                net=round(sum(vals), 2), positive=round(positive, 2), negative=round(negative, 2),
                profit_factor=round(positive / negative, 4) if negative else None,
                mean_win=round(positive / wins, 4) if wins else None,
                mean_loss=round(-negative / losses, 4) if losses else None)


def comparison(rows, key):
    ds = [r[key]["pnl"] - r["baseline"]["pnl"] for r in rows]
    return dict(metrics=metrics(rows, key), delta=round(sum(ds), 2),
                improved=sum(x > .005 for x in ds), worsened=sum(x < -.005 for x in ds),
                unchanged=sum(abs(x) <= .005 for x in ds),
                accepted=sum(len(r[key]["accepted"]) for r in rows),
                sign_lost=sum(r["baseline"]["pnl"] > 0 and r[key]["pnl"] <= 0 for r in rows),
                sign_saved=sum(r["baseline"]["pnl"] < 0 and r[key]["pnl"] >= 0 for r in rows),
                worst=sorted([dict(open_bj=r["open_bj"], side=r["side"],
                              baseline=r["baseline"]["pnl"], candidate=r[key]["pnl"],
                              delta=round(r[key]["pnl"] - r["baseline"]["pnl"], 2))
                              for r in rows], key=lambda x: x["delta"])[:8])


def run_cached(replay, entries_path, count):
    base = replay.base
    if abs(base.LOT_SIZE - .01) > 1e-10 or abs(base.MAX_SL_ABS - 8) > 1e-10:
        raise ValueError("requires unchanged .01 lot / SL8 research configuration")
    data, meta = replay.load_history()
    meta["guardian_fee_cash"] = .10
    entries = list(csv.DictReader(entries_path.open(encoding="utf-8-sig", newline="")))
    if count:
        entries = entries[-count:]
    engines = {name: replay.Backtest(data, meta, 55, guardian_policy="profit_floor_v3")
               for name in ("baseline", "wait_0", "wait_1", "wait_2")}
    old = engines["baseline"]
    m5_window = old.windows["M5"]
    @lru_cache(maxsize=1024)
    def context_end(end):
        frame = m5_window.df.iloc[max(0, end - 120):end].reset_index(drop=True)
        atr = base.calculate_atr(frame) if len(frame) >= 20 else 0.
        return frame, atr
    def context(ts):
        return context_end(bisect.bisect_right(m5_window.close_times, ts))
    for engine in engines.values():
        engine.guardian_context = context
    all_m1 = [Bar(*map(float, r)) for r in data["M1"][["time", "open", "high", "low", "close"]].itertuples(index=False, name=None)]
    all_m5 = [Bar(*map(float, r)) for r in data["M5"][["time", "open", "high", "low", "close"]].itertuples(index=False, name=None)]
    trs = [max(b.high - b.low, abs(b.high - all_m1[i-1].close), abs(b.low - all_m1[i-1].close))
           if i else b.high-b.low for i, b in enumerate(all_m1)]
    atr1 = [sum(trs[max(0,i-13):i+1])/min(i+1,14) for i in range(len(trs))]
    pairs = []
    started = time.monotonic()
    for entry_number, entry in enumerate(entries):
        ts = int(entry["fill_server_ts"])
        i = bisect.bisect_left(old.t, ts)
        if i == len(old.t) or old.t[i] != ts:
            raise ValueError("entry minute missing")
        side = entry["side"].lower()
        proposal = base.StrategyResult(side, int(entry["score"]), False, "fixed research entry",
            float(entry["sl"]), float(entry["tp"]), 0, 0,
            dict(session=entry["session"], setup=entry["strategy_id"], strategy_contract=dict(
                id=entry["strategy_id"], max_hold_minutes=90, cooldown_seconds=60)))
        result = dict(open_bj=entry["open_bj"], side=side, fill_server_ts=ts,
                      entry=float(entry["entry"]), sl=float(entry["sl"]), tp=float(entry["tp"]))
        for name, engine in engines.items():
            engine.open_position(proposal, i, ts)
            if engine.position is None or abs(engine.position["state"].entry_price-result["entry"]) > .001:
                raise ValueError("fixed entry changed or rejected")
            pending, accepted, rejected, signals = None, [], 0, 0
            delay = None if name == "baseline" else int(name[-1])
            sign = 1 if side == "buy" else -1
            for j in range(i, len(engine.t)):
                position = engine.position
                state = position["state"]
                guard = state.profit_floor
                before = position["sl"]
                # Pending reply is applied at a later minute's open, never to
                # the same minute that supplied its high/low/close evidence.
                if pending and j >= pending["index"]:
                    _, atr = context(engine.t[j])
                    live = engine.o[j] + (engine.spreads[j] if side == "sell" else 0.)
                    room = guard.room(atr, engine.spreads[j], engine.point)
                    step = max(guard.rules["advance_abs"], guard.rules["advance_atr"] * max(atr,guard.atr_floor))
                    if accept_stop(side, position["sl"], pending["stop"], live, room, step):
                        position["sl"] = pending["stop"]
                        guard.acknowledge(state.direction, pending["stop"])
                        accepted.append(dict(signal_ts=pending["signal_ts"], apply_ts=engine.t[j],
                                             stop=pending["stop"], price=live, minimum_room=room,
                                             m5_end=pending["m5_end"]))
                    else:
                        rejected += 1
                    pending = None
                if sign * (position["sl"] - before) < -1e-8:
                    raise AssertionError("SL moved backward")
                if engine.check_exit(j):
                    break
                engine.update_guardian(j)
                if sign * (position["sl"] - before) < -1e-8:
                    raise AssertionError("local SL moved backward after candidate")
                if delay is None or pending is not None or j < 2:
                    continue
                now = engine.t[j] + 60
                price = engine.c[j] + (engine.spreads[j] if side == "sell" else 0.)
                if sign * (price - state.entry_price) * engine.lot_value - engine.guard_fee > 0:
                    continue
                m5_end = bisect.bisect_right(m5_window.close_times, now)
                bars1, bars5 = all_m1[j-2:j+1], all_m5[m5_end-2:m5_end]
                if not weakening(side, ts, bars1, bars5, atr1[j], now):
                    continue
                _, atr = context(now)
                if atr <= 0:
                    continue
                room = guard.room(atr, engine.spreads[j], engine.point)
                stop = stop_proposal(side, bars1, price, atr, room, engine.spreads[j], engine.direction_tick_size)
                step = max(guard.rules["advance_abs"], guard.rules["advance_atr"] * max(atr,guard.atr_floor))
                if accept_stop(side, position["sl"], stop, price, room, step):
                    signals += 1
                    pending = dict(stop=stop, index=j+1+delay, signal_ts=now,
                                   m5_end=int(bars5[-1].time+300))
            if engine.position is not None:
                j = len(engine.t)-1
                price = engine.c[j]+(engine.spreads[j] if side == "sell" else 0.)
                engine.close(engine.t[j]+60, price, "缓存结束")
            trade = engine.trades[-1]
            result[name] = {k: trade[k] for k in ("close_bj", "exit", "exit_reason", "pnl", "hold_min")}
            result[name].update(accepted=accepted, rejected=rejected, signals=signals)
        pairs.append(result)
        if (entry_number+1) % 100 == 0 or entry_number == len(entries)-1:
            print(json.dumps(dict(progress=entry_number+1,total=len(entries),elapsed=round(time.monotonic()-started,1))), flush=True)
    return dict(pairs=pairs, source_meta=meta, policy=copy.deepcopy(old.guardian_policy))


def main():
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[2]
    parser.add_argument("--entries", default=str(root/"xau_fib_pa_logs/backtest/trades_legacy_base_20261010_121738_891629_e76a49b4.csv"))
    parser.add_argument("--limit", type=int, default=0, help="0 uses all fixed entries")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("limit cannot be negative")
    before = integrity(root)
    path = Path(args.entries).resolve()
    protocol = Path(__file__).with_name("2026-10-16-weakening-risk-protocol.md")
    inputs = [path,protocol,Path(__file__)] + sorted((root/"xau_fib_pa_logs/backtest").glob("rates_*"))
    hashes = {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    with offline(root) as replay:
        result = run_cached(replay, path, args.limit)
    unchanged = before == integrity(root)
    if not unchanged:
        raise AssertionError("existing production/configuration/log/state changed")
    pairs = result["pairs"]
    groups = {"all": pairs}
    for side in ("buy", "sell"):
        groups[side] = [r for r in pairs if r["side"] == side]
    for month in sorted({r["open_bj"][:7] for r in pairs}):
        groups[month] = [r for r in pairs if r["open_bj"].startswith(month)]
    summary = {group: dict(baseline=metrics(rows,"baseline"), **{
        name:comparison(rows,name) for name in ("wait_0","wait_1","wait_2")})
        for group, rows in groups.items()}
    result.update(version=VERSION, inputs_sha256=hashes, summary=summary,
                  source_integrity_unchanged=unchanged, integrity_file_count=len(before),
                  method="independent fixed entries, closed-M1/M5 mechanical proxy, no AI, next-bar effect; extra wait 0/1/2 M1 bars",
                  limitations="reused local-entry cache; not current adaptive AI outcomes, not portfolio P&L, no tick/live broker/slippage; common historical 90min cap")
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    flat = output.with_suffix(".csv")
    names = ["open_bj","side","entry","sl","tp","baseline","wait_0","wait_1","wait_2"]
    with flat.open("w",encoding="utf-8",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=names)
        writer.writeheader()
        for r in pairs:
            writer.writerow({k:r[k]["pnl"] if k in ("baseline","wait_0","wait_1","wait_2") else r[k] for k in names})
    print(json.dumps(dict(version=VERSION,range=[pairs[0]["open_bj"],pairs[-1]["open_bj"]],
                          all=summary["all"],unchanged=unchanged,output=str(output)),ensure_ascii=False),flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
