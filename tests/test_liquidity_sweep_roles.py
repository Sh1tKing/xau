"""Expose pivot, sweep and confirmation separately without changing scoring."""
import unittest
import xauusd_fib_volume_pa_trader as base


class SweepRoleTests(unittest.TestCase):
    def frame(self):
        lows=[4180.+i*.1 for i in range(15)]+[4172.52,4176.,4177.,4177.5,4178.,4178.5]
        rows=[dict(time=1792144800+i*900,open=4190.,high=4200.,low=x,close=4191.,tick_volume=100.)
              for i,x in enumerate(lows)]
        rows += [dict(time=1792144800+21*900,open=4176.,high=4177.,low=4171.35,close=4174.33,tick_volume=100.),
                 dict(time=1792144800+22*900,open=4174.5,high=4176.,low=4173.,close=4174.,tick_volume=100.),
                 dict(time=1792144800+23*900,open=4174.33,high=4178.,low=4172.67,close=4176.2,tick_volume=100.)]
        return base.pd.DataFrame(rows)

    def test_buy_and_sell_sweeps_keep_original_result_and_label_actual_roles(self):
        for direction in ("buy","sell"):
            df=self.frame()
            if direction=="sell":
                old=df.copy()
                df["open"],df["close"]=8400-old.open,8400-old.close
                df["high"],df["low"]=8400-old.low,8400-old.high
            before=base.detect_liquidity_sweep(df,direction,3.)
            facts={}
            after=base.detect_liquidity_sweep(df,direction,3.,evidence=facts)
            self.assertEqual(before,after)
            self.assertTrue(after[0])
            self.assertEqual(facts["pivot"]["time"],int(df.iloc[15].time))
            self.assertEqual(facts["sweep"]["time"],int(df.iloc[21].time))
            self.assertEqual(facts["confirmation"]["time"],int(df.iloc[-1].time))
            self.assertNotEqual(facts["sweep"]["time"],facts["confirmation"]["time"])
            evidence={}
            score=base.classify_price_action(df,direction,3.)
            self.assertEqual(score,base.classify_price_action(df,direction,3.,evidence=evidence))
            self.assertEqual(evidence["liquidity_sweep_evidence"],facts)
            self.assertEqual(evidence["prior_extreme_role"],"false_break_reference")

    def test_unconfirmed_sweep_does_not_invent_event_roles(self):
        df=self.frame();df.loc[df.index[-1],"close"]=4173.
        facts={"stale":"old"}
        result=base.detect_liquidity_sweep(df,"buy",3.,evidence=facts)
        self.assertFalse(result[0])
        self.assertEqual(facts,{})


if __name__ == "__main__":
    unittest.main()
