"""Coarse replay policy and next-bar protection; no tick path or AI performance claims."""
import copy
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import xauusd_fib_pa_backtest as replay
from test_session_retest_backtest import market

base = replay.base


class ProfitReplayTests(unittest.TestCase):
    def control(self,side="buy",spread_multiplier=1.,guardian_policy="profit_floor_v1"):
        data,meta,stamp = market()
        test = replay.Backtest(data,meta,55,guardian_policy=guardian_policy,spread_multiplier=spread_multiplier)
        sl,tp = (96.,112.) if side == "buy" else (104.,88.)
        state = base.GuardianState(1,side.upper(),100.,sl,tp,4.,100.,100.,
            profit_floor=base.ProfitFloor(copy.deepcopy(test.guardian_policy),sl,100.))
        test.position = dict(side=side,state=state,sl=sl,tp=tp,open_ts=stamp,signal_ts=stamp,
            score=85,session="london",setup="control",strategy_id="control",setup_name="control",
            max_hold_minutes=90,event_id=None,cooldown_seconds=60)
        test.guardian_context = lambda stamp:(None,2.)
        return test

    def test_closed_quote_not_unordered_bar_high_drives_profit_stop(self):
        test = self.control()
        test.c[0],test.h[0],test.l[0] = 103.,111.,99.
        test.update_guardian(0)
        self.assertAlmostEqual(test.position["sl"],100.97)
        self.assertEqual(test.position["state"].highest_price,103.)
        self.assertFalse(test.trades)
        # Only next-bar low may trigger the stop established at this close.
        test.o[1],test.l[1],test.h[1] = 104.,100.5,106.
        self.assertTrue(test.check_exit(1))
        self.assertAlmostEqual(test.trades[-1]["exit"],100.97)
        self.assertAlmostEqual(test.trades[-1]["pnl"],.87)

    def test_sell_uses_ask_and_scaled_spread_without_double_charge(self):
        test = self.control("sell",spread_multiplier=2.)
        self.assertAlmostEqual(test.spreads[0],.4)
        test.c[0],test.l[0] = 96.6,90.
        test.update_guardian(0)
        self.assertAlmostEqual(test.position["sl"],99.03)
        self.assertAlmostEqual(test.position["state"].lowest_price,97.)

    def test_no_legacy_structural_exit_and_atr_drop_does_not_advance(self):
        test = self.control();test.c[0] = test.c[1] = 105.
        test.update_guardian(0)
        stop = test.position["sl"]
        test.guardian_context = lambda stamp:(None,.1)
        test.update_guardian(1)
        self.assertEqual(test.position["sl"],stop)
        with patch.object(base,"is_structure_failure") as structural:
            test.check_structure(test.t[1]+60)
            structural.assert_not_called()

    def test_future_price_changes_cannot_change_earlier_floor(self):
        left,right = self.control(),self.control()
        left.c[0] = right.c[0] = 103.
        right.c[1] = right.h[1] = 100000.
        left.update_guardian(0);right.update_guardian(0)
        self.assertEqual(left.position["sl"],right.position["sl"])

    def test_explicit_legacy_still_available_and_invalid_costs_refused(self):
        data,meta,_ = market()
        legacy = replay.Backtest(data,meta,55,guardian_policy="legacy")
        self.assertIsNone(legacy.guardian_policy)
        self.assertEqual(legacy.guard_fee,0.)
        with self.assertRaises(ValueError):
            replay.Backtest(data,{**meta,"guardian_fee_cash":float("nan")},55,guardian_policy="profit_floor_v1")

    def test_v1_remains_unchanged_and_v2_reduces_loss_on_next_bar(self):
        for side in ("buy","sell"):
            with self.subTest(side=side):
                old=self.control(side)
                new=self.control(side,guardian_policy="profit_floor_v2")
                price=101. if side=="buy" else 99.-new.spreads[0]
                old.c[0]=new.c[0]=price
                old.update_guardian(0);new.update_guardian(0)
                self.assertEqual(old.position["sl"],96. if side=="buy" else 104.)
                self.assertAlmostEqual(new.position["sl"],99. if side=="buy" else 101.)
                self.assertEqual(old.guardian_policy["version"],"profit-floor-v1")
                self.assertEqual(new.guardian_policy["version"],"profit-floor-v2")
                self.assertFalse(new.trades)
                new.o[1]=100.
                new.l[1],new.h[1]=98.,102.
                self.assertTrue(new.check_exit(1))
                self.assertAlmostEqual(new.trades[-1]["pnl"],-1.1)

    def test_reduction_still_can_be_loss_and_does_not_use_bar_spike(self):
        test=self.control(guardian_policy="profit_floor_v2")
        test.c[0],test.h[0]=101.,111.
        test.update_guardian(0)
        self.assertEqual(test.position["sl"],99.)
        test.guardian_context=lambda stamp:(None,.1)
        test.c[1]=101.
        test.update_guardian(1)
        self.assertEqual(test.position["sl"],99.)


if __name__ == "__main__":
    unittest.main()
