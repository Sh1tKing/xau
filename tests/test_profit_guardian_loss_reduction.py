"""Confirmed loss reduction must preserve noise room, old contracts and execution gates."""
import copy
import json
import logging
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_profit_guardian as fixtures
from strategies.profit_guard import ProfitFloor, GuardStore
policy = fixtures.policy

base, ai = fixtures.base, fixtures.ai


class LossReductionMathTests(unittest.TestCase):
    guard = fixtures.ProfitMathTests.guard
    observe = fixtures.ProfitMathTests.observe
    promote = fixtures.ProfitMathTests.promote

    def test_new_policy_reduces_loss_on_both_sides_before_positive_lock(self):
        for direction, price, expected in (("BUY",101.,99.),("SELL",99.,101.)):
            with self.subTest(direction=direction):
                guard = self.guard(direction)
                self.assertEqual(guard.rules["version"],"profit-floor-v2")
                self.assertAlmostEqual(self.promote(guard,price,direction),expected)
                self.assertAlmostEqual(abs(price-expected),2.)
                self.assertLess(guard.net_at_stop(direction,100.,expected),0.)

    def test_loss_and_fee_adjusted_zero_cannot_start_reduction(self):
        for direction, prices in (("BUY",(99.,100.,100.5)),("SELL",(101.,100.,99.5))):
            for price in prices:
                with self.subTest(direction=direction,price=price):
                    self.assertIsNone(self.promote(self.guard(direction),price,direction,costs=.5))

    def test_spike_and_retracement_do_not_bypass_confirmation(self):
        guard = self.guard()
        self.assertIsNone(self.observe(guard,101.,100.))
        self.assertIsNone(self.observe(guard,108.,100.5))
        self.assertAlmostEqual(self.observe(guard,101.5,101.),99.)
        guard.acknowledge("BUY",99.)
        self.assertEqual(self.promote(guard,100.8,now=102.),99.)
        self.assertEqual(guard.confirmed_price,101.)

    def test_stage_progression_is_loss_reduction_then_breakeven_then_profit(self):
        for direction, prices in (("BUY",(101.,102.2,103.)),("SELL",(99.,97.8,97.))):
            guard = self.guard(direction)
            initial = guard.protected_sl
            for index, (price,phase) in enumerate(zip(prices,("loss_reduction","breakeven","profit_lock"))):
                target = self.promote(guard,price,direction,now=100.+index*2)
                guard.acknowledge(direction,target)
                facts = guard.protection_details(direction,100.,initial)
                self.assertEqual(facts["protected_phase"],phase)
                if phase == "breakeven":
                    self.assertAlmostEqual(facts["protected_estimated_net"],0.)

    def test_no_candidate_wider_than_original_or_confirmed_floor(self):
        guard = self.guard()
        self.assertIsNone(self.promote(guard,101.,atr=8.))
        self.assertEqual(guard.protected_sl,96.)
        guard = self.guard()
        first = self.promote(guard,101.)
        guard.acknowledge("BUY",first)
        self.assertEqual(self.promote(guard,101.5,now=102.,atr=8.),first)
        self.assertEqual(guard.protected_sl,first)

    def test_rounding_and_changed_cash_scale_use_net_costs(self):
        guard = self.guard()
        self.assertAlmostEqual(self.promote(guard,101.,cash_per_price=2.,costs=1.5),99.)
        self.assertAlmostEqual(guard.net_at_stop("BUY",100.,99.),-3.5)
        guard = self.guard()
        self.assertIsNone(self.promote(guard,101.,cash_per_price=.5,costs=.6))

    def test_positive_lock_also_reserves_room_against_weakest_confirmed_quote(self):
        guard = self.guard()
        self.observe(guard,102.4,100.)
        self.observe(guard,108.,100.5)
        target=self.observe(guard,104.,101.)
        self.assertAlmostEqual(target,100.4)
        self.assertAlmostEqual(guard.confirmed_price-target,2.)

    def test_old_v1_still_waits_for_positive_lock_after_restart(self):
        for direction,price in (("BUY",101.),("SELL",99.)):
            guard = self.guard(direction,version="profit-floor-v1")
            restored = ProfitFloor(**json.loads(json.dumps(guard.export())))
            self.assertIsNone(self.promote(restored,price,direction))
            self.assertEqual(restored.rules["version"],"profit-floor-v1")

    def test_original_trade_reduces_loss_at_one_dollar_without_shrinking_atr(self):
        guard = ProfitFloor(policy(),4100.63,4093.56)
        values = dict(direction="SELL",entry=4093.56,risk=7.07,atr=6.867142857142783,
                      spread=.33,broker_gap=.01,cash_per_price=1.,costs=.1)
        for now in (100.,100.5,101.):
            target = guard.observe(price=4092.56,now=now,**values)
        self.assertAlmostEqual(target,4099.427142857143)
        self.assertAlmostEqual(guard.room(values["atr"],.33,.01),values["atr"])
        guard.acknowledge("SELL",target)
        for now in (102.,102.5,103.):
            target = guard.observe(price=4085.99,now=now,**values)
        self.assertAlmostEqual(target,4092.857142857143)


