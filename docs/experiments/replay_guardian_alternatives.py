"""Six pre-registered loss-side controls. Cached bars only; no live AI/MT5.

These independent fixed-entry comparisons are not the current AI portfolio.
"""
import argparse
import bisect
import copy
import csv
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from docs.experiments.replay_guardian_weakening import (
    Bar, accept_stop, integrity, metrics, offline, rejection, stop_proposal)


VERSION = "guardian-alternatives-research-v1"
RULES = {
    "fast_m1_sl": "sl",
    "soft_m5_sl": "sl",
    "failed_start_sl": "sl",
    "volatility_exit": "close",
    "fast_m1_exit": "close",
    "giveback_exit": "close",
}
EXTRA_EXIT_COST = .10


def closed_sequence(bars, seconds, opened, now, count):
    return (len(bars) == count and bars[0].time >= opened
            and bars[-1].time + seconds == now
            and all(bars[i+1].time-bars[i].time == seconds for i in range(count-1)))


def m1_push(side, opened, bars, atr, now):
    if side not in ("buy", "sell") or atr <= 0 or not closed_sequence(bars,60,opened,now,3):
        return False
    a,b,c = bars
    if rejection(b,side,atr) or rejection(c,side,atr):
        return False
    if side == "buy":
        return (b.close < b.open and c.close < c.open and c.close < b.close < a.close
                and c.high < b.high and c.low < b.low)
    return (b.close > b.open and c.close > c.open and c.close > b.close > a.close
            and c.high > b.high and c.low > b.low)


def m5_soft(side, opened, bars, atr, now):
    if side not in ("buy", "sell") or atr <= 0 or len(bars) != 2:
        return False
    p,q = bars
    if (q.time-p.time != 300 or not opened < q.time+300 <= now < q.time+600
            or rejection(q,side,atr)):
        return False
    if side == "buy":
        return q.close < q.open and q.close < p.close and q.high < p.high
    return q.close > q.open and q.close > p.close and q.low > p.low


def signal(rule, *, side, opened, now, bars1, bars5, atr1, atr5,
           first3, peak_r, entry_atr, price, entry, net):
    if rule not in RULES:
        raise ValueError("unknown research rule")
    if net > 0 or side not in ("buy","sell"):
        return False
    push = m1_push(side,opened,bars1,atr1,now)
    if rule in ("fast_m1_sl","fast_m1_exit"):
        return push
    if rule == "giveback_exit":
        return push and peak_r >= .25
    soft = m5_soft(side,opened,bars5,atr5,now)
    if rule == "soft_m5_sl":
        return (soft and closed_sequence(bars1[-2:],60,opened,now,2)
                and not rejection(bars1[-1],side,atr1))
    if rule == "volatility_exit":
        sign = 1 if side == "buy" else -1
        return (push and soft and entry_atr > 0
                and -sign*(price-entry) >= 1.5*entry_atr)
    if (not push or now-opened < 300 or peak_r >= .25 or len(first3) != 3
            or first3[0].time != opened or first3[-1].time+60 > now
            or not all(first3[i+1].time-first3[i].time == 60 for i in range(2))):
        return False
    if side == "buy":
        bound = min(b.low for b in first3)
        return all(b.close < bound for b in bars1[-2:])
    bound = max(b.high for b in first3)
    return all(b.close > bound for b in bars1[-2:])


def queued_close_status(side,price,entry,sl,tp,cash_per_price,fee,expired=False):
    sign=1 if side=="buy" else -1
    stop_hit=price<=sl if side=="buy" else price>=sl
    tp_hit=(price>=tp if side=="buy" else price<=tp) if tp>0 else False
    if stop_hit or tp_hit or expired:
        return "original_protection"
    if sign*(price-entry)*cash_per_price-fee>0:
        return "recovered"
    return "execute"


