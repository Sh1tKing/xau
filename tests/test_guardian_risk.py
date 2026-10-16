"""Closed-candle risk facts are advisory, symmetric and numerically explicit."""
import copy
import unittest

from strategies.guardian_risk import build_review, validate_stop

ORIGIN=1800000000

def bars(seconds, last, count=16):
    return [dict(time=last-i*seconds, open=100., high=101., low=99., close=100.)
            for i in range(count-1, -1, -1)]


class RiskFactsTests(unittest.TestCase):
    def setUp(self):
        self.m1=bars(60, ORIGIN+1020)
        self.m5=bars(300, ORIGIN+600)
        self.m5[-2].update(open=104., high=105., low=100., close=103.)
        self.m5[-1].update(open=103., high=104., low=99., close=100.)
        self.m1[-3].update(open=104., high=105., low=102., close=103.)
        self.m1[-2].update(open=103., high=104., low=100., close=101.)
        self.m1[-1].update(open=101., high=102., low=98., close=99.)
        self.args=dict(direction="BUY", entry_time=ORIGIN+700, entry=104., current_sl=90.,
            bid=99., ask=99.33, server_now=ORIGIN+1081., m1=self.m1, m5=self.m5,
            atr_floor=3., room_atr=1., room_spread=3., room_abs=.5,
            broker_gap=.02, tick_size=.01, cash_per_price=1., costs=.1)

    def build(self, **kw):
        return build_review(**{**self.args, **kw})

    def test_m1_warning_and_m5_review_have_no_automatic_exit(self):
        r=self.build()
        self.assertTrue(r["available"])
        self.assertTrue(r["m1_warning"])
        self.assertTrue(r["m5_weakening"])
        self.assertTrue(r["review_sl"])
        self.assertEqual(r["automatic_action"], "none")
        self.assertGreater(r["candidate_sl"],90.)
        self.assertLess(r["candidate_estimated_net"],0.)
        self.assertEqual(r["candidate_phase"],"loss_reduction")
        self.assertGreaterEqual(99.-r["candidate_sl"]+1e-9,3.)

    def test_m1_alone_does_not_request_m5_weakening_review(self):
        self.m5[-1].update(open=100., high=105., low=99., close=104.)
        r=self.build()
        self.assertTrue(r["m1_warning"])
        self.assertFalse(r["m5_weakening"])
        self.assertFalse(r["review_sl"])
        self.assertIsNone(r["candidate_sl"])

    def test_sell_mirrors_buy_and_includes_ask_side_and_spread(self):
        buy=self.build()
        def mirror(seq):
            return [dict(time=b["time"],open=200-b["open"],close=200-b["close"],
                         high=200-b["low"],low=200-b["high"]) for b in seq]
        sell=self.build(direction="SELL",entry=96.,current_sl=110.,bid=101.,ask=101.33,
                        m1=mirror(self.m1),m5=mirror(self.m5))
        self.assertTrue(sell["review_sl"])
        self.assertTrue(sell["m1_warning"])
        self.assertAlmostEqual(sell["candidate_sl"],200-buy["candidate_sl"]+.33)

    def test_favorable_m5_rejection_is_exposed_not_weakening(self):
        self.m5[-1].update(open=103.,high=104.,low=90.,close=102.)
        r=self.build()
        self.assertTrue(r["m5_favorable_rejection"])
        self.assertFalse(r["m5_weakening"])
        self.assertFalse(r["review_sl"])

    def test_m1_rejection_defers_reference_sl_even_with_soft_m5(self):
        self.m1[-1].update(open=100.,high=101.,low=90.,close=100.5)
        r=self.build()
        self.assertTrue(r["m5_weakening"])
        self.assertFalse(r["review_sl"])

    def test_price_comparison_does_not_call_below_above(self):
        self.m5[-2].update(open=4178.,high=4180.,low=4176.75,close=4178.)
        self.m5[-1].update(open=4178.,high=4179.,low=4175.,close=4175.90)
        r=self.build(bid=4175.76,ask=4176.09,entry=4177.02,current_sl=4170.30)
        relation=r["m5_close_vs_previous_extreme"]
        self.assertEqual((relation["close"],relation["level"],relation["relation"]),(4175.9,4176.75,"below"))

    def test_bad_missing_unclosed_gapped_or_stale_source_is_unknown(self):
        for mode in ("missing","future","gap","stale","ohlc","duplicate"):
            source=copy.deepcopy(self.m1)
            if mode=="missing":source=source[:3]
            elif mode=="future":source[-1]["time"]+=60
            elif mode=="gap":source[-3]["time"]-=60
            elif mode=="stale":
                for b in source:b["time"]-=60
            elif mode=="ohlc":source[-1]["close"]=999.
            else:source[-2]["time"]=source[-1]["time"]
            r=self.build(m1=source)
            self.assertFalse(r["available"],mode)
            self.assertIsNone(r["m5_weakening"],mode)
            self.assertIsNone(r["candidate_sl"],mode)

    def test_pre_entry_m5_is_not_a_new_weakening_event(self):
        r=self.build(entry_time=ORIGIN+901)
        self.assertFalse(r["m5_weakening"])
        self.assertFalse(r["review_sl"])

    def test_unknown_cash_does_not_invent_net_profit(self):
        r=self.build(cash_per_price=None,costs=None)
        self.assertIsNotNone(r["candidate_sl"])
        self.assertIsNone(r["candidate_estimated_net"])
        self.assertEqual(r["candidate_phase"],"net_unknown")

    def test_stronger_actual_sl_is_retained_and_no_atr_shrinking(self):
        r=self.build(current_sl=98.,atr_floor=5.)
        self.assertIsNone(r["candidate_sl"])
        self.assertGreaterEqual(r["minimum_room"],5.)

    def test_build_does_not_mutate_sources(self):
        old=copy.deepcopy(self.args)
        self.build()
        self.assertEqual(self.args,old)


if __name__ == "__main__":
    unittest.main()
