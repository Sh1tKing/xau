"""Closed-source regime/entry tests, including BUY/SELL price mirrors."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

from strategies import chop


CLOCK = datetime(2026, 10, 20, 19, 45, 3, tzinfo=timezone.utc)  # Beijing 03:45


def bars(closes=None, clock=CLOCK):
    values = closes or ([100., 102., 101., 99., 98., 100.] * 4 + [100., 100.])
    end = int((clock.timestamp()+10800)//300)*300
    rows = []
    for i, value in enumerate(values):
        opening = values[i-1] if i else value
        rows.append(dict(time=end-(len(values)-i)*300, open=opening,
                         high=max(opening,value)+.2, low=min(opening,value)-.2, close=value))
    return rows


def mirror(rows):
    return [dict(r, open=300-r['open'], high=300-r['low'], low=300-r['high'], close=300-r['close']) for r in rows]


def directional_bars(clock=CLOCK):
    values=([100.,108.]*6 + [108.,107.,106.,105.,104.,103.,102.,101.,100.,99.,98.,97.] + [96.8,96.7])
    return [dict(r,open=200-r['open'],high=200-r['low'],low=200-r['high'],close=200-r['close'])
            for r in bars(values,clock=clock)]


def state(rows=None, clock=CLOCK, offset=3, tick=.01):
    return chop.analyze(rows if rows is not None else bars(clock=clock), now=clock,
                        server_utc_offset=offset, tick_size=tick)


class RepeatedChopTests(unittest.TestCase):
    def test_low_efficiency_plus_repeated_crosses_is_chop(self):
        s = state()
        self.assertEqual(s['state'], 'chop')
        self.assertTrue(s['night_guard'])
        self.assertEqual(s['selected_window'], '24')
        self.assertLessEqual(s['windows']['24']['efficiency'], .35)
        self.assertGreaterEqual(s['windows']['24']['midpoint_crosses'], 3)
        self.assertGreaterEqual(s['windows']['24']['reversals'], 5)
        for side in ('buy','sell'):
            d = chop.decide(s, side, bid=100., ask=100.2)
            self.assertFalse(d['allowed'])
            self.assertIn('反复震荡', d['reason'])

    def test_directional_move_and_flat_without_crosses_are_not_called_chop(self):
        for values in ([98+i*.15 for i in range(26)], [100.]*26):
            s = state(bars(values))
            self.assertEqual(s['state'], 'unconfirmed')
            for side in ('buy','sell'):
                self.assertTrue(chop.decide(s,side,bid=100.,ask=100.2)['allowed'])

    def test_single_spike_and_single_engulf_do_not_release_middle_entry(self):
        rows = bars()
        rows[-1].update(open=99., high=103., low=98.8, close=102.8)
        for rows, side, bid, ask in ((rows,'buy',102.8,103.), (mirror(rows),'sell',197.,197.2)):
            self.assertFalse(chop.decide(state(rows),side,bid=bid,ask=ask)['allowed'])

    def test_two_closed_breakout_bars_and_current_bid_release_mirrored_sides(self):
        rows = bars()
        rows[-2].update(open=102., high=103.2, low=101.8, close=103.)
        rows[-1].update(open=103., high=103.7, low=102.8, close=103.5)
        for r,side,bid,ask in ((rows,'buy',103.5,103.7),(mirror(rows),'sell',196.3,196.5)):
            s=state(r); d=chop.decide(s,side,bid=bid,ask=ask)
            self.assertTrue(d['allowed'], d)
            self.assertEqual(d['route'],'breakout_confirmed')
            middle = 100. if side == 'buy' else 200.
            self.assertFalse(chop.decide(s,side,bid=middle,ask=middle+.2)['allowed'])
        self.assertEqual(state(rows)['windows'], state()['windows'], 'confirmation must not redraw the box')

    def test_recent_directional_leg_is_not_forbidden_by_old_wide_chop_box(self):
        rows=directional_bars()
        for r,side,bid,ask in ((rows,'buy',103.3,103.5),(mirror(rows),'sell',196.5,196.7)):
            s=state(r)
            self.assertEqual(s['state'],'chop')
            self.assertTrue(s['windows']['24']['choppy'])
            self.assertFalse(s['windows']['12']['choppy'])
            d=chop.decide(s,side,bid=bid,ask=ask)
            self.assertTrue(d['allowed'],d)
            self.assertEqual(d['route'],'directional_leg_confirmed')
            other='sell' if side=='buy' else 'buy'
            self.assertFalse(chop.decide(s,other,bid=bid,ask=ask)['allowed'])
            middle=(s['windows']['12']['high']+s['windows']['12']['low'])/2
            self.assertFalse(chop.decide(s,side,bid=middle-.1,ask=middle+.1)['allowed'])

    def test_edge_rejection_needs_next_close_and_current_entry_space(self):
        rows=bars()
        rows[-2].update(open=98.25,high=98.5,low=97.5,close=98.45)
        rows[-1].update(open=98.45,high=98.9,low=98.35,close=98.8)
        for r,side,bid,ask in ((rows,'buy',98.8,98.9),(mirror(rows),'sell',201.1,201.2)):
            d=chop.decide(state(r),side,bid=bid,ask=ask)
            self.assertTrue(d['allowed'], d)
            self.assertEqual(d['route'],'edge_rejection_confirmed')
            self.assertFalse(chop.decide(state(r),side,bid=100.,ask=100.2)['allowed'])
        rows[-1]['close']=98.49
        self.assertFalse(chop.decide(state(rows),'buy',bid=98.49,ask=98.59)['allowed'])

    def test_beijing_window_boundaries_are_not_server_clock_boundaries(self):
        for hour,minute,expected in ((16,59,False),(17,0,True),(22,59,True),(23,0,False)):
            clock=CLOCK.replace(hour=hour,minute=minute)
            self.assertIs(state(clock=clock)['night_guard'],expected)
        original=bars()
        shifted=[dict(r,time=r['time']-10800) for r in original]
        for key in ('efficiency','reversals','midpoint_crosses','choppy'):
            self.assertEqual(state(shifted,offset=0)['windows']['24'][key],state(original)['windows']['24'][key])
        with self.assertRaises(ValueError): state(clock=CLOCK.replace(tzinfo=None))

    def test_night_short_window_catches_recent_chop_after_prior_trend(self):
        values=[122-i*2 for i in range(12)]+[100.,102.,98.,101.,99.,102.,98.,101.,99.,102.,98.,100.]+[100.,100.]
        s=state(bars(values))
        self.assertTrue(s['windows']['12']['choppy'])
        self.assertFalse(s['windows']['24']['choppy'])
        self.assertEqual(s['selected_window'],'12')
        self.assertEqual(s['state'],'chop')
        day=CLOCK.replace(hour=7)
        self.assertEqual(state(bars(values,clock=day),clock=day)['state'],'unconfirmed')

    def test_gap_stale_duplicate_nonfinite_short_and_unknown_offset_reject(self):
        for kind in ('gap','stale','duplicate','nan','short','ohcl','offset','tick'):
            rows=bars(); offset=3; tick=.01
            if kind=='gap': rows.pop(3)
            if kind=='stale': rows=[dict(r,time=r['time']-300) for r in rows]
            if kind=='duplicate': rows[2]['time']=rows[1]['time']
            if kind=='nan': rows[4]['close']=float('nan')
            if kind=='short': rows=rows[-25:]
            if kind=='ohcl': rows[4]['high']=rows[4]['low']-1
            if kind=='offset': offset=None
            if kind=='tick': tick=0
            with self.subTest(kind=kind), self.assertRaises(ValueError): state(rows,offset=offset,tick=tick)

    def test_forming_bar_never_affects_state(self):
        rows=bars()
        forming=dict(rows[-1],time=rows[-1]['time']+300,close=500.,high=500.)
        self.assertEqual(state(rows),state(rows+[forming]))

    def test_invalid_quotes_never_release_even_without_chop(self):
        s=state(bars([100.]*26))
        for bid,ask in ((101.,100.),(0.,1.),(100.,float('nan')),(True,101.)):
            self.assertFalse(chop.decide(s,'buy',bid=bid,ask=ask)['allowed'])

    def test_sell_breakout_bid_outside_but_ask_back_inside_does_not_release(self):
        rows=bars()
        rows[-2].update(open=98.,high=98.2,low=97.,close=97.2)
        rows[-1].update(open=97.2,high=97.3,low=96.7,close=96.8)
        self.assertFalse(chop.decide(state(rows),'sell',bid=97.,ask=98.)['allowed'])

    def test_full_mirror_preserves_regime_and_metrics(self):
        a,b=state(),state(mirror(bars()))
        self.assertEqual(a['state'],b['state'])
        for key in ('efficiency','midpoint_crosses','reversals','choppy'):
            self.assertEqual(a['windows']['24'][key],b['windows']['24'][key])

    def test_declaration_is_explicit_and_cannot_be_silently_relaxed(self):
        self.assertIsNone(chop.policy_for({}))
        c={'analysis_mode':True,'demo_only':True,'parameters':{'chop_policy':deepcopy(chop.DEFAULT_POLICY)}}
        self.assertEqual(chop.policy_for(c),chop.DEFAULT_POLICY)
        for change in ({'efficiency_max':.99},{'version':'other'},{'extra':1}):
            bad=deepcopy(c); bad['parameters']['chop_policy'].update(change)
            with self.assertRaises(ValueError): chop.policy_for(bad)

    def test_binding_rejects_new_source_window_engine_route_and_missing_values(self):
        s=state(bars([100.]*26)); d=chop.decide(s,'buy',bid=100.,ask=100.2)
        binding=chop.bind(s,d)
        self.assertEqual(chop.binding_error(binding,s,d),'')
        for key in binding:
            bad=deepcopy(binding);bad[key]='changed'
            self.assertTrue(chop.binding_error(bad,s,d),key)
        self.assertTrue(chop.binding_error({},s,d))


if __name__=='__main__': unittest.main()
