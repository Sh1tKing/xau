"""Pure confirmed M15 regions and reconstructible first M5 entry episodes.

All parameters are research hypotheses. No broker, environment or wall clock access.
"""
from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo
from crypto_demo.strategies.events import make_entry_event, validate_entry_event


def _rows(df, tf, now=None, offset=None):
    step = 300 if tf == 'M5' else 900
    rows = df[['time', 'open', 'high', 'low', 'close']].to_dict('records')
    previous = None
    for row in rows:
        for key, value in row.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('invalid/nonfinite ' + tf + ' ' + key)
        t = row['time']
        if t != int(t) or t % step or previous is not None and t <= previous:
            raise ValueError('invalid/duplicate/out-of-order ' + tf + ' timestamps')
        row['time'] = int(t)
        if row['high'] < max(row['open'], row['close'], row['low']) or row['low'] > min(row['open'], row['close']):
            raise ValueError('invalid ' + tf + ' OHLC')
        if now is not None and t + step - offset * 3600 > now:
            raise ValueError('unclosed/future ' + tf)
        previous = t
    if not rows:
        raise ValueError('missing ' + tf + ' coverage')
    return rows


def _clock(now, offset):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('aware decision clock required')
    if type(offset) not in (float, int) or not math.isfinite(offset) or abs(offset) > 14:
        raise ValueError('explicit server offset required')
    return now.timestamp()


def _continuous(rows, step):
    return all(b['time'] - a['time'] == step for a, b in zip(rows, rows[1:]))


def _refs(rows, tf):
    return [{'timeframe': tf, 'bar_time': r['time']} for r in rows]


