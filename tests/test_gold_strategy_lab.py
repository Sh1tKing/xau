"""Execution and causality checks for the separate offline experiment engine."""
import unittest

import numpy as np
import pandas as pd

from docs.experiments import gold_strategy_lab as lab


def rates(rows):
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "spread_abs"])


def signal(time=60, side=1, stop=98, target=106, hold=180, trail=False):
    return lab.Signal(time, "test", side, stop, target, 1.0, hold, 100, trail)


class GoldStrategyLabTests(unittest.TestCase):
    def test_same_minute_stop_target_is_stop(self):
        d = rates([[60, 100, 107, 97, 104, 0.2]])
        result, t = lab.simulate(d, [signal()], lab.Costs("test", slippage=0, rollover_fee=0))
        self.assertEqual(t[0]["reason"], "sl")
        self.assertAlmostEqual(t[0]["pnl"], -2.2)
        self.assertEqual(result["trades"], 1)

    def test_sell_stop_trigger_uses_ask(self):
        d = rates([[60, 100, 101.85, 99, 100, 0.2]])
        _, t = lab.simulate(d, [signal(side=-1, stop=102, target=94)],
                            lab.Costs("test", slippage=0, rollover_fee=0))
        self.assertEqual(t[0]["reason"], "sl")
        self.assertAlmostEqual(t[0]["pnl"], -2.0)

    def test_gap_loss_is_not_limited_to_stop_price(self):
        d = rates([[60, 100, 100.5, 99.5, 100, 0.2], [120, 96, 97, 95, 96, 0.2]])
        _, t = lab.simulate(d, [signal()], lab.Costs("test", slippage=0.1, rollover_fee=0))
        self.assertEqual(t[0]["reason"], "gap_sl")
        self.assertAlmostEqual(t[0]["exit"], 95.9)
        self.assertGreater(-t[0]["pnl"], t[0]["risk_dollars"])

    def test_fill_checks_actual_risk_cap_without_moving_stop(self):
        p, reason = lab.validate_fill(signal(stop=92, target=124), 100, .2, lab.Costs("x"), 8)
        self.assertIsNone(p)
        self.assertEqual(reason, "fill_stop_cap")
        p, reason = lab.validate_fill(signal(stop=92, target=124), 100, .2, lab.Costs("x"), 16)
        self.assertIsNone(reason)
        self.assertEqual(p["stop"], 92)

    def test_expired_quote_cannot_reuse_immediate_fill(self):
        d = rates([[60, 100, 101, 99, 100, .2], [120, 105, 105.5, 104, 105, .2]])
        result, t = lab.simulate(d, [signal()], lab.Costs("delay", delay_seconds=60))
        self.assertEqual(t, [])
        self.assertEqual(result["rejections"]["fill_rr"], 1)

    def test_no_fill_over_missing_minute(self):
        d = rates([[120, 100, 101, 99, 100, .2]])
        result, t = lab.simulate(d, [signal()], lab.Costs("test"))
        self.assertEqual(t, [])
        self.assertEqual(result["rejections"]["missing_fill_minute"], 1)

    def test_only_closed_higher_frame_is_visible(self):
        frame = pd.DataFrame({"close_time": [3600, 7200], "value": [1, 999]})
        c = lab.closed_context(frame, np.array([3599, 3600, 7199, 7200]), ["value"], "h_")
        self.assertTrue(np.isnan(c.h_value.iloc[0]))
        self.assertEqual(c.h_value.tolist()[1:], [1, 1, 999])

    def test_features_do_not_change_when_future_prices_change(self):
        d = rates([[i * 300, 100+i, 101+i, 99+i, 100.5+i, .2] for i in range(60)])
        d["spread"] = 20
        before = lab.features(d, 300)
        d.loc[40:, ["open", "high", "low", "close"]] += 10000
        after = lab.features(d, 300)
        pd.testing.assert_frame_equal(before.iloc[:40], after.iloc[:40])
        self.assertEqual(before.high12.iloc[20], d.high.iloc[8:20].max())

    def test_time_exit_does_not_use_future_minute_extrema(self):
        d = rates([[60, 100, 101, 99, 100, .2], [120, 100.5, 110, 95, 100, .2]])
        _, t = lab.simulate(d, [signal(hold=1)], lab.Costs("test", slippage=0, rollover_fee=0))
        self.assertEqual(t[0]["reason"], "time_exit")
        self.assertAlmostEqual(t[0]["exit"], 100.5)

    def test_cash_exhaustion_stops_account(self):
        # Gaps can exceed initial protection. Exhaustion must stop, not reset cash.
        d = rates([[60, 100, 101, 99, 100, .2], [120, 0, 1, 0, .5, .2],
                   [900, 100, 101, 99, 100, .2]])
        result, t = lab.simulate(d, [signal(), signal(900)], lab.Costs("test", slippage=0, rollover_fee=0))
        self.assertTrue(result["ruined"])
        self.assertEqual(len(t), 1)

    def test_outward_rounding_does_not_shrink_structural_stop(self):
        self.assertEqual(lab.outward(1, 98.1375), 98.13)
        self.assertEqual(lab.outward(-1, 101.8625), 101.87)

    def test_tp_bar_adverse_excursion_is_in_drawdown(self):
        d = rates([[60, 100, 107, 98.5, 104, .2]])
        result, t = lab.simulate(d, [signal()], lab.Costs("test", slippage=0, rollover_fee=0))
        self.assertEqual(t[0]["reason"], "tp")
        self.assertAlmostEqual(result["min_equity"], 98.3)
        self.assertAlmostEqual(result["max_drawdown_dollars"], 1.7)

    def test_larger_cash_profile_keeps_fixed_lot(self):
        d = rates([[60, 100, 107, 97, 104, .2]])
        a, ta = lab.simulate(d, [signal()], lab.Costs("test", slippage=0), initial_balance=100)
        b, tb = lab.simulate(d, [signal()], lab.Costs("test", slippage=0), initial_balance=1000)
        self.assertEqual(ta, tb)
        self.assertEqual(a["net_pnl"], b["net_pnl"])
        self.assertAlmostEqual(a["net_return_pct"], b["net_return_pct"] * 10)

    def test_complete_signal_generation_is_prefix_invariant(self):
        rng = np.random.default_rng(903)
        start = lab.epoch("2026-09-20T00:00:00Z")
        count = 12 * 1440
        close = 4000 + np.cumsum(rng.normal(.005, .30, count))
        opens = np.concatenate([[4000], close[:-1]])
        m1 = pd.DataFrame({"time": start + np.arange(count) * 60, "open": opens,
                           "high": np.maximum(opens, close) + rng.uniform(.01, .20, count),
                           "low": np.minimum(opens, close) - rng.uniform(.01, .20, count),
                           "close": close, "spread_abs": .15})
        full = {"M1": m1}
        for name, seconds in lab.PERIODS.items():
            if name == "M1":
                continue
            full[name] = m1.groupby(m1.time // seconds).agg(
                time=("time", "first"), open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"), spread_abs=("spread_abs", "last")).reset_index(drop=True)
        cutoff = start + 8 * 86400
        prefix = {k: v[v.time + lab.PERIODS[k] <= cutoff].reset_index(drop=True) for k, v in full.items()}
        a = lab.generate_signals(full)
        b = lab.generate_signals(prefix)
        self.assertGreater(sum(map(len, b.values())), 0)
        self.assertEqual({k: [s for s in v if s.time <= cutoff] for k, v in a.items()}, b)


if __name__ == "__main__":
    unittest.main()