def comparison(rows,key):
    ds = [r[key]["pnl"]-r["baseline"]["pnl"] for r in rows]
    exits = sum(r[key]["market_exits"] for r in rows)
    changed = [dict(open_bj=r["open_bj"],side=r["side"],baseline=r["baseline"]["pnl"],
                    candidate=r[key]["pnl"],delta=round(d,2))
               for r,d in zip(rows,ds) if abs(d) > .005]
    m = metrics(rows,key)
    return dict(metrics=m,delta=round(sum(ds),2),
        improved=sum(d>.005 for d in ds),worsened=sum(d<-.005 for d in ds),
        unchanged=sum(abs(d)<=.005 for d in ds),
        accepted=sum(len(r[key]["accepted"]) for r in rows),market_exits=exits,
        delta_extra_exit_cost=round(sum(ds)-exits*EXTRA_EXIT_COST,2),
        net_extra_exit_cost=round(m["net"]-exits*EXTRA_EXIT_COST,2),
        on_original_losers=round(sum(d for r,d in zip(rows,ds) if r["baseline"]["pnl"]<0),2),
        on_original_winners=round(sum(d for r,d in zip(rows,ds) if r["baseline"]["pnl"]>0),2),
        winners_to_nonwin=sum(r["baseline"]["pnl"]>0 and r[key]["pnl"]<=0 for r in rows),
        losers_to_nonloss=sum(r["baseline"]["pnl"]<0 and r[key]["pnl"]>=0 for r in rows),
        worst=sorted(changed,key=lambda x:x["delta"])[:5],
        best=sorted(changed,key=lambda x:x["delta"],reverse=True)[:5])


def block_bootstrap(pairs,keys):
    dates = sorted({r["open_bj"][:10] for r in pairs})
    rng = random.Random(20261016)
    draws = [[rng.randrange(len(dates)) for _ in dates] for _ in range(2000)]
    results = {}
    for key in keys:
        totals = {d:0. for d in dates}
        for r in pairs:
            totals[r["open_bj"][:10]] += r[key]["pnl"]-r["baseline"]["pnl"]
        values = [totals[d] for d in dates]
        sampled = sorted(sum(values[i] for i in draw) for draw in draws)
        results[key] = dict(days=len(dates),resamples=2000,seed=20261016,
                           delta_range_95=[round(sampled[49],2),round(sampled[1949],2)])
    return results


