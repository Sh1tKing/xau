import unittest
import pandas as pd
from strategies.patterns import patterns_facts


def frame(bodies):
    return pd.DataFrame([dict(time=1800+i*300,open=o,close=c,low=l,high=h) for i,(o,c,l,h) in enumerate(bodies)])


class PatternTests(unittest.TestCase):
    def test_body_not_wick(self):
        f=patterns_facts(frame([(11,10,8,13),(9,12,9,12)]),'M5')
        self.assertTrue(f['body_engulf_previous'])
        self.assertFalse(f['full_range_engulf_previous'])
        self.assertEqual(f['contiguous_opposite_bodies_covered'],1)
        self.assertEqual(f['first_in_trend'],'unknown')

    def test_wick_not_body(self):
        f=patterns_facts(frame([(12,10,9,13),(10.5,11.5,8,14)]),'M5')
        self.assertFalse(f['body_engulf_previous'])
        self.assertTrue(f['full_range_engulf_previous'])

    def test_full_range_fact_does_not_require_color(self):
        f=patterns_facts(frame([(10,11,9,12),(10.5,11.5,8,13)]),'M5')
        self.assertTrue(f['full_range_engulf_previous'])
        self.assertFalse(f['body_engulf_previous'])

    def test_doji_and_exact_ties(self):
        for rows in [[(10,10,9,11),(9,11,8,12)],[(11,10,9,12),(10,11,9,12)]]:
            f=patterns_facts(frame(rows),'M5')
            self.assertFalse(f['body_engulf_previous'])
        self.assertTrue(patterns_facts(frame([(11,10,9,12),(10,11,9,12)]),'M5')['equal_body'])

    def test_contiguous_count_stops_at_color(self):
        f=patterns_facts(frame([(11,10,9,12),(10,11,9,12),(12,11,10,13),(13,12,11,14),(9,15,8,16)]),'M5')
        self.assertEqual(f['contiguous_opposite_bodies_covered'],2)
        self.assertEqual(len(f['covered_bars']),2)

    def test_count_cap_and_prior_history(self):
        rows=[(12,11,10,13)]*6+[(10,14,9,15)]
        f=patterns_facts(frame(rows),'M5')
        self.assertEqual(f['contiguous_opposite_bodies_covered'],5)
        g=patterns_facts(frame([(11,10,9,12),(9,12,8,13),(12,11,10,13),(10,13,9,14)]),'M5')
        self.assertTrue(g['prior_in_covered_window'])
        self.assertEqual(g['past8_same_pattern_times'],[2100])

    def test_bad_and_relevant_gap_unavailable(self):
        df=frame([(11,10,9,12),(9,12,8,13)])
        for key,value in [('time',2400),('low',float('nan')),('high',8)]:
            broken=df.copy();broken.loc[1,key]=value
            self.assertFalse(patterns_facts(broken,'M5')['available'])

    def test_unrelated_old_gap_allowed(self):
        df=frame([(11,10,9,12)]*20+[(9,12,8,13)])
        df.loc[0,'time']=0
        self.assertTrue(patterns_facts(df,'M5')['available'])

    def test_optional_region_facts_no_signal(self):
        facts=patterns_facts(frame([(11,10,9,12),(9,12,8,13)]),'M5',
            [{'region_id':'x','center':11,'lower':10,'upper':12}])
        self.assertTrue(facts['region_facts'][0]['range_intersects'])
        self.assertNotIn('action',facts)

if __name__=='__main__':unittest.main()
