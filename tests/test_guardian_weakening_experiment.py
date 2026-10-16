"""Research-only temporal and BUY/SELL invariants; no terminal or AI imports."""
from dataclasses import replace
import unittest

from docs.experiments.replay_guardian_weakening import (
    Bar, accept_stop, rejection, stop_proposal, weakening)


def mirror(bar):
    return Bar(bar.time, 200-bar.open, 200-bar.low, 200-bar.high, 200-bar.close)


class WeakeningExperimentTests(unittest.TestCase):
    def setUp(self):
        self.m1 = [Bar(600, 100.7, 101.2, 100.6, 101.),
                   Bar(660, 101., 101.1, 99.5, 99.7),
                   Bar(720, 99.7, 99.9, 98.8, 99.1)]
        self.m5 = [Bar(0, 103., 105., 100.5, 104.),
                   Bar(300, 104., 104.5, 98.5, 100.)]

    def test_direction_mirror_has_same_weakening(self):
        self.assertTrue(weakening('buy',300,self.m1,self.m5,1.,780))
        self.assertTrue(weakening('sell',300,list(map(mirror,self.m1)),list(map(mirror,self.m5)),1.,780))

    def test_m1_must_be_closed_and_contiguous(self):
        self.assertFalse(weakening('buy',300,self.m1,self.m5,1.,779))
        broken=self.m1[:]
        broken[0]=replace(broken[0],time=540)
        self.assertFalse(weakening('buy',300,broken,self.m5,1.,780))

    def test_future_m5_and_pre_entry_m5_rejected(self):
        self.assertFalse(weakening('buy',300,self.m1,[replace(b,time=b.time+300) for b in self.m5],1.,780))
        self.assertFalse(weakening('buy',600,self.m1,self.m5,1.,780))

    def test_pre_entry_and_missing_m1_rejected(self):
        self.assertFalse(weakening('buy',601,self.m1,self.m5,1.,780))
        self.assertFalse(weakening('buy',300,self.m1[1:],self.m5,1.,780))

    def test_m5_boundary_touch_does_not_confirm_break(self):
        self.assertFalse(weakening('buy',300,self.m1,[self.m5[0],replace(self.m5[1],close=100.5)],1.,780))

    def test_favorable_rejection_prevents_automatic_signal(self):
        bars=self.m1[:-1]+[Bar(720,99.7,99.9,96.,99.1)]
        self.assertTrue(rejection(bars[-1],'buy',1.))
        self.assertFalse(weakening('buy',300,bars,self.m5,1.,780))
        self.assertFalse(weakening('sell',300,list(map(mirror,bars)),list(map(mirror,self.m5)),1.,780))

    def test_generated_stop_respects_full_room_and_mirror(self):
        buy=stop_proposal('buy',self.m1,99.1,2.,2.,.2,.01)
        sell=stop_proposal('sell',list(map(mirror,self.m1)),101.1,2.,2.,.2,.01)
        self.assertGreaterEqual(99.1-buy+1e-8,2.)
        self.assertGreaterEqual(sell-101.1+1e-8,2.)
        self.assertAlmostEqual(sell,200-buy+.2,places=8)

    def test_execution_rechecks_live_room_and_stronger_stop(self):
        self.assertTrue(accept_stop('buy',95.,97.,99.,2.,.2))
        self.assertFalse(accept_stop('buy',95.,97.,98.9,2.,.2))
        self.assertFalse(accept_stop('buy',97.1,97.,99.,2.,.2))
        self.assertTrue(accept_stop('sell',105.,103.,101.,2.,.2))
        self.assertFalse(accept_stop('sell',105.,103.,101.1,2.,.2))
        self.assertFalse(accept_stop('sell',102.9,103.,101.,2.,.2))


if __name__ == '__main__':
    unittest.main()