def run(replay,entries_path,limit):
    base = replay.base
    if abs(base.LOT_SIZE-.01)>1e-10 or abs(base.MAX_SL_ABS-8.)>1e-10:
        raise ValueError("research requires unchanged .01 lot / SL8")
    data,meta = replay.load_history()
    meta["guardian_fee_cash"] = .10
    entries = list(csv.DictReader(entries_path.open(encoding="utf-8-sig",newline="")))
    if limit:
        entries = entries[-limit:]
    configs = {"baseline":None}
    configs.update({f"{rule}_wait_{wait}":(rule,action,wait)
                    for rule,action in RULES.items() for wait in range(3)})
    engines = {key:replay.Backtest(data,meta,55,guardian_policy="profit_floor_v3") for key in configs}
    control = engines["baseline"]
    if control.guardian_policy["cash_floors"] != [[2.,.2],[4.,1.],[6.,2.],[10.,4.],[15.,6.]]:
        raise ValueError("original staircase changed")
    m5_window = control.windows["M5"]
    @lru_cache(maxsize=1024)
    def cached_context(end):
        frame=m5_window.df.iloc[max(0,end-120):end].reset_index(drop=True)
        return frame,base.calculate_atr(frame) if len(frame)>=20 else 0.
    def context(ts):
        return cached_context(bisect.bisect_right(m5_window.close_times,ts))
    for engine in engines.values():
        engine.guardian_context=context
    def get_bars(df):
        return [Bar(*map(float,r)) for r in df[["time","open","high","low","close"]].itertuples(index=False,name=None)]
    m1,m5 = get_bars(data["M1"]),get_bars(data["M5"])
    tr=[max(b.high-b.low,abs(b.high-m1[i-1].close),abs(b.low-m1[i-1].close)) if i else b.high-b.low
        for i,b in enumerate(m1)]
    atr1=[sum(tr[max(0,i-13):i+1])/min(i+1,14) for i in range(len(tr))]
    pairs=[]
    started=time.monotonic()
    for entry_no,entry in enumerate(entries):
        ts=int(entry["fill_server_ts"])
        i=bisect.bisect_left(control.t,ts)
        if i==len(control.t) or control.t[i]!=ts:
            raise ValueError("fixed entry minute missing")
        side=entry["side"].lower()
        sign=1 if side=="buy" else -1
        initial_entry=float(entry["entry"])
        risk=sign*(initial_entry-float(entry["sl"]))
        _,initial_atr=context(ts)
        end=min(len(control.t),bisect.bisect_left(control.t,ts+90*60)+1)
        facts={}
        peak_r=0.
        for j in range(i,end):
            now=control.t[j]+60
            price=control.c[j]+(control.spreads[j] if side=="sell" else 0.)
            net=sign*(price-initial_entry)*control.lot_value-control.guard_fee
            peak_r=max(peak_r,net/(risk*control.lot_value))
            _,a5=context(now)
            m5_end=bisect.bisect_right(m5_window.close_times,now)
            b1=m1[max(0,j-2):j+1]
            b5=m5[max(0,m5_end-2):m5_end]
            arguments=dict(side=side,opened=ts,now=now,bars1=b1,bars5=b5,atr1=atr1[j],atr5=a5,
                first3=m1[i:i+3],peak_r=peak_r,entry_atr=initial_atr,price=price,entry=initial_entry,net=net)
            facts[j]=dict(trigger={rule:signal(rule,**arguments) for rule in RULES},
                          bars1=b1,m5_end=int(b5[-1].time+300) if b5 else 0,price=price,atr=a5)
        proposal=base.StrategyResult(side,int(entry["score"]),False,"fixed alternative research",
            float(entry["sl"]),float(entry["tp"]),0,0,
            dict(session=entry["session"],setup=entry["strategy_id"],strategy_contract=dict(
                id=entry["strategy_id"],max_hold_minutes=90,cooldown_seconds=60)))
        row=dict(open_bj=entry["open_bj"],side=side,fill_server_ts=ts,entry=initial_entry,
                 sl=float(entry["sl"]),tp=float(entry["tp"]))
        for key,engine in engines.items():
            engine.open_position(proposal,i,ts)
            if engine.position is None or abs(engine.position["state"].entry_price-initial_entry)>.001:
                raise ValueError("fixed entry changed or rejected")
            pending=None
            accepted=[]
            proposed=rejected=market_exits=0
            config=configs[key]
            for j in range(i,len(engine.t)):
                pos=engine.position
                state=pos["state"]
                guard=state.profit_floor
                before=pos["sl"]
                if pending and j>=pending["index"]:
                    live=engine.o[j]+(engine.spreads[j] if side=="sell" else 0.)
                    if pending["action"]=="close":
                        status=queued_close_status(side,live,initial_entry,pos["sl"],pos["tp"],
                                                   engine.lot_value,engine.guard_fee,engine.time_exit_due(j))
                        if status=="original_protection":
                            # Existing server protection or common old deadline
                            # takes precedence over a queued experimental exit.
                            engine.check_exit(j)
                            if engine.position is not None:
                                raise AssertionError("opening protection was not honored")
                            break
                        if status=="execute":
                            accepted.append(dict(action="close",signal_ts=pending["signal_ts"],
                                signal_bar_open=pending["signal_bar_open"],apply_ts=engine.t[j],
                                m5_end=pending["m5_end"],price=live))
                            market_exits+=1
                            engine.close(engine.t[j],live,"实验主动退出:"+config[0])
                            break
                        rejected+=1
                    else:
                        _,a5=context(engine.t[j])
                        room=guard.room(a5,engine.spreads[j],engine.point)
                        step=max(guard.rules["advance_abs"],guard.rules["advance_atr"]*max(a5,guard.atr_floor))
                        if accept_stop(side,pos["sl"],pending["stop"],live,room,step):
                            old_sl=pos["sl"]
                            pos["sl"]=pending["stop"]
                            guard.acknowledge(state.direction,pending["stop"])
                            accepted.append(dict(action="sl",signal_ts=pending["signal_ts"],
                                signal_bar_open=pending["signal_bar_open"],apply_ts=engine.t[j],
                                m5_end=pending["m5_end"],stop=pending["stop"],previous_sl=old_sl,
                                price=live,minimum_room=room))
                        else:
                            rejected+=1
                    pending=None
                if sign*(pos["sl"]-before)<-1e-8:
                    raise AssertionError("SL widened before bar execution")
                if engine.check_exit(j):
                    break
                engine.update_guardian(j)
                if sign*(pos["sl"]-before)<-1e-8:
                    raise AssertionError("SL widened during local protection")
                if config is None or pending is not None or j not in facts:
                    continue
                rule,action,wait=config
                f=facts[j]
                if not f["trigger"][rule] or f["atr"]<=0:
                    continue
                pending=dict(action=action,index=j+1+wait,signal_ts=engine.t[j]+60,
                             signal_bar_open=engine.t[j],m5_end=f["m5_end"])
                if action=="sl":
                    room=guard.room(f["atr"],engine.spreads[j],engine.point)
                    target=stop_proposal(side,f["bars1"],f["price"],f["atr"],room,
                                         engine.spreads[j],engine.direction_tick_size)
                    step=max(guard.rules["advance_abs"],guard.rules["advance_atr"]*max(f["atr"],guard.atr_floor))
                    if not accept_stop(side,pos["sl"],target,f["price"],room,step):
                        pending=None
                        continue
                    pending["stop"]=target
                proposed+=1
            if engine.position is not None:
                j=len(engine.t)-1
                price=engine.c[j]+(engine.spreads[j] if side=="sell" else 0.)
                engine.close(engine.t[j]+60,price,"缓存结束")
            trade=engine.trades[-1]
            row[key]={k:trade[k] for k in ("close_bj","exit","exit_reason","pnl","hold_min")}
            row[key].update(accepted=accepted,proposed=proposed,rejected=rejected,market_exits=market_exits)
            if config:
                for event in accepted:
                    if not (event["m5_end"]<=event["signal_ts"]
                            and event["apply_ts"]>=event["signal_ts"]+config[2]*60
                            and event["apply_ts"]>=event["signal_bar_open"]+60):
                        raise AssertionError("lookahead or same-bar execution")
        pairs.append(row)
        if (entry_no+1)%100==0 or entry_no==len(entries)-1:
            print(json.dumps(dict(progress=entry_no+1,total=len(entries),elapsed=round(time.monotonic()-started,1))),flush=True)
    return dict(pairs=pairs,policy=copy.deepcopy(control.guardian_policy),meta=meta,configs=configs)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--limit",type=int,default=0)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    if args.limit<0:
        parser.error("limit must be nonnegative")
    entries_path=ROOT/"xau_fib_pa_logs/backtest/trades_legacy_base_20261010_121738_891629_e76a49b4.csv"
    reference=ROOT/".superpowers/sdd/weakening-risk-20261016/full-1958.json"
    protocol=Path(__file__).with_name("2026-10-16-guardian-alternatives-protocol.md")
    inputs=[entries_path,reference,Path(__file__),protocol,Path(__file__).with_name("replay_guardian_weakening.py")]
    inputs+=sorted((ROOT/"xau_fib_pa_logs/backtest").glob("rates_*"))
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    before=integrity(ROOT)
    with offline(ROOT) as replay:
        result=run(replay,entries_path,args.limit)
    if before!=integrity(ROOT):
        raise AssertionError("existing production/configuration/log/state changed")
    pairs=result["pairs"]
    old=json.loads(reference.read_text(encoding="utf-8"))
    frozen={r["fill_server_ts"]:r for r in old["pairs"]}
    for r in pairs:
        original=frozen[r["fill_server_ts"]]
        if (r["side"]!=original["side"] or any(r["baseline"][k]!=original["baseline"][k]
                for k in ("exit","pnl","hold_min","close_bj"))):
            raise AssertionError("baseline differs from frozen full control")
    keys=[k for k in result["configs"] if k!="baseline"]
    groups={"all":pairs,"buy":[r for r in pairs if r["side"]=="buy"],
            "sell":[r for r in pairs if r["side"]=="sell"],
            "June_August":[r for r in pairs if r["open_bj"]<"2026-09-01"],
            "September_October":[r for r in pairs if r["open_bj"]>="2026-09-01"]}
    for month in sorted({r["open_bj"][:7] for r in pairs}):
        groups[month]=[r for r in pairs if r["open_bj"].startswith(month)]
    summary={g:dict(baseline=metrics(rows,"baseline"),
                   **{k:comparison(rows,k) for k in keys}) for g,rows in groups.items()}
    result.update(version=VERSION,inputs_sha256=hashes,summary=summary,
        block_bootstrap=block_bootstrap(pairs,keys),source_integrity_unchanged=True,
        integrity_file_count=len(before),frozen_baseline_exact_matches=len(pairs),
        extra_exit_cost=EXTRA_EXIT_COST,
        limitations="reused local entries, independent overlapping tickets; not AI or current portfolio; no real tick/broker/slippage; old common 90min control")
    output=Path(args.output).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    with output.with_suffix(".csv").open("w",encoding="utf-8",newline="") as handle:
        fields=["open_bj","side","entry","sl","tp","baseline"]+keys
        writer=csv.DictWriter(handle,fieldnames=fields)
        writer.writeheader()
        for r in pairs:
            writer.writerow({k:r[k]["pnl"] if k in keys or k=="baseline" else r[k]
                             for k in fields})
    print(json.dumps(dict(version=VERSION,n=len(pairs),baseline=summary["all"]["baseline"],
        candidates={k:{q:summary["all"][k][q] for q in ("delta","improved","worsened","market_exits","delta_extra_exit_cost")}
                    for k in keys},unchanged=True,output=str(output)),ensure_ascii=False),flush=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
