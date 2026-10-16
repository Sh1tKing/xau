"""Actual AI-3 POST binding and underwater SL execution, without network/terminal."""
import copy
import json
import unittest
from unittest.mock import patch

import test_ai3_target_review as setup
import test_profit_guardian as fixtures

ai, base = fixtures.ai, fixtures.base


class WeakeningExecutionTests(unittest.TestCase):
    quote=fixtures.ProfitExecutionTests.quote
    poll=fixtures.ProfitExecutionTests.poll
    lock=fixtures.ProfitExecutionTests.lock

    def setUp(self):
        setup.TargetReviewExecutionTests.setUp(self)
        self.trader.guardian_states.clear()
        self.trader.guard_store.put(123,None)
        self.position.sl,self.position.tp=90.,130.
        self.position.time=int(self.now+10800)-600
        self.quote(99.)
        self.state=self.trader.get_or_create_guardian_state(self.position,self.tick,
            frozen=setup.policy(),original_sl=90.,original_tp=130.)
        self.state.profit_floor.atr_floor=2.
        self.requests.clear()

    def snapshot(self):
        snap=self.trader.collect_ai3_snapshot()
        return dict(action="tighten_sl",ticket=123,snapshot_id=snap["snapshot_id"],
                    new_stop_loss=96.,new_take_profit=None,take_profit_evidence=[],
                    reasoning="闭合M5走弱但原理由未失效，保持空间主动减险"),snap["protection_review"]

    def apply(self,payload,proof):
        return self.trader.apply_ai3_stop_review(self.position,payload,proof)

    def test_underwater_stop_tightening_does_not_require_profit_or_cash_tier(self):
        payload,proof=self.snapshot()
        original=(self.state.initial_sl,self.state.initial_tp,self.state.risk,copy.deepcopy(self.state.profit_floor.rules))
        self.assertTrue(self.apply(payload,proof))
        self.assertEqual((self.position.sl,self.position.tp),(96.,130.))
        self.assertEqual(len(self.requests),1)
        self.assertEqual(original,(self.state.initial_sl,self.state.initial_tp,self.state.risk,self.state.profit_floor.rules))
        self.assertLess(self.position.sl,self.position.price_open)

    def test_hold_never_automatically_executes_the_reference_stop_or_closes(self):
        payload,proof=self.snapshot()
        payload.update(action="hold",new_stop_loss=96.)
        with patch.object(self.trader,"close_position_by_ticket") as close:
            self.trader.execute_ai3(payload,review=proof)
            close.assert_not_called()
        self.assertFalse(self.requests)
        self.assertEqual(self.position.sl,90.)

    def test_missing_old_or_invented_proof_never_modifies_or_retries(self):
        for changed in (None,dict(snapshot_id="old"),dict(ticket=0),dict(ticket=999),dict(reasoning="")):
            payload,proof=self.snapshot()
            if changed is None:proof=None
            else:payload.update(changed)
            with patch.object(ai,"call_chat") as call:
                self.trader.execute_ai3(payload,review=proof)
                call.assert_not_called()
        self.assertFalse(self.requests)

    def test_account_full_identity_original_tp_and_risk_are_bound(self):
        for field,value in (("identifier",999),("volume",.02),("price_open",101.),("tp",131.)):
            payload,proof=self.snapshot()
            with patch.object(self.position,field,value):self.assertFalse(self.apply(payload,proof))
        payload,proof=self.snapshot()
        with patch.object(self.account,"server","other"):
            self.assertFalse(self.apply(payload,proof))
        with patch.object(self.account,"trade_mode",base.mt5.ACCOUNT_TRADE_MODE_REAL):
            self.assertFalse(self.apply(payload,proof))
        proof["positions"]["123"]["risk"]=9.
        self.assertFalse(self.apply(payload,proof))
        self.assertFalse(self.requests)

    def test_source_change_new_closed_bar_and_history_gaps_reject(self):
        payload,proof=self.snapshot();self.changed_bar=True
        self.assertFalse(self.apply(payload,proof));self.changed_bar=False
        payload,proof=self.snapshot();self.quote(99.,dt=61.)
        self.assertFalse(self.apply(payload,proof))
        payload,proof=self.snapshot()
        original=self.trader.get_rates
        def missing(frame,count,shift=1):
            data=original(frame,count,shift)
            return data.drop(data.index[-3])
        self.trader.get_rates=missing
        self.assertFalse(self.apply(payload,proof))
        self.assertFalse(self.requests)

    def test_price_drift_freshness_crossing_and_stop_room_are_checked_at_execution(self):
        for mode in ("drift","stale","crossed","lead","near","expired","future"):
            payload,proof=self.snapshot()
            old_tick=copy.deepcopy(self.tick.__dict__)
            if mode=="drift":self.quote(102.)
            elif mode=="stale":self.tick.time-=10;self.tick.time_msc-=10000
            elif mode=="crossed":self.tick.ask=self.tick.bid-.1
            elif mode=="lead":self.tick.time+=10;self.tick.time_msc+=10000
            elif mode=="near":payload["new_stop_loss"]=98.
            elif mode=="expired":proof["observed_at"]-=91.
            else:proof["observed_at"]+=1.
            self.assertFalse(self.apply(payload,proof),mode)
            self.tick.__dict__.update(old_tick)
        self.assertFalse(self.requests)

    def test_stronger_manual_sl_and_failed_broker_reply_cannot_claim_protection(self):
        payload,proof=self.snapshot();self.position.sl=96.5
        self.assertFalse(self.apply(payload,proof));self.assertFalse(self.requests)
        self.position.sl=90.;self.state.profit_floor.protected_sl=90.
        payload,proof=self.snapshot();self.retcode=10016
        self.assertFalse(self.apply(payload,proof))
        self.assertEqual(self.position.sl,90.)
        self.assertEqual(self.state.profit_floor.protected_sl,90.)

    def test_sell_uses_ask_and_mirrored_full_room_without_touching_tp(self):
        self.position.type=base.mt5.POSITION_TYPE_SELL
        self.position.sl,self.position.tp=110.,70.
        self.trader.guardian_states.clear();self.trader.guard_store.put(123,None)
        self.quote(101.)
        self.state=self.trader.get_or_create_guardian_state(self.position,self.tick,
            frozen=setup.policy(),original_sl=110.,original_tp=70.)
        self.state.profit_floor.atr_floor=2.
        payload,proof=self.snapshot();payload["new_stop_loss"]=104.
        self.assertTrue(self.apply(payload,proof))
        self.assertEqual((self.position.sl,self.position.tp),(104.,70.))

    def test_existing_bounded_broker_clock_lead_still_allows_a_fresh_sl_review(self):
        self.tick.time_msc+=2635
        self.tick.time=int(self.tick.time_msc/1000)
        payload,proof=self.snapshot()
        self.assertTrue(self.apply(payload,proof))
        self.assertEqual(self.position.sl,96.)

    def test_cash_export_and_reference_estimates_use_the_same_refreshed_costs(self):
        self.deals[0].commission=-.25
        self.position.swap=-.2
        self.state.profit_floor.fees_known=False
        pack=self.trader.collect_ai3_snapshot()["market_pack"]
        guard=pack["positions"][0]["profit_guard"]
        self.assertTrue(guard["fees_known"])
        self.assertAlmostEqual(guard["cost_cash"],.7)
        self.assertEqual(guard["fee_status"],"opening_history_plus_exit_reserve")

    def test_each_post_refreshes_and_only_the_successful_latest_snapshot_can_change_sl(self):
        for use_old in (True,False):
            def call(*args,**kwargs):
                old=kwargs["refresh_request"]()
                self.quote(99.1)
                latest=kwargs["refresh_request"]()
                snapshot=old if use_old else latest
                return json.dumps(dict(action="tighten_sl",ticket=123,snapshot_id=snapshot["snapshot_id"],
                    new_stop_loss=96.,new_take_profit=None,take_profit_evidence=[],reasoning="闭合M5风险复核"))
            with patch.object(ai,"call_chat",side_effect=call),patch.object(self.trader,"ai3_is_current",return_value=True):
                self.trader.ai3_task(100,1)
            self.assertEqual(len(self.requests),0 if use_old else 1)

    def test_snapshot_has_computed_facts_and_original_context_prefix(self):
        self.trader.entry_contexts["123"]=dict(status="complete",initial_risk=10.,
            strategy_contract={"id":"original"},strategy_evidence={"buy":{"original":1},"sell":{"original":2}})
        snapshot=self.trader.collect_ai3_snapshot()
        pack=snapshot["market_pack"]
        self.assertIn("risk_review",pack["positions"][0])
        self.assertTrue(pack["positions"][0]["risk_review"]["available"])
        self.assertLess(snapshot["prompt"].index('"entry_context"'),snapshot["prompt"].index('"risk_review"'))
        self.assertEqual(pack["positions"][0]["entry_context"]["strategy_evidence"],
                         {"buy":{"original":1},"sell":{"original":2}})

    def test_post_snapshot_logging_excludes_headers_keys_and_private_thinking(self):
        payload,proof=self.snapshot()
        snapshot=self.trader.collect_ai3_snapshot()
        payload["snapshot_id"]=snapshot["snapshot_id"]
        answer=dict(id="offline",type="message",content=[{"type":"thinking","thinking":"PRIVATE_DO_NOT_LOG"},
            {"type":"text","text":json.dumps(payload)}],stop_reason="end_turn",usage={"input_tokens":100,"output_tokens":100})
        response=fixtures.SimpleNamespace(status_code=200,headers={},text="offline",json=lambda:answer)
        with patch.object(ai,"AI_DECISION_SNAPSHOT_DIR",str(self.root/"decisions")), \
             patch.object(ai,"AI_USAGE_DIR",str(self.root/"usage")), \
             patch.object(ai.requests,"post",return_value=response), \
             patch.object(ai,"AI_HTTP_RETRIES",1),patch.object(ai,"backup_chat_config",return_value=
                 dict(url="https://offline.invalid/v1/messages",key="PRIVATE_KEY_DO_NOT_LOG",model="offline",effort="low")):
            ai.call_chat("AI-3","offline","low",ai.AI3_INSTRUCTIONS,snapshot["prompt"],40,
                "https://offline.invalid/v1/messages","PRIVATE_KEY_DO_NOT_LOG",refresh_request=lambda:snapshot,
                request_context={"decision_id":"offline-post"})
        paths=list((self.root/"decisions").glob("*.jsonl"))
        text="".join(p.read_text(encoding="utf-8") for p in paths)
        self.assertIn('"ai3_input"',text)
        self.assertIn('"ai3_response"',text)
        self.assertNotIn("PRIVATE_KEY_DO_NOT_LOG",text)
        self.assertNotIn("PRIVATE_DO_NOT_LOG",text)


if __name__ == "__main__":
    unittest.main()