class LossReductionExecutionTests(unittest.TestCase):
    def setUp(self):
        fixtures.ProfitExecutionTests.setUp(self)
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable,previous)
        for scope in (patch.object(base.logger,"handlers",[]),patch.object(base.logger,"propagate",False)):
            scope.start()
            self.addCleanup(scope.stop)
    sell = fixtures.ProfitExecutionTests.sell
    quote = fixtures.ProfitExecutionTests.quote
    poll = fixtures.ProfitExecutionTests.poll
    lock = fixtures.ProfitExecutionTests.lock

    def test_confirmed_reduction_submits_negative_net_sl_and_keeps_tp(self):
        for sell in (False,True):
            if sell:self.sell()
            tp = self.position.tp
            with self.assertLogs(base.logger,level="INFO") as logs:
                self.lock(99. if sell else 101.)
            self.assertAlmostEqual(self.position.sl,101. if sell else 99.)
            self.assertEqual(self.position.tp,tp)
            self.assertEqual(self.state.profit_floor.protected_sl,self.position.sl)
            self.assertIn("减亏确认"," ".join(logs.output))
            self.assertNotIn("锁盈确认"," ".join(logs.output))

    def test_failed_negative_stop_is_intent_until_done_and_survives_restart(self):
        self.retcode = 10016
        self.lock(101.)
        self.assertEqual(self.position.sl,96.)
        self.assertEqual(self.state.profit_floor.desired_sl,99.)
        self.assertEqual(self.state.profit_floor.protected_sl,96.)
        restored = GuardStore(self.root/"guard.json")
        self.assertFalse(restored.error)
        self.trader.guardian_states.clear()
        self.trader.guard_store = restored
        self.retcode = base.mt5.TRADE_RETCODE_DONE
        self.poll(101.)
        self.state = self.trader.guardian_states[123]
        self.assertEqual(self.position.sl,99.)
        self.assertEqual(self.state.risk,4.)
        self.assertEqual(self.state.profit_floor.pending_since,0.)

    def test_lost_floating_profit_defers_pending_reduction(self):
        self.retcode = 10016
        self.lock(101.,)
        self.retcode = base.mt5.TRADE_RETCODE_DONE
        count = len(self.requests)
        self.poll(100.05)
        self.assertEqual(len(self.requests),count)
        self.assertEqual(self.position.sl,96.)

    def test_fresh_fee_loss_and_unknown_cash_block_pending_reduction(self):
        self.retcode=10016
        self.lock(101.)
        self.retcode=base.mt5.TRADE_RETCODE_DONE
        count=len(self.requests)
        self.position.swap=-1.1
        self.poll(101.)
        self.assertEqual(len(self.requests),count)
        self.position.swap=0.
        with patch.object(base.mt5,"order_calc_profit",return_value=None):
            self.poll(101.)
        self.assertEqual(self.position.sl,96.)

    def test_reason_string_cannot_create_unconfirmed_or_old_policy_reduction(self):
        self.quote(101.)
        self.assertFalse(self.trader.modify_position_sl(self.position,99.,"local loss reduction"))
        self.state.profit_floor = ProfitFloor(policy(version="profit-floor-v1"),96.,101.,
                                              desired_sl=99.,atr_floor=2.)
        self.assertFalse(self.trader.modify_position_sl(self.position,99.,"local loss reduction"))
        self.assertFalse(self.requests)

    def test_grid_rounds_reduction_outward_and_protected_line_never_recedes(self):
        self.info.trade_tick_size=.25
        self.lock(101.13)
        self.assertEqual(self.position.sl,99.)
        self.lock(100.9)
        self.assertEqual(self.position.sl,99.)
        self.info.trade_tick_size=.01
        self.position.sl=0.
        self.poll(101.)
        self.assertEqual(self.position.sl,99.)

    def test_old_json_container_can_hold_both_frozen_versions(self):
        legacy = copy.deepcopy(self.trader.guard_store.records["123"])
        legacy["profit_floor"]["rules"]["version"]="profit-floor-v1"
        self.root.joinpath("guard.json").write_text(json.dumps(
            {"version":"profit-floor-v1","positions":{"123":legacy}}),encoding="utf-8")
        store = GuardStore(self.root/"guard.json")
        self.assertFalse(store.error)
        legacy_rules=copy.deepcopy(store.records["123"]["profit_floor"]["rules"])
        second=copy.deepcopy(self.trader.guard_store.records["123"])
        second["identity"]["ticket"]=124
        second["identity"]["identifier"]=457
        self.assertTrue(store.put(124,second))
        loaded=GuardStore(self.root/"guard.json")
        self.assertEqual(loaded.records["123"]["profit_floor"]["rules"],legacy_rules)
        self.assertEqual(loaded.records["124"]["profit_floor"]["rules"]["version"],"profit-floor-v2")

    def test_new_entry_compatibility_alias_freezes_v3_but_saved_v1_is_unchanged(self):
        for mode in ("profit_floor_v1","profit_floor_v2","profit_floor_v3"):
            with patch.dict(os.environ,{"FIB_PA_GUARDIAN_POLICY":mode}):
                self.assertEqual(base.frozen_guardian_policy()["version"],"profit-floor-v3")
        with patch.dict(os.environ,{"FIB_PA_GUARDIAN_POLICY":"legacy"}):
            self.assertIsNone(base.frozen_guardian_policy())

    def test_ai3_snapshot_reports_reduction_separately_from_profit(self):
        self.lock(101.)
        payload=json.loads(self.trader.collect_ai3_snapshot()["prompt"].split("\n",1)[1])
        facts=payload["positions"][0]["profit_guard"]
        self.assertEqual(facts["protected_phase"],"loss_reduction")
        self.assertAlmostEqual(facts["protected_estimated_net"],-1.1)
        self.assertIn("减亏",ai.AI3_INSTRUCTIONS)
        self.assertIn("protected_estimated_net",ai.AI3_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
