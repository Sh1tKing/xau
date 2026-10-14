"""Fixed-entry M1 exit control. No strategy activation, terminal or paid AI.

The latest chronological N entries from an existing local replay are evaluated
independently. Different exits may overlap, so these are not portfolio returns.
"""
import argparse
import bisect
import csv
from datetime import datetime
import hashlib
import io
import json
import logging
from pathlib import Path
import socket
import sys
from unittest.mock import patch


def main():
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(root))
    parser = argparse.ArgumentParser()
    parser.add_argument("--entries",required=True)
    parser.add_argument("--limit",type=int,default=100)
    parser.add_argument("--guardian-policy",choices=("profit_floor_v1","profit_floor_v2"),default="profit_floor_v1")
    parser.add_argument("--baseline-policy",choices=("legacy","profit_floor_v1"),default="legacy")
    parser.add_argument("--output",help="Optional result path; omit to use the historical log directory")
    args = parser.parse_args()
    if args.limit < 1:parser.error("limit must be positive")
    logging.disable(logging.CRITICAL)
    original_open=logging.FileHandler._open
    def isolated_open(handler):
        target=Path(handler.baseFilename).resolve()
        return io.StringIO() if target.is_relative_to(root/"xau_fib_pa_logs") else original_open(handler)
    with patch.object(logging.FileHandler,"_open",isolated_open):
        import xauusd_fib_pa_backtest as replay
    base = replay.base
    def forbidden(*a,**kw):
        raise AssertionError("terminal and HTTP are forbidden in cached control")
    import requests
    with patch.object(base.mt5,"initialize",side_effect=forbidden), \
         patch.object(base.mt5,"order_send",side_effect=forbidden), \
         patch.object(requests,"post",side_effect=forbidden), \
         patch.object(requests.sessions.Session,"request",side_effect=forbidden), \
         patch.object(socket.socket,"connect",side_effect=forbidden), \
         patch.object(socket.socket,"connect_ex",side_effect=forbidden):
        data,meta = replay.load_history()
        entries_path = Path(args.entries).resolve()
        with entries_path.open(encoding="utf-8-sig",newline="") as handle:
            entries = list(csv.DictReader(handle))[-args.limit:]
        # Equal cash charge in both controls, common original 90-minute deadline.
        rules = base.frozen_guardian_policy() or base.profit_policy()
        meta["guardian_fee_cash"] = rules["fee_reserve"]
        old = replay.Backtest(data,meta,55,guardian_policy=args.baseline_policy)
        new = replay.Backtest(data,meta,55,guardian_policy=args.guardian_policy)
        rules = new.guardian_policy
        m5_closes = set(old.windows["M5"].close_times)
        if abs(base.LOT_SIZE-.01) > 1e-10 or abs(base.MAX_SL_ABS-8.) > 1e-10:
            raise ValueError("control requires existing .01 lot / SL8 configuration")
        pairs = []
        for entry in entries:
            ts = int(entry["fill_server_ts"])
            i = bisect.bisect_left(old.t,ts)
            if i == len(old.t) or old.t[i] != ts:
                raise ValueError("frozen entry minute absent from cache")
            side = entry["side"].lower()
            proposal = base.StrategyResult(side,int(entry["score"]),False,"fixed cached control",
                float(entry["sl"]),float(entry["tp"]),0,0,
                dict(session=entry["session"],setup=entry["strategy_id"],
                     strategy_contract=dict(id=entry["strategy_id"],max_hold_minutes=90,cooldown_seconds=60)))
            result = dict(fill_server_ts=ts,entry=float(entry["entry"]),sl=proposal.stop_loss,
                          tp=proposal.take_profit,side=side)
            for name,engine in (("baseline",old),("candidate",new)):
                engine.open_position(proposal,i,ts)
                if engine.position is None or abs(engine.position["state"].entry_price-result["entry"]) > .001:
                    raise ValueError("original entry is no longer executable")
                protected = engine.position["sl"]
                for j in range(i,len(engine.t)):
                    forced = engine.position.pop("exit_next",None)
                    if forced and not engine.time_exit_due(j):
                        price = engine.o[j]+(engine.spreads[j] if side == "sell" else 0.)
                        engine.close(engine.t[j],price,forced)
                    elif not engine.check_exit(j):
                        engine.update_guardian(j)
                    if engine.position is None:break
                    if engine.guardian_policy:
                        current = engine.position["sl"]
                        if (current-protected)*(1 if side == "buy" else -1) < -1e-8:
                            raise AssertionError("a confirmed stop moved backwards")
                        protected = current
                    if engine.t[j]+60 in m5_closes:
                        engine.check_structure(engine.t[j]+60)
                if engine.position is not None:
                    j = len(engine.t)-1
                    price = engine.c[j]+(engine.spreads[j] if side == "sell" else 0.)
                    engine.close(engine.t[j]+60,price,"缓存结束")
                trade = engine.trades[-1]
                result[name] = {k:trade[k] for k in ("exit","exit_reason","pnl","hold_min")}
            pairs.append(result)
    delta = [p["candidate"]["pnl"]-p["baseline"]["pnl"] for p in pairs]
    source_hashes = {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (root/"xauusd_fib_volume_pa_trader.py",root/"xauusd_fib_pa_backtest.py",
                               root/"strategies"/"profit_guard.py")}
    output = dict(method="independent fixed-entry closed-M1 control; stops effective next bar; no AI1/2/3 or tick path",
        limitation="not a single-position portfolio; reused cache and legacy entries are not the active adaptive AI strategy",
        entries_file=str(entries_path),entries_sha256=hashlib.sha256(entries_path.read_bytes()).hexdigest(),
        count=len(pairs),range_bj=[entries[0]["open_bj"],entries[-1]["open_bj"]],
        original_risk_cap=base.MAX_SL_ABS,lot=base.LOT_SIZE,common_deadline_minutes=90,
        common_fee_cash=meta["guardian_fee_cash"],policy=rules,baseline_policy=old.guardian_policy or "legacy",
        guardian_policy=args.guardian_policy,baseline_mode=args.baseline_policy,
        improved=sum(x>.005 for x in delta),worsened=sum(x<-.005 for x in delta),
        unchanged=sum(abs(x)<=.005 for x in delta),source_hashes=source_hashes,pairs=pairs)
    output["cache_sha256"] = {p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted((root/"xau_fib_pa_logs"/"backtest").glob("rates_*")) if p.is_file()}
    path = (Path(args.output).resolve() if args.output else root/"xau_fib_pa_logs"/"backtest"/
            ("profit_guard_fixed_"+datetime.now().strftime("%Y%m%d_%H%M%S_%f")+".json"))
    path.write_text(json.dumps(output,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    print(json.dumps({k:v for k,v in output.items() if k not in ("pairs","source_hashes")},ensure_ascii=False))
    print("output:",path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
