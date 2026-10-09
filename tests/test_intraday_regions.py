import unittest
from datetime import datetime, timezone
import pandas as pd
from strategies.regions import confirmed_regions, region_facts, active_window
from strategies.definitions.region_retest import P, STRATEGY
from strategies.definitions.region_rejection import STRATEGY as REJECTION
from strategies.framework import Registry

BASE=int(datetime(2026,10,9,7,tzinfo=timezone.utc).timestamp())


def data(n=24):
    # Single strict support centered at row18; confirmation row20.
    rows=[dict(time=BASE+i*900,open=102,high=103,low=101,close=102) for i in range(n)]
    rows[18].update(low=99)
    return pd.DataFrame(rows).astype({"open":float,"high":float,"low":float,"close":float})


def clock(df, seconds=0, offset=0):
    return datetime.fromtimestamp(int(df.iloc[-1]['time'])+900-offset*3600+seconds, timezone.utc)


def m5_after(m15, count=3, values=None):
    start=BASE+21*900
    bars=[dict(time=start+i*300,open=102,high=103,low=101,close=102) for i in range(count)]
    if values:
        for i,v in values.items():bars[i].update(v)
    # Includes last fully closed M5 before region confirmation, needed coverage evidence.
    return pd.DataFrame([dict(time=start-600,open=102,high=103,low=101,close=102),dict(time=start-300,open=102,high=103,low=101,close=102)]+bars)


def m5_clock(df, offset=0, extra=0):
    return datetime.fromtimestamp(int(df.iloc[-1]['time'])+300-offset*3600+extra,timezone.utc)