def active_window(now):
    """Current contiguous union of local London/NY intervals, with DST."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('aware decision clock required')
    spans = []
    for zone, end_hour in [('Europe/London', 17), ('America/New_York', 16)]:
        local = now.astimezone(ZoneInfo(zone))
        for delta in (-1, 0, 1):
            day = (local + timedelta(days=delta)).date()
            start = datetime(day.year, day.month, day.day, 8, tzinfo=ZoneInfo(zone))
            end = start.replace(hour=end_hour)
            spans.append((start.timestamp(), end.timestamp(), zone, str(day)))
    unions = []
    for start, end, zone, day in sorted(spans):
        if unions and start <= unions[-1]['end_utc']:
            unions[-1]['end_utc'] = max(end, unions[-1]['end_utc'])
            unions[-1]['members'].append({'zone': zone, 'local_date': day})
        else:
            unions.append({'start_utc': start, 'end_utc': end, 'members': [{'zone': zone, 'local_date': day}]})
    return next((w for w in unions if w['start_utc'] <= now.timestamp() < w['end_utc']), None)


def confirmed_regions(df_m15, now, server_offset_hours, params):
    """Freeze pivot geometry/ATR at right2 close; retain original provenance."""
    result = {'regions': [], 'blockers': []}
    try:
        clock = _clock(now, server_offset_hours)
        rows = _rows(df_m15, 'M15', clock, server_offset_hours)
        if len(rows) < 17:
            raise ValueError('insufficient M15 ATR/pivot coverage')
        # A pivot is scoped by anchor in the last48, never by a rolling index ID.
        for i in range(max(2, len(rows)-params['pivot_lookback']), len(rows)-2):
            confirmation = rows[i+2]
            expiry = confirmation['time'] + 900 + params['region_valid_bars'] * 900 - server_offset_hours*3600
            if clock >= expiry:
                continue
            five = rows[i-2:i+3]
            for kind, column, compare in [('support', 'low', min), ('resistance', 'high', max)]:
                value = rows[i][column]
                others = [r[column] for j, r in enumerate(five) if j != 2]
                if value != compare([value] + others) or value in others:
                    continue
                first = i+2-params['atr_period']
                atr_rows = rows[max(0, first):i+3]
                if first < 0 or len(atr_rows) != params['atr_period']+1 or not _continuous(atr_rows, 900) or not _continuous(five, 900):
                    result['blockers'].append('missing relevant M15 pivot/ATR coverage')
                    continue
                trs = [max(r['high']-r['low'], abs(r['high']-p['close']), abs(r['low']-p['close'])) for p, r in zip(atr_rows, atr_rows[1:])]
                atr = sum(trs) / params['atr_period']
                if atr <= 0:
                    result['blockers'].append('nonpositive frozen ATR')
                    continue
                width = params['halfwidth_atr'] * atr
                result['regions'].append({'region_id': f"{kind}/{rows[i]['time']}/{confirmation['time']}", 'type': kind,
                    'anchor_bar_time': rows[i]['time'], 'confirmation_bar_time': confirmation['time'],
                    'confirmation_close_utc': confirmation['time']+900-server_offset_hours*3600,
                    'expiry_utc': expiry, 'center': value, 'lower': value-width, 'upper': value+width, 'atr': atr,
                    'pivot_bars': five, 'source_refs': _refs(five, 'M15'), 'atr_bars': atr_rows,
                    'atr_source_refs': _refs(atr_rows, 'M15')})
        result['regions'].sort(key=lambda r: r['region_id'])
        result['blockers'] = sorted(set(result['blockers']))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        result['blockers'].append(str(exc))
    return result


def _cycles(region, m5, m15, offset, p):
    """Touch cycles reset only after whole-range leave plus M15 confirmation."""
    cycles, episode, leave_time = [], None, None
    for bar in m5:
        close = bar['time']+300-offset*3600
        if close <= region['confirmation_close_utc']:
            continue
        touches = bar['low'] <= region['upper'] and bar['high'] >= region['lower']
        leaves = bar['low'] >= region['upper']+p['reset_atr']*region['atr'] or bar['high'] <= region['lower']-p['reset_atr']*region['atr']
        if episode is not None and leaves:
            if leave_time is None:
                leave_time = close
            confirm = [r for r in m15 if leave_time <= r['time']+900-offset*3600 <= close and
                (r['close'] >= region['upper']+p['reset_atr']*region['atr'] or r['close'] <= region['lower']-p['reset_atr']*region['atr'])]
            if confirm:
                episode = None
        elif not leaves:
            leave_time = None
        if episode is None and touches:
            episode, leave_time = bar['time'], None
        cycles.append((bar, episode))
    return cycles


def region_facts(kind, df_m15, df_m5, now, server_offset_hours, action, params):
    out = {'action': action, 'server_offset_hours': server_offset_hours, 'closed_trigger': {'confirmed': False},
           'blockers': [], 'candidates': [], 'selection_order': []}
    try:
        clock = _clock(now, server_offset_hours)
        if action not in ('buy', 'sell') or kind not in ('retest', 'rejection'):
            raise ValueError('invalid region action/kind')
        m15 = _rows(df_m15, 'M15', clock, server_offset_hours)
        m5 = _rows(df_m5, 'M5', clock, server_offset_hours)
        if len(m5) < 3 or not _continuous(m5[-3:], 300):
            raise ValueError('missing last3 M5 protection coverage')
        found = confirmed_regions(df_m15, now, server_offset_hours, params)
        out['blockers'].extend(found['blockers'])
        window = active_window(now)
        out['active_window'] = window
        if window is None:
            raise ValueError('outside local region entry window')
        sign = 1 if action == 'buy' else -1
        latest = m5[-1]
        latest_m15 = math.floor((clock+server_offset_hours*3600)/900)*900-900
        if m15[-1]['time'] != latest_m15:
            raise ValueError('missing latest closed M15 context')
        for region in found['regions']:
            diagnostic = {'region_id': region['region_id'], 'status': 'no_first_trigger'}
            out['candidates'].append(diagnostic)
            if kind == 'rejection' and region['type'] != ('support' if action == 'buy' else 'resistance'):
                diagnostic['status'] = 'opposite region type'
                continue
            confirm_raw = region['confirmation_bar_time']+900
            sequence = [r for r in m5 if r['time'] >= confirm_raw]
            if (not sequence or sequence[0]['time'] != confirm_raw or not _continuous(sequence, 300)
                    or confirm_raw-300 not in {r['time'] for r in m5}
                    or not _continuous([r for r in m15 if r['time'] >= region['confirmation_bar_time']], 900)):
                diagnostic['status'] = 'missing relevant M5 episode coverage'
                diagnostic['blockers'] = [diagnostic['status']]
                continue
            cycles = _cycles(region, sequence, m15, server_offset_hours, params)
            edge = region['upper'] if action == 'buy' else region['lower']
            atr = region['atr']
            trigger, episode, breakout = None, None, None
            if kind == 'rejection':
                invalid, used = set(), set()
                for bar, ep in cycles:
                    if ep is None:
                        continue
                    close = bar['time']+300-server_offset_hours*3600
                    opposing = [r for r in m15 if ep+300 <= r['time']+900 <= bar['time']+300 and
                        sign*(r['close']-(region['lower'] if action == 'buy' else region['upper'])) < -params['break_atr']*atr]
                    if opposing:
                        invalid.add(ep)
                    pierced = bar['low'] <= region['lower']-params['break_atr']*atr if action == 'buy' else bar['high'] >= region['upper']+params['break_atr']*atr
                    defended = sign*(bar['close']-region['center']) >= params['hold_atr']*atr
                    if pierced and defended and ep not in invalid and ep not in used:
                        used.add(ep)
                        trigger, episode = bar, ep
                if trigger is not None and trigger['time'] != latest['time']:
                    diagnostic['status'] = 'missed first rejection'
                    continue
            else:
                # M15 crossing requires original previous close, after pivot confirmation.
                breaks = []
                armed = True
                for prev, bar in zip(m15, m15[1:]):
                    if bar['time']+900 <= confirm_raw:
                        continue
                    level = edge+sign*params['break_atr']*atr
                    if sign*(bar['close']-edge) <= -params['invalidation_atr']*atr:
                        armed = True
                    if armed and sign*(prev['close']-level) <= 0 < sign*(bar['close']-level):
                        breaks.append(bar)
                        armed = False
                for br in breaks:
                    brclose = br['time']+900
                    consumed = False
                    for bar, ep in cycles:
                        elapsed = bar['time']+300-brclose
                        if elapsed <= 0 or elapsed > params['max_retest_seconds'] or (bar['time']-brclose)//300 >= params['max_retest_bars']:
                            continue
                        if sign*(bar['close']-edge) <= -params['invalidation_atr']*atr:
                            consumed = True
                        touch = bar['low'] <= edge+params['retest_atr']*atr if action == 'buy' else bar['high'] >= edge-params['retest_atr']*atr
                        defend = sign*(bar['close']-edge) >= params['hold_atr']*atr
                        if touch and defend and not consumed:
                            trigger, episode, breakout, consumed = bar, ep if ep is not None else bar['time'], br, True
                    # consumed remains true through this break; later bars cannot revive it.
                if trigger is not None and trigger['time'] != latest['time']:
                    diagnostic['status'] = 'missed first retest'
                    continue
            if trigger is None:
                continue
            close = trigger['time']+300-server_offset_hours*3600
            if not window['start_utc'] <= close < window['end_utc']:
                diagnostic['status'] = 'trigger outside window'
                continue
            end = min(close+300, window['end_utc'], region['expiry_utc'])
            if not close <= clock < end:
                diagnostic['status'] = 'expired trigger'
                continue
            refs = region['source_refs']+region['atr_source_refs']+_refs([r for r in m15 if region['confirmation_bar_time'] <= r['time'] and r['time']+900 <= trigger['time']+300], 'M15')+_refs([r for r in m5 if confirm_raw-300 <= r['time'] <= trigger['time']], 'M5')
            if breakout:
                refs += _refs([r for r in m15 if region['confirmation_bar_time'] <= r['time'] <= breakout['time']], 'M15')
            refs = list({(r['timeframe'], r['bar_time']): r for r in refs}.values())
            event_id = f"region/{region['region_id']}/touch/{episode}/{action}"
            event = make_entry_event(event_id=event_id, region_id=region['region_id'], episode_start_bar_time=episode,
                action=action, trigger_timeframe='M5', trigger_bar_time=trigger['time'], trigger_close_utc=close,
                valid_until_utc=end, source_refs=refs).to_dict()
            boundary = edge if kind == 'retest' else (region['lower'] if action == 'buy' else region['upper'])
            anchor = boundary-sign*params['invalidation_atr']*atr
            # Include overlapping opposing zones: entry already inside a barrier has no free forward room.
            obstacle_regions = [{**r, 'obstacle_price': r['lower'] if action == 'buy' else r['upper']}
                for r in found['regions']
                if r['type'] == ('resistance' if action == 'buy' else 'support')
                and sign*((r['upper'] if action == 'buy' else r['lower'])-trigger['close']) >= 0]
            candidate = {'region': region, 'event_id': event_id, 'entry_event': event,
                'closed_trigger': {'confirmed': True, 'timeframe': 'M5', 'bar_time': trigger['time'], 'action': action},
                'risk_anchor': {'price': anchor, 'source_refs': list({(r['timeframe'], r['bar_time']): r for r in region['source_refs']+region['atr_source_refs']}.values())},
                'target_obstacles': sorted(obstacle_regions, key=lambda r: (sign*(r['obstacle_price']-trigger['close']), r['region_id'])),
                'protection_bars': m5[-3:], 'trigger_bar': trigger, 'breakout_bar': breakout,
                'distance': abs(trigger['close']-region['center'])}
            diagnostic['status'] = 'confirmed'
            diagnostic['candidate'] = candidate
        valid = [d['candidate'] for d in out['candidates'] if 'candidate' in d]
        valid.sort(key=lambda c: (-c['closed_trigger']['bar_time'], c['distance'], c['region']['region_id']))
        out['selection_order'] = [c['region']['region_id'] for c in valid]
        if valid:
            out.update(valid[0])
            error = validate_entry_event(out, now, server_offset_hours, df_m5, df_m15)
            if error:
                out['blockers'].append(error)
                out['closed_trigger']['confirmed'] = False
        else:
            out['blockers'].extend(b for d in out['candidates'] for b in d.get('blockers', []))
            out['blockers'].append('no latest first region trigger')
        out['blockers'] = sorted(set(out['blockers']))
        if out['blockers']:
            out['closed_trigger']['confirmed'] = False
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        out['blockers'].append(str(exc))
    return out
