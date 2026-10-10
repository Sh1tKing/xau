"""Read-only cached cohort diagnosis. No MT5, AI, trading or parameter search.

Fixed-entry SL/TP-only paths are independent ticket diagnostics, NOT a portfolio
backtest: their longer holdings can overlap other original entries.
"""
import bisect
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[2]


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def trade_stats(rows):
    pnl = [float(r['pnl']) for r in rows]
    winners, losers = [v for v in pnl if v > 0], [v for v in pnl if v < 0]
    exits = {}
    for row in rows:
        reason = row['exit_reason']
        group = exits.setdefault(reason, dict(trades=0, net=0.0, losses=0))
        group['trades'] += 1
        group['net'] += float(row['pnl'])
        group['losses'] += float(row['pnl']) < 0
    for group in exits.values():
        group['net'] = round(group['net'], 2)
    return dict(trades=len(rows), net=round(sum(pnl), 2), wins=len(winners), losses=len(losers),
        win_rate=round(len(winners) / len(rows), 4),
        profit_factor=round(sum(winners) / -sum(losers), 4) if losers else None,
        average_win=round(statistics.mean(winners), 2) if winners else None,
        average_loss=round(statistics.mean(losers), 2) if losers else None,
        median_initial_risk=round(statistics.median(abs(float(r['sl'])-float(r['entry'])) for r in rows), 3),
        median_planned_rr=round(statistics.median(abs(float(r['tp'])-float(r['entry'])) /
            abs(float(r['sl'])-float(r['entry'])) for r in rows), 3), exits=exits)


def path_diagnosis(row, bars, times, offset, point, spread_multiplier, lot_value):
    start = int(row['fill_server_ts'])
    index = bisect.bisect_left(times, start)
    assert index < len(times) and times[index] == start
    sign = 1 if row['side'] == 'BUY' else -1
    entry, sl, tp = (float(row[k]) for k in ('entry', 'sl', 'tp'))
    risk = abs(entry-sl)
    assert risk > 0 and sign*(entry-sl) > 0 and sign*(tp-entry) > 0
    original_exit = int(datetime.strptime(row['close_bj'], '%Y-%m-%d %H:%M').replace(
        tzinfo=timezone(timedelta(hours=8))).timestamp() + offset*3600)
    deadline = start + (180 if row['strategy_id'] == 'session_retest' else 120)*60
    mfe = mae = 0.0
    closed_bars = 0
    for bar in bars[index:bisect.bisect_left(times, original_exit)]:
        spread = float(bar['spread'])*point*spread_multiplier
        high = float(bar['high']) + (spread if sign < 0 else 0)
        low = float(bar['low']) + (spread if sign < 0 else 0)
        mfe = max(mfe, high-entry if sign > 0 else entry-low)
        mae = max(mae, entry-low if sign > 0 else high-entry)
        closed_bars += 1
    result = dict(event_id=row['event_id'], strategy_id=row['strategy_id'],
        original_pnl=float(row['pnl']), original_exit=row['exit_reason'],
        completed_bars_before_exit=closed_bars,
        mfe_before_exit_r=round(mfe/risk, 4), mae_before_exit_r=round(mae/risk, 4))
    # Fixed policy defined before reading its outcomes, no target/stop grid.
    expected = start
    for bar in bars[index:]:
        t = int(bar['time'])
        if t != expected:
            result['fixed_path_status'] = 'unknown_missing_minute'
            return result
        spread = float(bar['spread'])*point*spread_multiplier
        ask_add = spread if sign < 0 else 0
        o, h, l = (float(bar[k])+ask_add for k in ('open', 'high', 'low'))
        reason = price = None
        if (o <= sl if sign > 0 else o >= sl):
            reason, price = 'initial_stop_gap', o
        elif t >= deadline:
            reason, price = 'original_max_hold', o
        elif (l <= sl if sign > 0 else h >= sl):
            reason, price = 'initial_stop', sl
        elif (h >= tp if sign > 0 else l <= tp):
            reason, price = 'original_target', tp
        if reason:
            result.update(fixed_path_status='known', fixed_exit=reason,
                fixed_pnl_per_ticket=round(sign*(price-entry)*lot_value, 2),
                fixed_hold_min=(t-start)//60)
            return result
        expected += 60
    result['fixed_path_status'] = 'unknown_cache_end'
    return result


def main():
    matrix = json.loads((ROOT/'xau_fib_pa_logs/build/matrix_results.json').read_text(encoding='utf-8'))
    bars = read_csv(ROOT/'xau_fib_pa_logs/backtest/rates_M1.csv')
    times = [int(b['time']) for b in bars]
    assert all(a < b for a, b in zip(times, times[1:])), 'M1 ordering'
    output = dict(scope='Original cached trades and independent fixed-entry ticket paths; not AI or portfolio performance',
        path_policy='Original entry, SL/TP and strategy max hold; same historical spread; SL before TP; missing minute unknown; no new signals or tuned parameters',
        mfe_scope='Only completed M1 bars strictly before original exit minute; intraminute path unknown', cases=[])
    for case in matrix:
        if case['case'] not in ('baseline', 'retest', 'rejection', 'all3'):
            continue
        path = Path(case['summary_path'])
        rows = read_csv(path.with_name(path.name.replace('summary_', 'trades_').replace('.json', '.csv')))
        assert len(rows) == case['metrics']['trades']
        assert abs(sum(float(r['pnl']) for r in rows)-case['metrics']['net']) < .02
        metadata = case['metrics']['metadata']
        paths = [path_diagnosis(r, bars, times, metadata['server_offset'], metadata['point'],
                               case['spread_multiplier'], metadata['contract_size']*.01) for r in rows]
        grouped = {}
        for sid in sorted(set(r['strategy_id'] for r in rows)):
            group = [r for r in rows if r['strategy_id'] == sid]
            p = [r for r in paths if r['strategy_id'] == sid]
            known = [r for r in p if r['fixed_path_status'] == 'known']
            stats = trade_stats(group)
            stats['fixed_entry_ticket_diagnosis'] = dict(
                known=len(known), unknown=len(p)-len(known),
                originally_losing_tickets=len([r for r in known if r['original_pnl'] < 0]),
                losing_tickets_rescued=len([r for r in known if r['original_pnl'] < 0 and r['fixed_pnl_per_ticket'] > 0]),
                winning_tickets_lost=len([r for r in known if r['original_pnl'] > 0 and r['fixed_pnl_per_ticket'] < 0]),
                improved_tickets=len([r for r in known if r['fixed_pnl_per_ticket'] > r['original_pnl']]),
                worsened_tickets=len([r for r in known if r['fixed_pnl_per_ticket'] < r['original_pnl']]),
                original_target_reached=len([r for r in known if r['fixed_exit'] == 'original_target']),
                original_stop_reached=len([r for r in known if r['fixed_exit'].startswith('initial_stop')]),
                losing_tickets_with_pre_exit_mfe_below_05r=len([r for r in p if r['original_pnl'] < 0 and r['mfe_before_exit_r'] < .5]))
            grouped[sid] = stats
        output['cases'].append(dict(case=case['case'], by_strategy=grouped, ticket_paths=paths))
    out = ROOT/'xau_fib_pa_logs/build/intraday_failure_diagnosis.json'
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps([{k:v for k,v in c.items() if k != 'ticket_paths'} for c in output['cases']], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
