"""Pure research checks for the six alternative rules, without MT5 or HTTP."""
from dataclasses import replace
import unittest
from docs.experiments.replay_guardian_alternatives import (
    Bar,RULES,closed_sequence,m1_push,m5_soft,queued_close_status,signal)


def mirror(bar):
    return Bar(bar.time,200-bar.open,200-bar.low,200-bar.high,200-bar.close)


class AlternativesExperimentTests(unittest.TestCase):
    def setUp(self):
        self.args=dict(side='buy',opened=300,now=780,
            bars1=[Bar(600,100.7,101.2,100.6,101.),Bar(660,101.,101.1,99.5,99.7),Bar(720,99.7,99.9,98.8,99.1)],
            bars5=[Bar(0,103.,105.,100.5,104.),Bar(300,104.,104.5,98.5,100.)],
            first3=[Bar(300,103.,104.,100.,103.),Bar(360,103.,104.,100.5,102.),Bar(420,102.,103.,100.2,101.)],
            atr1=1.,atr5=2.,peak_r=.3,entry_atr=2.,price=99.1,entry=103.,net=-4.)

    def test_every_rule_is_symmetric(self):
        for name in RULES:
            args={**self.args,'peak_r':.1 if name=='failed_start_sl' else .3}
            other={**args,'side':'sell','bars1':list(map(mirror,args['bars1'])),
                   'bars5':list(map(mirror,args['bars5'])),'first3':list(map(mirror,args['first3'])),
                   'price':200-args['price'],'entry':200-args['entry']}
            with self.subTest(rule=name):
                self.assertTrue(signal(name,**args))
                self.assertTrue(signal(name,**other))

    def test_all_rules_pause_when_net_positive(self):
        for name in RULES:
            self.assertFalse(signal(name,**{**self.args,'net':.01}))

    def test_m1_rules_do_not_wait_for_m5_boundary(self):
        args={**self.args,'bars5':[]}
        self.assertTrue(signal('fast_m1_sl',**args))
        self.assertTrue(signal('fast_m1_exit',**args))
        self.assertFalse(signal('volatility_exit',**args))

    def test_m5_weakness_needs_no_extreme_break(self):
        q=replace(self.args['bars5'][-1],close=102.,low=101.)
        bars=[self.args['bars5'][0],q]
        self.assertGreater(q.close,bars[0].low)
        self.assertTrue(m5_soft('buy',300,bars,2.,780))
        self.assertFalse(m5_soft('buy',300,bars,2.,599))
        self.assertFalse(m5_soft('buy',300,bars,2.,900))

    def test_future_and_noncontiguous_m1_cannot_trigger(self):
        a=self.args
        self.assertFalse(m1_push('buy',300,a['bars1'],1.,779))
        bars=a['bars1'][:]
        bars[0]=replace(bars[0],time=540)
        self.assertFalse(m1_push('buy',300,bars,1.,780))
        self.assertFalse(closed_sequence(a['bars1'],60,601,780,3))

    def test_rejection_blocks_early_m1_exit(self):
        bars=self.args['bars1'][:-1]+[Bar(720,99.7,99.9,96.,99.1)]
        self.assertFalse(signal('fast_m1_exit',**{**self.args,'bars1':bars}))

    def test_failed_start_requires_price_failure_not_time(self):
        args={**self.args,'peak_r':.1}
        self.assertTrue(signal('failed_start_sl',**args))
        # After many minutes, a recovered initial range is still not a failure.
        narrow=[replace(b,low=98.) for b in args['first3']]
        self.assertFalse(signal('failed_start_sl',**{**args,'first3':narrow}))
        self.assertFalse(signal('failed_start_sl',**{**args,'peak_r':.25}))

    def test_failed_start_cannot_use_future_initial_range(self):
        args={**self.args,'peak_r':.1}
        future=args['first3'][:-1]+[replace(args['first3'][-1],time=900)]
        self.assertFalse(signal('failed_start_sl',**{**args,'first3':future}))

    def test_volatility_exit_uses_frozen_entry_atr(self):
        self.assertTrue(signal('volatility_exit',**self.args))
        self.assertFalse(signal('volatility_exit',**{**self.args,'entry_atr':3.}))
        self.assertFalse(signal('volatility_exit',**{**self.args,'entry_atr':0.}))

    def test_giveback_needs_prior_closed_progress(self):
        self.assertTrue(signal('giveback_exit',**self.args))
        self.assertFalse(signal('giveback_exit',**{**self.args,'peak_r':.249}))

    def test_existing_protection_precedes_queued_exit(self):
        self.assertEqual(queued_close_status('buy',94.,100.,95.,110.,1.,.1),'original_protection')
        self.assertEqual(queued_close_status('sell',106.,100.,105.,90.,1.,.1),'original_protection')
        self.assertEqual(queued_close_status('buy',111.,100.,95.,110.,1.,.1),'original_protection')
        self.assertEqual(queued_close_status('buy',99.,100.,95.,110.,1.,.1,True),'original_protection')

    def test_recovery_cancels_late_exit_with_cash_costs(self):
        self.assertEqual(queued_close_status('buy',100.2,100.,95.,110.,1.,.1),'recovered')
        self.assertEqual(queued_close_status('sell',99.8,100.,105.,90.,1.,.1),'recovered')
        self.assertEqual(queued_close_status('buy',100.05,100.,95.,110.,1.,.1),'execute')


if __name__=='__main__':
    unittest.main()