class RegionTests(unittest.TestCase):
    def test_right_confirmation_no_lookahead(self):
        df=data(21)
        self.assertEqual(confirmed_regions(df.iloc[:20],clock(df.iloc[:20]),0,P)['regions'],[])
        regions=confirmed_regions(df,clock(df),0,P)['regions']
        self.assertEqual(len(regions),1)
        r=regions[0]
        self.assertEqual(r['anchor_bar_time'],BASE+18*900)
        self.assertEqual(r['confirmation_bar_time'],BASE+20*900)
        self.assertEqual(len(r['pivot_bars']),5)
        self.assertEqual(len(r['atr_bars']),15)
        self.assertAlmostEqual(r['atr'],30/14)

    def test_freeze_id_prefix_and_rolling(self):
        df=data(24);a=confirmed_regions(df.iloc[:21],clock(df.iloc[:21]),0,P)['regions'][0]
        b=confirmed_regions(df,clock(df),0,P)['regions'][0]
        c=confirmed_regions(df.iloc[2:],clock(df),0,P)['regions'][0]
        self.assertEqual((a['region_id'],a['atr']),(b['region_id'],b['atr']))
        self.assertEqual(a['region_id'],c['region_id'])
        self.assertEqual(a['source_refs'],c['source_refs'])

    def test_ties_expiry_and_ohlc(self):
        df=data(21);df.loc[17,'low']=99
        self.assertEqual(confirmed_regions(df,clock(df),0,P)['regions'],[])
        df=data(37)
        self.assertEqual(confirmed_regions(df,clock(df),0,P)['regions'],[])
        df=data(21);df.loc[10,'high']=90
        self.assertTrue(confirmed_regions(df,clock(df),0,P)['blockers'])

    def test_clock_offset_future_and_duplicates(self):
        df=data(21)
        for now,off in [(clock(df).replace(tzinfo=None),0),(clock(df),None),(clock(df),True),(clock(df),15),(clock(df),-1)]:
            self.assertTrue(confirmed_regions(df,now,off,P)['blockers'])
        good=confirmed_regions(df,clock(df,offset=2),2,P)
        self.assertEqual(len(good['regions']),1)
        dupe=df.copy();dupe.loc[10,'time']=dupe.loc[9,'time']
        self.assertTrue(confirmed_regions(dupe,clock(df),0,P)['blockers'])

    def test_relevant_atr_gap_and_unrelated_gap(self):
        df=data(21);df.loc[0,'time']=BASE-900*10
        self.assertEqual(len(confirmed_regions(df,clock(df),0,P)['regions']),1)
        bad=df.drop(index=10)
        self.assertTrue(confirmed_regions(bad,clock(df),0,P)['blockers'])

    def test_dst_union_and_end_exclusive(self):
        for day, start_hour,end_hour in [(datetime(2026,7,1,tzinfo=timezone.utc),7,20),
                                        (datetime(2026,12,1,tzinfo=timezone.utc),8,21),
                                        (datetime(2026,10,28,tzinfo=timezone.utc),8,20)]:
            win=active_window(day.replace(hour=14))
            self.assertEqual(datetime.fromtimestamp(win['start_utc'],timezone.utc).hour,start_hour)
            self.assertEqual(datetime.fromtimestamp(win['end_utc'],timezone.utc).hour,end_hour)
            self.assertEqual(len(win['members']),2)
            self.assertIsNone(active_window(day.replace(hour=end_hour)))

    def test_first_rejection_and_missed_consumed(self):
        df=data(21)
        bars=m5_after(df,1,{0:dict(low=98.6,close=100,open=100,high=101)})
        f=region_facts('rejection',df,bars,m5_clock(bars),0,'buy',P)
        self.assertEqual(f['blockers'],[])
        self.assertTrue(f['closed_trigger']['confirmed'])
        self.assertAlmostEqual(f['risk_anchor']['price'],99-.4*(30/14))
        self.assertEqual(f['entry_event']['episode_start_bar_time'],bars.iloc[-1]['time'])
        self.assertTrue(f['risk_anchor']['source_refs'])
        later=m5_after(df,2,{0:dict(low=98.6,close=100,open=100,high=101),1:dict(low=98.6,close=100,open=100,high=101)})
        g=region_facts('rejection',df,later,m5_clock(later),0,'buy',P)
        self.assertFalse(g['closed_trigger']['confirmed'])
        self.assertIn('missed first rejection',[d['status'] for d in g['candidates']])

    def test_coverage_and_closed_trigger_ttl(self):
        df=data(21);bars=m5_after(df,2,{1:dict(low=98.6,close=100,open=100,high=101)})
        # Earlier ranges are away; latest first touch.
        f=region_facts('rejection',df,bars,m5_clock(bars),0,'buy',P)
        self.assertTrue(f['closed_trigger']['confirmed'],f)
        for bad in [bars.iloc[2:],bars.drop(index=2)]:
            self.assertTrue(region_facts('rejection',df,bad,m5_clock(bars),0,'buy',P)['blockers'])
        for extra in [300,-1]:
            g=region_facts('rejection',df,bars,m5_clock(bars,extra=extra),0,'buy',P)
            self.assertFalse(g['closed_trigger']['confirmed'])

    def test_retest_break_own_wick_excluded_and_first(self):
        # Support can be broken upwards; previous postconfirmation M15 closes below upper threshold.
        df=data(23);df.loc[21,['open','high','low','close']]=[99,100,98.9,99]
        df.loc[22,['open','high','low','close']]=[99,102,98.8,101]
        start=BASE+21*900
        rows=[dict(time=start-300,open=102,high=103,low=101,close=102)]
        for i in range(7):rows.append(dict(time=start+i*300,open=101,high=102,low=100.5,close=101))
        rows[-2].update(low=99.2) # breakout's own final M5 wick, must not count
        rows[-1].update(low=99.3,close=100)
        bars=pd.DataFrame(rows)
        f=region_facts('retest',df,bars,m5_clock(bars),0,'buy',P)
        self.assertTrue(f['closed_trigger']['confirmed'],f)
        self.assertEqual(f['trigger_bar']['time'],bars.iloc[-1]['time'])
        self.assertAlmostEqual(f['risk_anchor']['price'],99+.1*(30/14)-.3*(30/14))
        nextbar=bars.iloc[-1].to_dict();nextbar['time']+=300
        later=pd.concat([bars,pd.DataFrame([nextbar])],ignore_index=True)
        g=region_facts('retest',df,later,m5_clock(later),0,'buy',P)
        self.assertFalse(g['closed_trigger']['confirmed'])

    def test_rejection_reset_requires_new_m15_close_and_restart_id(self):
        df=data(23)
        start=BASE+21*900
        # First rejection then whole M5 leave; row21 closes at12:30 to confirm reset.
        rows=[dict(time=start-300,open=102.,high=103.,low=101.,close=102.)]
        for i in range(7):
            rows.append(dict(time=start+i*300,open=102.,high=103.,low=101.,close=102.))
        rows[1].update(open=100.,high=101.,low=98.6,close=100.)
        rows[-1].update(open=100.,high=101.,low=98.6,close=100.)
        bars=pd.DataFrame(rows)
        f=region_facts('rejection',df,bars,m5_clock(bars),0,'buy',P)
        self.assertTrue(f['closed_trigger']['confirmed'],f)
        self.assertEqual(f['entry_event']['episode_start_bar_time'],bars.iloc[-1]['time'])
        # Reconstructing from a rolling prefix that still contains provenance preserves ID.
        g=region_facts('rejection',df.iloc[2:],bars,m5_clock(bars),0,'buy',P)
        self.assertEqual(f['event_id'],g['event_id'])
        # Re-touch before any new M15 close remains the old consumed cycle.
        early=bars.iloc[:4].copy();early.loc[3,['open','high','low','close']]=[100.,101.,98.6,100.]
        early_m15=df.iloc[:22].copy()
        early_m15.loc[21,['open','high','low','close']]=[100.,103.,98.6,100.]
        h=region_facts('rejection',early_m15,early,m5_clock(early),0,'buy',P)
        self.assertFalse(h['closed_trigger']['confirmed'])
        self.assertIn('missed first rejection',[d['status'] for d in h['candidates']])

    def test_opposing_m15_close_invalidates_rejection_episode(self):
        df=data(22);df.loc[21,['open','high','low','close']]=[100.,101.,98.,98.]
        bars=m5_after(df,4,{0:dict(open=99.,high=100.,low=98.8,close=99.),
            1:dict(open=99.,high=100.,low=98.8,close=99.),
            2:dict(open=99.,high=100.,low=98.8,close=99.),
            3:dict(open=100.,high=101.,low=98.6,close=100.)})
        f=region_facts('rejection',df,bars,m5_clock(bars),0,'buy',P)
        self.assertFalse(f['closed_trigger']['confirmed'])

    def test_sell_mirror_and_opposite_obstacle_refs(self):
        df=data(21)
        # Introduce confirmed resistance while keeping support pivot.
        df.loc[16,'high']=105.
        bars=pd.DataFrame([dict(time=t,open=102.,high=103.,low=101.,close=102.) for t in range(BASE+19*900-300,BASE+21*900+1,300)])
        bars.loc[len(bars)-1,['open','high','low','close']]=[104.,105.6,103.,104.]
        f=region_facts('rejection',df,bars,m5_clock(bars),0,'sell',P)
        self.assertTrue(f['closed_trigger']['confirmed'],f)
        self.assertGreater(f['risk_anchor']['price'],f['region']['upper'])
        self.assertEqual(f['target_obstacles'][0]['type'],'support')
        self.assertEqual(len(f['target_obstacles'][0]['pivot_bars']),5)
        self.assertTrue(f['target_obstacles'][0]['atr_source_refs'])
        self.assertEqual(f['target_obstacles'][0]['obstacle_price'],f['target_obstacles'][0]['upper'])

    def test_retest_invalidated_and_30minute_exclusive_next_bar(self):
        df=data(25);df.loc[21,['open','high','low','close']]=[99.,100.,98.9,99.]
        df.loc[22,['open','high','low','close']]=[99.,102.,98.8,101.]
        start=BASE+21*900
        rows=[dict(time=start-300,open=102.,high=103.,low=101.,close=102.)]
        for i in range(13):rows.append(dict(time=start+i*300,open=101.,high=102.,low=100.5,close=101.))
        rows[7].update(open=99.,high=100.,low=98.,close=98.)
        rows[-1].update(open=100.,high=101.,low=99.3,close=100.)
        bars=pd.DataFrame(rows)
        f=region_facts('retest',df,bars,m5_clock(bars),0,'buy',P)
        self.assertFalse(f['closed_trigger']['confirmed'])
        # First retouch after elapsed1800 is too late, including own breakout wick.
        rows[7].update(open=101.,high=102.,low=100.5,close=101.)
        rows.append(dict(time=start+13*300,open=100.,high=101.,low=99.3,close=100.))
        rows[-2].update(open=101.,high=102.,low=100.5,close=101.)
        late=pd.DataFrame(rows)
        self.assertFalse(region_facts('retest',df,late,m5_clock(late),0,'buy',P)['closed_trigger']['confirmed'])

    def test_preconfirmation_missing_even_when_touch_latest(self):
        df=data(21);bars=m5_after(df,1,{0:dict(low=98.6,close=100,open=100,high=101)})
        f=region_facts('rejection',df,bars.iloc[2:],m5_clock(bars),0,'buy',P)
        self.assertFalse(f['closed_trigger']['confirmed'])
        self.assertTrue(any('coverage' in b for b in f['blockers']))

    def test_explicit_offset_same_utc_and_stale_m15_rejected(self):
        df=data(21);bars=m5_after(df,1,{0:dict(low=98.6,close=100,open=100,high=101)})
        f=region_facts('rejection',df,bars,m5_clock(bars,offset=2),2,'buy',P)
        self.assertTrue(f['closed_trigger']['confirmed'],f)
        self.assertEqual(f['entry_event']['trigger_close_utc'],int(bars.iloc[-1]['time'])+300-7200)
        many=m5_after(df,4,{3:dict(low=98.6,close=100,open=100,high=101)})
        stale=region_facts('rejection',df,many,m5_clock(many),0,'buy',P)
        self.assertIn('missing latest closed M15 context',stale['blockers'])

    def test_deterministic_selection_and_common_touch_key(self):
        from unittest.mock import patch
        from copy import deepcopy
        df=data(21);bars=m5_after(df,1,{0:dict(low=98.6,close=100,open=100,high=101)})
        source=confirmed_regions(df,clock(df),0,P)
        second=deepcopy(source['regions'][0]);second['region_id']='support/second/confirmed'
        source['regions'].append(second)
        with patch('strategies.regions.confirmed_regions',return_value=source):
            f=region_facts('rejection',df,bars,m5_clock(bars),0,'buy',P)
        self.assertEqual(f['selection_order'],sorted(f['selection_order']))
        self.assertEqual(f['event_id'],f"region/{f['region']['region_id']}/touch/{int(bars.iloc[-1]['time'])}/buy")
        self.assertEqual(len(f['protection_bars']),3)
        self.assertEqual(f['entry_event']['event_id'],f['event_id'])

    def test_window_and_region_expiry_cap_event_ttl(self):
        from unittest.mock import patch
        from copy import deepcopy
        df=data(21);bars=m5_after(df,1,{0:dict(low=98.6,close=100,open=100,high=101)})
        now=m5_clock(bars);close=now.timestamp()
        window={'start_utc':close-1000,'end_utc':close+90,'members':[]}
        with patch('strategies.regions.active_window',return_value=window):
            f=region_facts('rejection',df,bars,now,0,'buy',P)
        self.assertEqual(f['entry_event']['valid_until_utc'],close+90)
        source=deepcopy(confirmed_regions(df,clock(df),0,P));source['regions'][0]['expiry_utc']=close+45
        with patch('strategies.regions.confirmed_regions',return_value=source):
            g=region_facts('rejection',df,bars,now,0,'buy',P)
        self.assertEqual(g['entry_event']['valid_until_utc'],close+45)

    def test_overlapping_opposing_zone_kept_with_first_edge(self):
        from unittest.mock import patch
        from copy import deepcopy
        df=data(21);bars=m5_after(df,1,{0:dict(low=98.6,close=100,open=100,high=101)})
        source=confirmed_regions(df,clock(df),0,P)
        opposing=deepcopy(source['regions'][0]);opposing.update(region_id='resistance/overlap/confirmed',
            type='resistance',center=99.9,lower=99.7,upper=100.1)
        source['regions'].append(opposing)
        with patch('strategies.regions.confirmed_regions',return_value=source):
            f=region_facts('rejection',df,bars,m5_clock(bars),0,'buy',P)
        self.assertEqual(len(f['target_obstacles']),1)
        self.assertEqual(f['target_obstacles'][0]['obstacle_price'],99.7)
        self.assertLess(f['target_obstacles'][0]['obstacle_price'],f['trigger_bar']['close'])

    def test_disabled_contract_and_baseline_registry(self):
        for s in [STRATEGY,REJECTION]:
            self.assertFalse(s.enabled);self.assertFalse(s.m5_boost)
            self.assertEqual(s.stop_reference,'region');self.assertEqual(s.target_r,2.5)
            self.assertEqual(s.max_hold_minutes,120);self.assertEqual(s.cooldown_seconds,600)
            Registry.validate(s)
        snap=Registry('strategies/definitions').snapshot()
        self.assertFalse(snap.error,snap.error)
        self.assertEqual([s.id for s in snap.active('london')],['session_retest'])

if __name__=='__main__':unittest.main()

