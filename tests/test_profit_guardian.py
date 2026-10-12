"""Profit preservation with executable quotes, durable floors and mocked MT5."""
import copy
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai
from strategies.profit_guard import ProfitFloor, GuardStore, policy, VERSION

base = ai.base


class ProfitMathTests(unittest.TestCase):
    def guard(self, direction="BUY", **overrides):
        return ProfitFloor(policy(**overrides), 96. if direction == "BUY" else 104., 100.)

    def observe(self, guard, price, now=100., direction="BUY", **changes):
        values = dict(direction=direction, entry=100., risk=4., price=price, atr=2.,
                      spread=.33, broker_gap=.21, cash_per_price=1., costs=.10, now=now)
        values.update(changes)
        return guard.observe(**values)

    def promote(self, guard, price, direction="BUY", now=100., **changes):
        self.observe(guard, price, now, direction, **changes)
        self.observe(guard, price, now+.5, direction, **changes)
        return self.observe(guard, price, now+1., direction, **changes)

    def test_small_profit_can_lock_without_one_r_gate(self):
        guard = self.guard()
        stop = self.promote(guard, 103.)
        self.assertLess(guard.peak_net/4., 1.)
        self.assertAlmostEqual(stop, 100.97)
        self.assertGreater(103.-stop, 2.)

    def test_sell_mirrors_buy_on_executable_ask(self):
        guard = self.guard("SELL")
        self.assertAlmostEqual(self.promote(guard, 97., "SELL"), 99.03)

    def test_losses_and_unaffordable_room_never_make_a_profit_stop(self):
        for price in (99.,100.1,101.,102.):
            self.assertIsNone(self.promote(self.guard(), price))

    def test_net_stages_increase_retention(self):
        guard = self.guard()
        self.assertAlmostEqual(self.promote(guard,103.),100.97)
        self.assertAlmostEqual(self.promote(guard,105.,now=102.),102.06)
        self.assertAlmostEqual(self.promote(guard,109.,now=104.),104.55)

    def test_retracement_cannot_undo_desired_or_acknowledged_floor(self):
        for direction, best, pullback in (("BUY",105.,103.),("SELL",95.,97.)):
            guard = self.guard(direction)
            stop = self.promote(guard,best,direction)
            guard.acknowledge(direction,stop)
            self.assertEqual(self.promote(guard,pullback,direction,now=102.),stop)
            self.assertEqual(guard.protected_sl,stop)

    def test_atr_fall_and_cost_change_without_new_price_do_not_raise_target(self):
        guard = self.guard()
        stop = self.promote(guard,105.)
        self.assertEqual(self.promote(guard,105.,now=102.,atr=.1,costs=0.),stop)
        self.assertEqual(guard.atr_floor,2.)

    def test_spike_is_not_confirmed_and_observation_gaps_reset_interval(self):
        guard = self.guard()
        self.assertIsNone(self.observe(guard,110.,100.))
        self.assertIsNone(self.observe(guard,100.1,100.5))
        self.assertIsNone(self.observe(guard,105.,101.))
        self.assertIsNone(self.observe(guard,105.,110.))
        self.assertEqual(guard.confirmed_price,100.)
        self.assertIsNotNone(self.observe(guard,105.,111.))

    def test_confirmation_uses_weakest_quote_not_intrasecond_best(self):
        guard = self.guard()
        self.observe(guard,103.,100.)
        self.observe(guard,110.,100.5)
        self.observe(guard,104.,101.)
        self.assertEqual(guard.confirmed_price,103.)
        self.assertAlmostEqual(guard.desired_sl,100.97)

    def test_fee_reserve_is_not_a_second_spread_charge(self):
        guard = self.guard()
        self.assertAlmostEqual(self.promote(guard,103.,spread=.2,costs=.5),101.)
        self.assertAlmostEqual(guard.peak_net,2.5)

    def test_broker_and_spread_constraints_can_defer_new_lock(self):
        for changes in (dict(broker_gap=4.),dict(spread=2.),dict(atr=8.)):
            self.assertIsNone(self.promote(self.guard(),103.,**changes))

    def test_invalid_facts_or_policy_never_produce_prices(self):
        for key in ("price","atr","cash_per_price","costs","now"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                changes = {key:float("nan")}
                self.observe(self.guard(),changes.pop("price",103.),**changes)
        for overrides in (dict(stages=[[1.,.3]]),dict(stages=[[0.,.5],[1.,.3]]),
                          dict(room_atr=-1.),dict(fee_reserve=float("inf"))):
            with self.assertRaises(ValueError):
                self.guard(**overrides)

    def test_restart_preserves_rules_and_floor_but_not_unconfirmed_spike(self):
        guard = self.guard()
        self.promote(guard,103.)
        guard.acknowledge("BUY",guard.desired_sl)
        self.observe(guard,110.,103.)
        restored = ProfitFloor(**json.loads(json.dumps(guard.export())))
        self.assertEqual(restored.protected_sl,guard.protected_sl)
        self.assertEqual(restored.confirmed_price,103.)
        self.assertEqual(restored.pending_since,0.)
        self.assertEqual(restored.rules,guard.rules)


class ProfitExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1791900000.
        self.position = SimpleNamespace(ticket=123,identifier=456,symbol=base.SYMBOL,magic=base.MAGIC,
            type=base.mt5.POSITION_TYPE_BUY,price_open=100.,sl=96.,tp=112.,volume=.01,
            time=int(self.now+10800),profit=0.,swap=0.)
        self.tick = SimpleNamespace(bid=100.,ask=100.33,time=int(self.now+10800),time_msc=(self.now+10800)*1000)
        self.info = SimpleNamespace(point=.01,digits=2,trade_tick_size=.01,
                                    trade_stops_level=20,trade_freeze_level=10)
        self.deals = [SimpleNamespace(position_id=456,symbol=base.SYMBOL,entry=base.mt5.DEAL_ENTRY_IN,
                                      commission=0.,fee=0.)]
        self.requests = []
        self.retcode = base.mt5.TRADE_RETCODE_DONE
        self.atr = 2.
        def send(request):
            self.requests.append(dict(request))
            if self.retcode == base.mt5.TRADE_RETCODE_DONE and request["action"] == base.mt5.TRADE_ACTION_SLTP:
                self.position.sl = request["sl"]
            return SimpleNamespace(retcode=self.retcode,comment="offline")
        def calc(action,symbol,volume,opened,closed):
            return (closed-opened)*(1 if action == base.mt5.ORDER_TYPE_BUY else -1)*(volume/.01)
        scopes = [patch.dict(os.environ,{"FIB_PA_GUARDIAN_STATE_FILE":str(self.root/"guard.json"),
                                        "FIB_PA_STRATEGY_STATE_FILE":str(self.root/"events.json")}),
                  patch.object(ai,"AI3_STATE_FILE",str(self.root/"entry.json")),
                  patch.object(base,"ENABLE_TRADING",True),patch.object(base,"GUARDIAN_ENABLED",True),
                  patch.object(base.time,"time",side_effect=lambda:self.now),
                  patch.object(base.mt5,"positions_get",side_effect=lambda **kw:(self.position,)),
                  patch.object(base.mt5,"symbol_info_tick",side_effect=lambda symbol:self.tick),
                  patch.object(base.mt5,"symbol_info",side_effect=lambda symbol:self.info),
                  patch.object(base.mt5,"history_deals_get",side_effect=lambda **kw:self.deals),
                  patch.object(base.mt5,"order_calc_profit",side_effect=calc),
                  patch.object(base.mt5,"order_send",side_effect=send),
                  patch.object(base,"calculate_atr",side_effect=lambda frame:self.atr)]
        for scope in scopes:
            scope.start(); self.addCleanup(scope.stop)
        self.trader = ai.ThreeAITrader()
        self.trader.positions_known = True
        self.trader.get_positions = lambda:[self.position]
        self.trader.get_rates = lambda *a,**kw:base.pd.DataFrame([dict(time=100,open=100.,high=101.,low=99.,close=100.,tick_volume=100)]*120)
        self.state = self.trader.get_or_create_guardian_state(self.position,self.tick,frozen=policy(),original_sl=96.,original_tp=112.)

    def sell(self):
        self.trader.guardian_states.clear()
        self.trader.guard_store.put(123,None)
        self.position.type = base.mt5.POSITION_TYPE_SELL
        self.position.sl,self.position.tp = 104.,88.
        self.state = self.trader.get_or_create_guardian_state(self.position,self.tick,frozen=policy(),original_sl=104.,original_tp=88.)

    def quote(self,price,dt=.5):
        self.now += dt
        if self.position.type == base.mt5.POSITION_TYPE_BUY:
            self.tick.bid,self.tick.ask = price,price+.33
        else:
            self.tick.bid,self.tick.ask = price-.33,price
        self.tick.time,self.tick.time_msc = int(self.now+10800),(self.now+10800)*1000

    def poll(self,price,dt=.5):
        self.quote(price,dt)
        self.trader.run_guardian()

    def lock(self,price=105.):
        for _ in range(3):
            self.poll(price)

    def test_unprofitable_new_position_does_not_use_legacy_virtual_or_structural_exit(self):
        with patch.object(base,"is_structure_failure",return_value=(True,"old")) as structural, \
             patch.object(self.trader,"close_position_by_ticket") as close:
            self.lock(99.)
            self.assertEqual(self.position.sl,96.)
            self.assertEqual(self.state.virtual_sl,0.)
            structural.assert_not_called(); close.assert_not_called()

    def test_both_sides_lock_profit_on_current_close_quote_and_keep_tp(self):
        for sell in (False,True):
            if sell:self.sell()
            old_tp = self.position.tp
            self.lock(95. if sell else 105.)
            self.assertAlmostEqual(self.position.sl,97.94 if sell else 102.06)
            self.assertEqual(self.position.tp,old_tp)
            self.assertEqual(self.state.profit_floor.protected_sl,self.position.sl)

    def test_pullback_holds_floor_instead_of_following_price_backwards(self):
        self.lock()
        stop = self.position.sl
        self.lock(104.)
        self.assertEqual(self.position.sl,stop)
        self.assertEqual(len(self.requests),1)

    def test_no_extra_submission_when_atr_falls_or_same_best_repeats(self):
        self.lock()
        self.atr = .1
        self.lock()
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.state.profit_floor.atr_floor,2.)

    def test_failed_update_is_not_acknowledged_and_retries_at_same_peak(self):
        self.retcode = 10016
        self.lock()
        self.assertEqual(self.position.sl,96.)
        self.assertEqual(self.state.profit_floor.protected_sl,96.)
        self.assertGreater(self.state.profit_floor.desired_sl,100.)
        self.retcode = base.mt5.TRADE_RETCODE_DONE
        self.poll(105.)
        self.assertAlmostEqual(self.position.sl,102.06)

    def test_latest_quote_can_defer_an_old_intent_without_clamping_it(self):
        self.retcode = 10016
        self.lock()
        self.retcode = base.mt5.TRADE_RETCODE_DONE
        self.poll(103.)
        self.assertEqual(self.position.sl,96.)
        self.poll(105.)
        self.assertAlmostEqual(self.position.sl,102.06)

    def test_ai3_hold_and_looser_or_too_close_suggestions_keep_floor(self):
        self.lock()
        stop = self.position.sl
        for action,sl in (("hold",0),("tighten_sl",101.),("tighten_sl",104.8)):
            self.trader.execute_ai3(dict(action=action,ticket=123,new_stop_loss=sl,reasoning="offline"))
        self.assertEqual(self.position.sl,stop)
        self.assertEqual(len(self.requests),1)
        self.trader.execute_ai3(dict(action="tighten_sl",ticket=123,new_stop_loss=103.,reasoning="structure"))
        self.assertEqual(self.position.sl,103.)
        self.assertEqual(self.state.profit_floor.protected_sl,103.)

    def test_costs_cover_open_and_exit_charges_and_swap_without_double_spread(self):
        self.deals[0].commission,self.deals[0].fee = -.25,-.05
        self.position.swap = -.2
        self.lock()
        self.assertAlmostEqual(self.state.profit_floor.cost_cash,.8)
        self.assertAlmostEqual(self.position.sl,102.48)

    def test_unknown_profit_model_keeps_existing_sl(self):
        with patch.object(base.mt5,"order_calc_profit",return_value=None):self.lock()
        self.assertFalse(self.requests)
        self.assertEqual(self.position.sl,96.)
        self.assertEqual(self.state.profit_floor.atr_floor,2.)
        self.trader.execute_ai3(dict(action="tighten_sl",ticket=123,new_stop_loss=104.,reasoning="too close"))
        self.assertFalse(self.requests)

    def test_unknown_fee_history_is_reported_as_estimated(self):
        self.deals = None
        self.lock()
        self.assertFalse(self.state.profit_floor.fees_known)
        self.assertAlmostEqual(self.state.profit_floor.cost_cash,.1)

    def test_stale_crossed_and_future_quotes_do_not_move_floor(self):
        self.quote(105.)
        for change in (dict(time=self.tick.time-10,time_msc=self.tick.time_msc-10000),
                       dict(ask=104.),dict(time=self.tick.time+10,time_msc=self.tick.time_msc+10000)):
            with patch.multiple(self.tick,**change):self.trader.run_guardian()
        self.assertFalse(self.requests)

    def test_pinned_clock_rejects_an_exact_hour_old_quote(self):
        self.quote(105.)
        self.tick.time -= 3600; self.tick.time_msc -= 3600000
        self.assertEqual(base.tick_clock_offset(self.tick,self.now),2.)
        self.assertEqual(self.state.profit_floor.server_offset,3.)
        self.trader.run_guardian()
        self.assertFalse(self.requests)

    def test_external_tighter_stop_is_durable_even_without_atr(self):
        self.quote(105.)
        self.position.sl,self.atr = 101.,0.
        self.trader.run_guardian()
        self.trader.guardian_states.clear()
        self.trader.guard_store = GuardStore(self.root/"guard.json")
        self.position.sl = 96.
        restored = self.trader.get_or_create_guardian_state(self.position,self.tick)
        self.assertEqual(restored.profit_floor.protected_sl,101.)
        self.trader.run_guardian()
        self.assertEqual(self.position.sl,101.)

    def test_initial_server_widening_does_not_redefine_original_protection(self):
        for sell in (False,True):
            if sell:self.sell()
            self.trader.guardian_states.clear();self.trader.guard_store.put(123,None)
            original_sl,original_tp = (104.,88.) if sell else (96.,112.)
            self.position.sl = 105. if sell else 95.
            state = self.trader.get_or_create_guardian_state(self.position,self.tick,frozen=policy(),
                                                           original_sl=original_sl,original_tp=original_tp)
            self.assertEqual(state.risk,4.)
            self.assertEqual(state.profit_floor.protected_sl,original_sl)
            self.poll(100.)
            self.assertEqual(self.position.sl,original_sl)

    def test_missing_or_loosened_server_sl_restores_acknowledged_floor(self):
        self.lock()
        stop = self.position.sl
        for removed in (0.,96.):
            self.position.sl = removed
            self.poll(105.)
            self.assertEqual(self.position.sl,stop)

    def test_crossing_floor_exits_using_saved_identity(self):
        self.lock()
        with patch.object(self.trader,"close_position_by_ticket",return_value=True) as close:
            self.poll(102.)
            close.assert_called_once()
            self.assertIn("底线",close.call_args.args[1])

    def test_restart_restores_original_risk_rules_peak_and_floor(self):
        self.lock()
        original = self.state.profit_floor.export()
        self.trader.guardian_states.clear()
        self.trader.guard_store = GuardStore(self.root/"guard.json")
        with patch.dict(os.environ,{"FIB_PA_PROFIT_ROOM_ATR":"9"}):
            restored = self.trader.get_or_create_guardian_state(self.position,self.tick)
        self.assertEqual(restored.risk,4.)
        self.assertEqual(restored.initial_sl,96.)
        self.assertEqual(restored.profit_floor.export(),original)

    def test_identity_change_does_not_reuse_saved_floor(self):
        self.lock()
        self.trader.guardian_states.clear()
        for field,value in (("identifier",999),("volume",.02),("price_open",101.)):
            with patch.object(self.position,field,value),self.assertRaises(ValueError):
                self.trader.get_or_create_guardian_state(self.position,self.tick)

    def test_atomic_save_failure_does_not_claim_new_profit_lock(self):
        with patch.object(self.trader.guard_store,"put",return_value=False):self.lock()
        self.assertEqual(self.position.sl,96.)
        self.assertFalse(self.requests)

    def test_replace_failure_keeps_previous_file_and_in_memory_record(self):
        before = (self.root/"guard.json").read_bytes()
        records = copy.deepcopy(self.trader.guard_store.records)
        with patch("strategies.profit_guard.os.replace",side_effect=OSError("offline disk failure")):
            self.assertFalse(self.trader.guard_store.put(123,None))
        self.assertEqual((self.root/"guard.json").read_bytes(),before)
        self.assertEqual(self.trader.guard_store.records,records)
        self.assertEqual(list(self.root.glob("guard.json.*")),[])

    def test_observe_mode_does_not_acknowledge_or_persist_a_real_lock(self):
        before = (self.root/"guard.json").read_bytes()
        with patch.object(base,"ENABLE_TRADING",False):self.lock()
        self.assertEqual(self.position.sl,96.)
        self.assertEqual(self.state.profit_floor.protected_sl,96.)
        self.assertEqual((self.root/"guard.json").read_bytes(),before)
        self.assertFalse(self.requests)

    def test_legacy_position_without_frozen_rule_does_not_migrate_on_restart(self):
        self.trader.guardian_states.clear(); self.trader.guard_store.put(123,None)
        state = self.trader.get_or_create_guardian_state(self.position,self.tick)
        self.assertIsNone(state.profit_floor)

    def reserve_original(self,*,bound=True):
        event = "test-profit-event"
        token = base.execution_token(event,base.SYMBOL,base.MAGIC,"buy")
        self.position.comment = token
        rules = policy(room_atr=2.)
        metadata = dict(guardian_policy=rules,guardian_initial_sl=96.,guardian_initial_tp=112.,
                        server_utc_offset=3.,max_hold_minutes=90,cooldown_seconds=60,
                        execution_token=token,event_id=event,symbol=base.SYMBOL,magic=base.MAGIC,action="buy")
        self.assertTrue(self.trader.strategy_ledger.reserve(event,self.now,metadata))
        if bound:self.assertTrue(self.trader.strategy_ledger.record_position(123,event,self.now,90,60))
        self.trader.guardian_states.clear();self.trader.guard_store.put(123,None)
        return rules

    def test_crash_before_binding_recovers_original_policy_and_deadline_from_exact_token(self):
        rules = self.reserve_original(bound=False)
        with patch.dict(os.environ,{"FIB_PA_PROFIT_ROOM_ATR":"9"}):self.trader.run_guardian()
        state = self.trader.guardian_states[123]
        self.assertEqual(state.profit_floor.rules,rules)
        self.assertEqual(state.risk,4.)
        ledger = self.trader.strategy_ledger.snapshot()
        self.assertEqual(ledger["positions"]["123"]["opened_at_utc"],self.now)
        deadline = self.now+90*60
        self.assertEqual(self.trader.strategy_ledger.due_tickets(deadline), (123,))
        self.quote(100.,90*60)
        with patch.object(self.trader,"close_position_by_ticket",return_value=True) as close:
            self.trader.run_guardian()
            close.assert_called_once()
            self.assertIn("最长持有期限",close.call_args.args[1])

    def test_corrupt_store_does_not_rebuild_a_known_new_position_as_legacy(self):
        self.reserve_original()
        path = self.root/"corrupt.json";path.write_text("broken",encoding="utf-8")
        self.trader.guard_store = GuardStore(path)
        with self.assertRaises(ValueError):self.trader.get_or_create_guardian_state(self.position,self.tick)
        with patch.object(base,"is_structure_failure") as old_exit:self.trader.run_guardian()
        old_exit.assert_not_called()
        self.assertFalse(self.requests)
        self.assertEqual(path.read_text(),"broken")

    def test_partial_identity_cannot_be_frozen_or_written(self):
        self.trader.guardian_states.clear();self.trader.guard_store.put(123,None)
        for field,value in (("identifier",0),("time",0),("symbol","OTHER"),("magic",base.MAGIC+1)):
            with patch.object(self.position,field,value),self.assertRaises(ValueError):
                self.trader.get_or_create_guardian_state(self.position,self.tick,frozen=policy(),
                                                       original_sl=96.,original_tp=112.)
        self.assertNotIn("123",self.trader.guard_store.records)

    def entry_result(self):
        return base.StrategyResult("buy",80,False,"offline",96.,112.,80,0,{})

    def test_store_preflight_failure_sends_no_entry_order(self):
        with patch.object(self.trader,"validate_strategy_entry",return_value=""), \
             patch.object(self.trader,"current_position_count",return_value=0), \
             patch.object(self.trader,"validate_execution_prices",return_value=""), \
             patch.object(self.trader.guard_store,"flush",return_value=False):
            self.assertFalse(base.FibVolumePATrader.place_order(self.trader,self.entry_result()))
        self.assertFalse(self.requests)

    def test_ai3_receives_acknowledged_floor_intent_original_rules_and_room(self):
        self.lock()
        self.info.trade_stops_level = 300
        snapshot = self.trader.collect_ai3_snapshot()
        item = json.loads(snapshot["prompt"].split("\n",1)[1])["positions"][0]
        guard = item["profit_guard"]
        self.assertEqual(guard["protected_sl"],self.position.sl)
        self.assertEqual(guard["desired_sl"],self.state.profit_floor.desired_sl)
        self.assertEqual(guard["rules"],self.state.profit_floor.rules)
        self.assertAlmostEqual(guard["minimum_room"],3.01)
        self.assertTrue(guard["broker_distance_known"])
        self.assertIn("hold不得撤回保护底线",ai.AI3_INSTRUCTIONS)

    def test_corrupt_state_is_not_overwritten(self):
        path = self.root/"bad.json"; path.write_text("broken",encoding="utf-8")
        store = GuardStore(path)
        self.assertTrue(store.error)
        self.assertFalse(store.put(123,{}))
        self.assertEqual(path.read_text(),"broken")


if __name__ == "__main__":
    unittest.main()
