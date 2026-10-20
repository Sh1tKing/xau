"""Saved-input filter audit; no synthetic trades, AI, MT5, env or log writes.

Run from the repository: python -X utf8 docs/experiments/replay_chop_filter.py
Results select ORIGINAL settled outcomes only, not a strategy backtest/P&L.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from strategies import chop


def audit(folder, start, through):
    inputs, executions, tickets, settlements = {}, {}, {}, {}
    sources, skipped = [], Counter()
    files = sorted((folder/'ai_decisions').glob('*.jsonl'))
    for source in files:
        if not start <= source.stem <= through:
            continue
        cap, last = source.stat().st_size, None
        sha=hashlib.sha256(); hashed_bytes=0
        with source.open('rb') as handle:
            number=0
            while handle.tell() < cap:
                line=handle.readline(); number+=1
                if not line or handle.tell()>cap:
                    skipped['partial_at_read_boundary']+=1;break
                sha.update(line)
                hashed_bytes+=len(line)
                try: row=json.loads(line)
                except ValueError:
                    skipped['unreadable_jsonl']+=1;continue
                last=row.get('timestamp'); stage=row.get('stage'); p=row.get('payload') or {}; did=row.get('decision_id')
                if stage=='ai1_input':
                    pack=p.get('market_evidence') or {}
                    if pack.get('symbol')!='XAUUSD':continue
                    candidates=[]
                    for c in pack.get('opportunities',[]):
                        side=c.get('action')
                        candidates.append(dict(action=side,opportunity_id=c.get('opportunity_id'),
                            offset=c.get('strategy_evidence',{}).get(side,{}).get('evidence',{}).get('server_offset_hours')))
                    if not candidates:
                        candidates=[dict(action=side,opportunity_id=None,offset=assess.get('evidence',{}).get('server_offset_hours'))
                                    for side,assess in pack.get('strategy_evidence',{}).items()]
                    inputs[did]=dict(timestamp=last,source=source.name,line=number,quote=pack.get('live_quote',{}),
                                     bars=pack.get('m5_bars',[])[-26:],candidates=candidates)
                elif stage=='execution_result' and p.get('placed'):
                    executions[did]=dict(timestamp=last,proposal=p.get('proposal') or {})
                elif stage=='ai3_input':
                    for position in (p.get('market_evidence') or {}).get('positions',[]):
                        did_open=(position.get('entry_context') or {}).get('decision_id')
                        if did_open and position.get('ticket'):
                            tickets.setdefault(did_open,set()).add(int(position['ticket']))
        sources.append(dict(path=str(source),bytes_limit=cap,bytes_hashed=hashed_bytes,
                            sha256=sha.hexdigest(),last_valid_timestamp=last))
    for source in sorted(folder.glob('Fib_PA_AI3_*.log')):
        date=source.stem.removeprefix('Fib_PA_AI3_')
        if not start <= date <= through:continue
        raw=source.read_bytes()
        for number,line in enumerate(raw.decode('utf-8-sig').splitlines(),1):
            if ('平仓结算 ticket=' not in line and '平仓记录 ticket=' not in line) or 'source=broker_history' not in line:
                continue
            ticket=re.search(r'ticket=(\d+)',line)
            prices=re.search(r'price_open=([\d.]+) price_close=([\d.]+)',line)
            net=re.search(r'net_profit=([-\d.]+)',line)
            if ticket and prices and net:
                settlements[int(ticket[1])]=dict(net=float(net[1]),price_open=float(prices[1]),price_close=float(prices[2]),
                                                source=source.name,line=number,timestamp=line[:23])
        sources.append(dict(path=str(source),bytes_limit=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
    trades=[]
    for did, execution in executions.items():
        if len(tickets.get(did,set()))!=1:
            skipped['execution_ticket_unlinked']+=1;continue
        ticket=next(iter(tickets[did])); settled=settlements.get(ticket)
        if settled is None:
            skipped['no_complete_broker_settlement']+=1;continue
        inp=inputs.get(did)
        if not inp:
            skipped['no_saved_ai1_input']+=1;continue
        proposal=execution['proposal']; side=proposal.get('action')
        opportunity=(proposal.get('details') or {}).get('opportunity_id')
        matches=[c for c in inp['candidates'] if c['action']==side and (not opportunity or c['opportunity_id']==opportunity)]
        if len(matches)!=1:
            skipped['ambiguous_candidate']+=1;continue
        quote=inp['quote']
        try:
            ctx=chop.analyze(inp['bars'],now=quote.get('observed_at'),server_utc_offset=matches[0]['offset'],tick_size=.01)
            decision=chop.decide(ctx,side,bid=quote.get('bid'),ask=quote.get('ask'))
        except (ValueError,TypeError,KeyError) as exc:
            skipped['unsupported_source']+=1
            trades.append(dict(decision_id=did,ticket=ticket,entry_time=execution['timestamp'],side=side,
                               settlement=settled,status='unavailable',error=str(exc)))
            continue
        trades.append(dict(decision_id=did,ticket=ticket,entry_time=execution['timestamp'],side=side,settlement=settled,
                           status='allowed' if decision['allowed'] else 'blocked',decision=decision,context=ctx,
                           input=inp,offset=matches[0]['offset']))
    def summary(rows):
        return dict(n=len(rows),original_wins=sum(r['settlement']['net']>0 for r in rows),
                    original_losses=sum(r['settlement']['net']<0 for r in rows),
                    original_net=round(sum(r['settlement']['net'] for r in rows),2))
    groups={}
    for label,rows in (('all_linked_settled',trades),('beijing_01_07',[r for r in trades if 1<=datetime.fromisoformat(r['entry_time']).hour<7]),
                       ('oct21_00_07',[r for r in trades if r['entry_time'].startswith('2026-10-21T0') and datetime.fromisoformat(r['entry_time']).hour<7])):
        groups[label]={status:summary([r for r in rows if r['status']==status]) for status in ('allowed','blocked','unavailable')}
    return dict(version=chop.VERSION,registered_policy=chop.DEFAULT_POLICY,broker_tick_size_assumed=.01,
                log_display_timezone_assumed='Asia/Shanghai',
                interpretation='Filter-only audit of original actual settled outcomes; no replacement AI/entries/positions simulated.',
                sources=sources,skipped=dict(skipped),summary=groups,trades=sorted(trades,key=lambda r:r['entry_time']))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log-dir',type=Path,default=ROOT/'server_deploy/xau_fib_pa_logs')
    parser.add_argument('--start',default='2026-10-14');parser.add_argument('--through',default='2026-10-21')
    parser.add_argument('--output',type=Path,default=ROOT/'.superpowers/sdd/chop-20261021/filter-audit.json')
    args=parser.parse_args();result=audit(args.log_dir,args.start,args.through)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(summary=result['summary'],skipped=result['skipped']),ensure_ascii=False))
    for trade in result['trades']:
        if trade['entry_time']>='2026-10-20T20':
            print(trade['entry_time'],trade['ticket'],trade['side'],trade['settlement']['net'],trade['status'],
                  trade.get('context',{}).get('selected_window'),trade.get('decision',{}).get('route'))
