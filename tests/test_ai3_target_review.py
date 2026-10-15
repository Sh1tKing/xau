"""AI-3 may review TP from closed candles without loosening the cash floor."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_profit_guardian as fixtures
from strategies.profit_guard import policy

ai, base = fixtures.ai, fixtures.base


class TargetReviewSchemaTests(unittest.TestCase):
    def test_wire_contract_exposes_atomic_protection_and_candle_basis(self):
        schema=ai.response_json_schema("AI-3")
        self.assertIn("adjust_protection",schema["properties"]["action"]["enum"])
        for key in ("snapshot_id","new_take_profit","take_profit_evidence"):
            self.assertIn(key,schema["properties"])


class TargetReviewExecutionTests(unittest.TestCase):
    quote=fixtures.ProfitExecutionTests.quote
    poll=fixtures.ProfitExecutionTests.poll
    lock=fixtures.ProfitExecutionTests.lock

    def setUp(self):
        fixtures.ProfitExecutionTests.setUp(self)
        self.trader.guardian_states.clear();self.trader.guard_store.put(123,None)
        self.state=self.trader.get_or_create_guardian_state(self.position,self.tick,frozen=policy(),
            original_sl=96.,original_tp=112.)
        self.atr=8.32
        self.account=SimpleNamespace(login=12345,server="offline-demo",trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO)
        scope=patch.object(base.mt5,"account_info",side_effect=lambda:self.account)
        scope.start();self.addCleanup(scope.stop)
        self.changed_bar=False
        def rates(frame,count,shift=1):
            seconds={base.mt5.TIMEFRAME_M1:60,base.mt5.TIMEFRAME_M5:300,
                     base.mt5.TIMEFRAME_M15:900,base.mt5.TIMEFRAME_H1:3600}[frame]
            closed=(int(self.now+10800)//seconds)*seconds-seconds
            return base.pd.DataFrame([dict(time=closed-i*seconds,open=100.,high=101.,low=99.,
                close=100.1 if self.changed_bar else 100.,tick_volume=100.) for i in range(count-1,-1,-1)])
        self.trader.get_rates=rates
        original=base.mt5.order_send.side_effect
        def send(request):
            result=original(request)
            if result.retcode == base.mt5.TRADE_RETCODE_DONE and request["action"] == base.mt5.TRADE_ACTION_SLTP:
                self.position.tp=request["tp"]
            return result
        base.mt5.order_send.side_effect=send
        self.lock(105.1)
        self.requests.clear()

    def snapshot(self):
        snap=self.trader.collect_ai3_snapshot()
        proof=snap["protection_review"]
        payload=dict(action="adjust_protection",ticket=123,snapshot_id=proof["snapshot_id"],
            new_stop_loss=None,new_take_profit=110.,take_profit_evidence=[
                dict(timeframe="M5",time=proof["bars"]["M5"][-1]["time"])],reasoning="已收盘M5拒绝，目标收至前方障碍")
        return payload,proof

    def apply(self,payload,proof):
        return self.trader.apply_ai3_target_review(self.position,payload,proof)

    def test_tp_only_preserves_latest_actual_sl_and_original_risk_contract(self):
        payload,proof=self.snapshot()
        self.position.sl=101.  # A local/manual improvement while the model was thinking.
        self.assertTrue(self.apply(payload,proof))
        self.assertEqual(len(self.requests),1)
        self.assertEqual((self.requests[0]["sl"],self.requests[0]["tp"]),(101.,110.))
        self.assertEqual((self.state.initial_sl,self.state.initial_tp,self.state.risk),(96.,112.,4.))
        self.assertEqual(self.state.profit_floor.protected_sl,101.)

    def test_valid_combined_sl_tp_is_one_broker_request(self):
        self.position.tp=124.
        self.quote(112.1)
        payload,proof=self.snapshot()
        payload.update(new_stop_loss=103.,new_take_profit=120.)
        self.assertTrue(self.apply(payload,proof))
        self.assertEqual([(r["sl"],r["tp"]) for r in self.requests],[(103.,120.)])

    def test_invalid_sl_rejects_the_entire_tp_change(self):
        payload,proof=self.snapshot();payload["new_stop_loss"]=96.
        self.assertFalse(self.apply(payload,proof))
        self.assertFalse(self.requests);self.assertEqual(self.position.tp,112.)

    def test_extension_requires_actual_cost_covered_stop_and_respects_six_r(self):
        payload,proof=self.snapshot();payload["new_take_profit"]=115.
        self.assertTrue(self.apply(payload,proof))
        payload,proof=self.snapshot();payload["new_take_profit"]=125.
        self.requests.clear()
        self.assertFalse(self.apply(payload,proof));self.assertFalse(self.requests)
        self.position.tp=112.;self.position.sl=self.state.profit_floor.protected_sl=96.
        payload,proof=self.snapshot();payload["new_take_profit"]=115.
        self.assertFalse(self.apply(payload,proof));self.assertFalse(self.requests)

    def test_missing_or_invented_request_id_ticket_or_candle_never_modifies(self):
        for changes in (dict(snapshot_id="invented"),dict(ticket=0),dict(ticket=999),
                        dict(take_profit_evidence=[]),dict(take_profit_evidence=[dict(timeframe="M1",time=1)]),
                        dict(take_profit_evidence=[dict(timeframe="M5",time=300)]),dict(reasoning="")):
            payload,proof=self.snapshot();payload.update(changes)
            self.assertFalse(self.apply(payload,proof),changes)
        payload,proof=self.snapshot()
        self.assertFalse(self.apply(payload,None));self.assertFalse(self.requests)

    def test_changed_closed_candle_or_original_target_rejects_reply(self):
        payload,proof=self.snapshot();self.changed_bar=True
        self.assertFalse(self.apply(payload,proof));self.changed_bar=False
        payload,proof=self.snapshot();self.position.tp=113.
        self.assertFalse(self.apply(payload,proof));self.assertFalse(self.requests)

    def test_quote_or_fee_change_rechecks_instead_of_blindly_using_old_target(self):
        payload,proof=self.snapshot();self.quote(106.2)
        self.assertFalse(self.apply(payload,proof))
        payload,proof=self.snapshot();payload["new_take_profit"]=106.5
        self.assertFalse(self.apply(payload,proof))
        payload,proof=self.snapshot();self.position.swap=-20.
        self.assertFalse(self.apply(payload,proof));self.assertFalse(self.requests)

    def test_expired_future_or_unclosed_source_is_rejected(self):
        for change in ("expired","future","not_closed"):
            payload,proof=self.snapshot()
            if change=="expired":proof["observed_at"]-=91.
            elif change=="future":proof["observed_at"]+=1.
            else:proof["server_offset"]=-12.
            self.assertFalse(self.apply(payload,proof),change)
        self.assertFalse(self.requests)

    def test_account_server_demo_and_full_position_identity_are_bound(self):
        for field,value in (("server","other"),("login",999),("trade_mode",base.mt5.ACCOUNT_TRADE_MODE_REAL)):
            payload,proof=self.snapshot()
            with patch.object(self.account,field,value):self.assertFalse(self.apply(payload,proof))
        for field,value in (("identifier",999),("volume",.02),("price_open",101.)):
            payload,proof=self.snapshot()
            with patch.object(self.position,field,value):self.assertFalse(self.apply(payload,proof))
        self.assertFalse(self.requests)

    def test_missing_current_profit_model_or_invalid_target_does_not_submit(self):
        payload,proof=self.snapshot()
        with patch.object(base.mt5,"order_calc_profit",return_value=None):
            self.assertFalse(self.apply(payload,proof))
        for target in (0.,-1.,True,float("nan"),float("inf"),100.,105.2,112.05):
            payload,proof=self.snapshot();payload["new_take_profit"]=target
            self.assertFalse(self.apply(payload,proof))
        self.assertFalse(self.requests)

    def test_broker_failure_does_not_claim_or_save_tp_change(self):
        payload,proof=self.snapshot();self.retcode=10016
        original=self.state.profit_floor.protected_sl
        self.assertFalse(self.apply(payload,proof))
        self.assertEqual(self.position.tp,112.)
        self.assertEqual(self.state.profit_floor.protected_sl,original)

    def test_old_frozen_policy_does_not_silently_gain_tp_authority(self):
        payload,proof=self.snapshot()
        self.state.profit_floor=fixtures.ProfitFloor(fixtures.policy(),100.6,105.1,server_offset=3.)
        self.assertFalse(self.apply(payload,proof));self.assertFalse(self.requests)

    def test_current_guard_floor_cannot_be_replaced_by_a_widened_server_stop(self):
        payload,proof=self.snapshot();self.position.sl=96.
        self.assertFalse(self.apply(payload,proof));self.assertFalse(self.requests)

    def test_wire_schema_and_snapshot_have_the_same_review_contract(self):
        payload,proof=self.snapshot()
        self.assertFalse(ai.response_schema_error(payload,ai.response_json_schema("AI-3")))
        self.assertEqual(proof["positions"]["123"]["identity"],base.guardian_identity(self.position))

    def test_sell_tp_shortening_and_extension_use_ask_and_keep_sl(self):
        self.position.type=base.mt5.POSITION_TYPE_SELL
        self.position.sl,self.position.tp=104.,88.
        self.trader.guardian_states.clear();self.trader.guard_store.put(123,None)
        self.quote(100.)
        self.state=self.trader.get_or_create_guardian_state(self.position,self.tick,frozen=policy(),
            original_sl=104.,original_tp=88.)
        self.lock(94.9);self.requests.clear()
        payload,proof=self.snapshot();payload["new_take_profit"]=90.
        protected=self.position.sl
        self.assertTrue(self.apply(payload,proof))
        self.assertEqual((self.position.sl,self.position.tp),(protected,90.))
        payload,proof=self.snapshot();payload["new_take_profit"]=85.
        self.assertTrue(self.apply(payload,proof))
        self.assertEqual(self.position.tp,85.)
        payload,proof=self.snapshot();payload["new_take_profit"]=75.
        self.assertFalse(self.apply(payload,proof))

    def test_tp_rounding_stays_on_grid_and_does_not_invent_extra_reward(self):
        self.info.trade_tick_size=.25
        payload,proof=self.snapshot();payload["new_take_profit"]=110.13
        self.assertTrue(self.apply(payload,proof))
        self.assertEqual(self.position.tp,110.)

    def test_stale_or_crossed_quote_stops_tp_before_any_broker_request(self):
        for stale in (False,True):
            payload,proof=self.snapshot()
            old=copy.deepcopy(self.tick.__dict__)
            if stale:self.tick.time-=10;self.tick.time_msc-=10000
            else:self.tick.ask=self.tick.bid-1.
            self.assertFalse(self.apply(payload,proof))
            self.tick.__dict__.update(old)
        self.assertFalse(self.requests)

    def test_no_extra_post_or_repair_is_started_for_invalid_reply(self):
        payload,proof=self.snapshot();payload["snapshot_id"]="wrong"
        with patch.object(ai,"call_chat") as call:
            self.trader.execute_ai3(payload,review=proof)
            call.assert_not_called()
        self.assertFalse(self.requests)

    def test_reply_from_an_earlier_retry_cannot_replace_latest_post_proof(self):
        def call(*args,**kwargs):
            first=kwargs["refresh_request"]()
            self.quote(105.3)
            kwargs["refresh_request"]()
            proof=first["protection_review"]
            return json.dumps(dict(action="adjust_protection",ticket=123,snapshot_id=proof["snapshot_id"],
                new_stop_loss=None,new_take_profit=110.,take_profit_evidence=[
                    dict(timeframe="M5",time=proof["bars"]["M5"][-1]["time"])],reasoning="旧请求回复"))
        with patch.object(ai,"call_chat",side_effect=call),patch.object(self.trader,"ai3_is_current",return_value=True):
            self.trader.ai3_task(100,1)
        self.assertEqual(self.position.tp,112.)
        self.assertFalse(self.requests)

    def test_successful_post_uses_latest_refreshed_evidence(self):
        def call(*args,**kwargs):
            first=kwargs["refresh_request"]()
            self.quote(105.3)
            latest=kwargs["refresh_request"]()
            proof=latest["protection_review"]
            self.assertNotEqual(first["snapshot_id"],latest["snapshot_id"])
            return json.dumps(dict(action="adjust_protection",ticket=123,snapshot_id=proof["snapshot_id"],
                new_stop_loss=None,new_take_profit=110.,take_profit_evidence=[
                    dict(timeframe="M5",time=proof["bars"]["M5"][-1]["time"])],reasoning="闭合M5拒绝"))
        with patch.object(ai,"call_chat",side_effect=call),patch.object(self.trader,"ai3_is_current",return_value=True):
            self.trader.ai3_task(100,1)
        self.assertEqual(self.position.tp,110.)
        self.assertEqual(len(self.requests),1)


if __name__ == "__main__":
    unittest.main()
