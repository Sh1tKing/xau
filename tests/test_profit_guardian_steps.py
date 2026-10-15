"""Small cash floors leave room instead of waiting for a full M5 ATR profit."""
import copy
import json
import logging
import unittest
from unittest.mock import patch

from strategies.profit_guard import ProfitFloor, policy
import test_profit_guardian as fixtures
base = fixtures.base


class SteppedProfitMathTests(unittest.TestCase):
    def guard(self, direction="BUY", **changes):
        return ProfitFloor(policy(**changes), 94. if direction == "BUY" else 106., 100.)

    def promote(self, guard, net, direction="BUY", now=100., **changes):
        sign = 1 if direction == "BUY" else -1
        values = dict(direction=direction, entry=100., risk=6., price=100.+sign*(net+.10),
                      atr=8.32, spread=.33, broker_gap=.21, cash_per_price=1., costs=.10)
        values.update(changes)
        for offset in (0., .5, 1.):
            result = guard.observe(now=now+offset, **values)
        return result

    def test_logged_five_dollar_givebacks_lock_small_amount_on_both_sides(self):
        for direction in ("BUY", "SELL"):
            for net, room in ((4.90, 5.895), (4.60, 8.322857142857)):
                with self.subTest(direction=direction, net=net):
                    guard = self.guard(direction)
                    target = self.promote(guard, net, direction, atr=room)
                    self.assertEqual(guard.rules["version"], "profit-floor-v3")
                    self.assertAlmostEqual(guard.net_at_stop(direction, 100., target), 1.)
                    self.assertAlmostEqual(abs(guard.confirmed_price-target), net-1.)
                    self.assertEqual(guard.atr_floor, room)

    def test_three_dollar_profit_keeps_most_of_its_pullback_room(self):
        for direction in ("BUY", "SELL"):
            guard = self.guard(direction)
            target = self.promote(guard, 3., direction)
            self.assertAlmostEqual(guard.net_at_stop(direction,100.,target), .20)
            self.assertAlmostEqual(abs(guard.confirmed_price-target),2.80)

    def test_floor_moves_by_cash_steps_and_does_not_chase_between_them(self):
        for direction in ("BUY", "SELL"):
            guard = self.guard(direction)
            sign = 1 if direction == "BUY" else -1
            for i, (net, floor) in enumerate(((2.,.2),(2.8,.2),(3.9,.2),(4.,1.),
                    (5.9,1.),(6.,2.),(9.9,2.),(10.,4.),(14.9,4.),(15.,6.))):
                with self.subTest(direction=direction, net=net):
                    target = self.promote(guard,net,direction,now=100.+i*2,atr=20.)
                    self.assertAlmostEqual(guard.net_at_stop(direction,100.,target),floor)
                    self.assertAlmostEqual(abs(100.+sign*(net+.10)-target),net-floor)
                    guard.acknowledge(direction,target)

    def test_saved_v2_still_reduces_loss_without_new_cash_exception(self):
        guard = self.guard(version="profit-floor-v2")
        target = self.promote(guard,4.60)
        self.assertAlmostEqual(guard.net_at_stop("BUY",100.,target),4.60-8.32)
        self.assertNotIn("cash_floors",guard.rules)
        restored=ProfitFloor(**json.loads(json.dumps(guard.export())))
        self.assertEqual(restored.rules,guard.rules)

    def test_default_policy_is_strict_and_rejects_bad_cash_steps(self):
        for changes in ({"cash_floors":[[2.,2.]]},{"cash_floors":[[4.,.5],[2.,.2]]},
                        {"cash_floors":[[2.,.5],[4.,.2]]},{"floor_room_r":0.}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                policy(**changes)

    def test_unconfirmed_spike_cannot_activate_the_next_cash_step(self):
        guard=self.guard()
        values=dict(direction="BUY",entry=100.,risk=6.,atr=8.32,spread=.33,broker_gap=.21,
                    cash_per_price=1.,costs=.1)
        guard.observe(price=102.2,now=100.,**values)
        guard.observe(price=110.,now=100.5,**values)
        target=guard.observe(price=102.3,now=101.,**values)
        self.assertAlmostEqual(guard.net_at_stop("BUY",100.,target),.2)
        self.assertAlmostEqual(guard.confirmed_price,102.2)

    def test_costs_spread_and_original_risk_can_defer_floor_without_clamping(self):
        for changes in (dict(spread=1.),dict(risk=12.)):
            guard=self.guard()
            target=self.promote(guard,2.,**changes)
            self.assertTrue(target is None or guard.net_at_stop("BUY",100.,target)<0.)
            self.assertGreater(guard.atr_floor,8.)

    def test_fees_and_cash_scale_are_included_and_floor_grid_is_not_rounded_below_net(self):
        for direction in ("BUY","SELL"):
            sign=1 if direction=="BUY" else -1
            guard=self.guard(direction)
            price=100.+sign*2.275
            target=self.promote(guard,4.,direction,price=price,risk=4.,cash_per_price=2.,costs=.55,tick_size=.25)
            self.assertGreaterEqual(guard.net_at_stop(direction,100.,target),1.-1e-8)
            self.assertGreaterEqual(abs(price-target),guard.floor_room(4.,.33,.21))
            self.assertAlmostEqual(target% .25,0.)

    def test_restart_keeps_frozen_steps_and_discards_unfinished_confirmation(self):
        old_steps=[[2.,.20],[4.,.50],[6.,1.],[10.,2.],[15.,4.]]
        guard=self.guard(cash_floors=old_steps)
        target=self.promote(guard,4.6)
        self.assertAlmostEqual(guard.net_at_stop("BUY",100.,target),.5)
        guard.acknowledge("BUY",target)
        restored=ProfitFloor(**json.loads(json.dumps(guard.export())))
        self.assertEqual(restored.rules,guard.rules)
        self.assertEqual(restored.rules["cash_floors"],old_steps)
        self.assertNotEqual(restored.rules["cash_floors"],policy()["cash_floors"])
        self.assertEqual(restored.protected_sl,target)
        self.assertEqual(restored.pending_since,0.)
        self.assertEqual(self.promote(restored,4.8,now=105.),target)

    def test_first_feasible_grid_price_still_gets_sustained_confirmation(self):
        guard=self.guard()
        self.promote(guard,2.2,risk=8.,costs=.153,price=102.353,tick_size=.25)
        self.assertLess(guard.net_at_stop("BUY",100.,guard.desired_sl),0.)
        target=self.promote(guard,2.347,now=102.,risk=8.,costs=.153,price=102.5,tick_size=.25)
        self.assertEqual(target,100.5)
        self.assertAlmostEqual(guard.confirmed_price,102.5)

    def test_already_confirmed_floor_can_submit_when_spread_normalizes_at_same_price(self):
        guard=self.guard()
        self.promote(guard,3.,spread=1.1)
        self.assertTrue(not guard.desired_sl or guard.net_at_stop("BUY",100.,guard.desired_sl)<0.)
        target=self.promote(guard,3.,now=103.,spread=.33)
        self.assertAlmostEqual(guard.net_at_stop("BUY",100.,target),.2)
        self.assertEqual(guard.atr_floor,8.32)


class SteppedProfitExecutionTests(unittest.TestCase):
    def setUp(self):
        fixtures.ProfitExecutionTests.setUp(self)
        old_level=logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable,old_level)
        for scope in (patch.object(base.logger,"handlers",[]),patch.object(base.logger,"propagate",False)):
            scope.start();self.addCleanup(scope.stop)
        self.trader.guardian_states.clear()
        self.trader.guard_store.put(123,None)
        self.state=self.trader.get_or_create_guardian_state(self.position,self.tick,frozen=policy(),
            original_sl=96.,original_tp=112.)
        self.atr=8.32

    quote=fixtures.ProfitExecutionTests.quote
    poll=fixtures.ProfitExecutionTests.poll
    lock=fixtures.ProfitExecutionTests.lock

    def test_real_last_trade_floor_executes_before_full_m5_space_is_affordable(self):
        self.position.price_open=4153.42
        self.position.sl,self.position.tp=4148.27,4167.90
        self.trader.guardian_states.clear();self.trader.guard_store.put(123,None)
        self.quote(4153.42)
        self.state=self.trader.get_or_create_guardian_state(self.position,self.tick,frozen=policy(),
            original_sl=4148.27,original_tp=4167.90)
        self.atr=5.895
        self.lock(4156.52)  # net +3; not a forecast of what the actual tick path filled.
        self.assertAlmostEqual(self.position.sl,4153.72)
        self.assertAlmostEqual(self.state.profit_floor.net_at_stop("BUY",4153.42,self.position.sl),.2)
        self.assertAlmostEqual(self.tick.bid-self.position.sl,2.8)
        self.assertEqual(self.position.tp,4167.90)
        self.lock(4158.42)
        self.assertAlmostEqual(self.position.sl,4154.52)
        self.assertEqual(self.state.initial_sl,4148.27)

    def test_same_tier_or_atr_drop_does_not_submit_extra_updates(self):
        self.lock(103.1)
        first=self.position.sl
        count=len(self.requests)
        self.atr=.1
        self.lock(103.8)
        self.assertEqual(self.position.sl,first)
        self.assertEqual(len(self.requests),count)
        self.assertEqual(self.state.profit_floor.atr_floor,8.32)

    def test_failure_is_not_confirmed_and_restart_can_retry_at_same_quote(self):
        self.retcode=10016
        self.lock(103.1)
        self.assertEqual(self.position.sl,96.)
        self.assertEqual(self.state.profit_floor.protected_sl,96.)
        self.assertAlmostEqual(self.state.profit_floor.desired_sl,100.3)
        self.trader.guardian_states.clear()
        self.trader.guard_store=fixtures.GuardStore(self.root/"guard.json")
        self.retcode=base.mt5.TRADE_RETCODE_DONE
        self.poll(103.1)
        self.assertAlmostEqual(self.position.sl,100.3)

    def test_pending_floor_cannot_use_current_price_or_costs_that_no_longer_fit(self):
        self.retcode=10016;self.lock(103.1)
        self.retcode=base.mt5.TRADE_RETCODE_DONE
        before=len(self.requests)
        self.poll(100.8)
        self.assertEqual(len(self.requests),before)
        self.position.swap=-4.
        self.poll(103.1)
        self.assertEqual(len(self.requests),before)
        self.assertEqual(self.position.sl,96.)

    def test_a_too_tight_ai_suggestion_cannot_borrow_the_floor_exception(self):
        with self.assertLogs(base.logger,level="INFO") as logs:
            self.lock(103.1)
        self.assertAlmostEqual(self.position.sl,100.3,msg="\n".join(logs.output))
        self.assertFalse(self.trader.modify_position_sl(self.position,102.,"AI-3 tighten"))
        self.assertAlmostEqual(self.position.sl,100.3)

    def test_memory_only_step_target_does_not_replace_durable_confirmation(self):
        with patch.object(self.trader.guard_store,"put",return_value=False):
            self.lock(103.1)
        self.assertFalse(self.trader.modify_position_sl(self.position,100.3,"local profit floor"))
        self.assertFalse(self.requests)

    def test_sell_grid_and_external_stronger_protection_never_reverse(self):
        self.position.type=base.mt5.POSITION_TYPE_SELL
        self.position.sl,self.position.tp=104.,88.
        self.trader.guardian_states.clear();self.trader.guard_store.put(123,None)
        self.state=self.trader.get_or_create_guardian_state(self.position,self.tick,frozen=policy(),
            original_sl=104.,original_tp=88.)
        self.info.trade_tick_size=.25
        self.lock(96.9)
        self.assertEqual(self.position.sl,99.5)
        self.position.sl=99.
        self.poll(97.)
        self.assertEqual(self.state.profit_floor.protected_sl,99.)
        self.position.sl=0.
        self.poll(97.)
        self.assertEqual(self.position.sl,99.)

    def test_snapshot_exposes_full_atr_and_exact_cash_floor_separately(self):
        self.lock(103.1)
        pack=json.loads(self.trader.collect_ai3_snapshot()["prompt"].split("\n",1)[1])
        guard=pack["positions"][0]["profit_guard"]
        self.assertEqual(guard["minimum_room"],8.32)
        self.assertAlmostEqual(guard["stepped_floor"]["stop"],100.3)
        self.assertEqual(guard["stepped_floor"]["minimum_room"],1.)
        self.assertEqual(guard["protected_phase"],"profit_lock")


if __name__ == "__main__":
    unittest.main()
