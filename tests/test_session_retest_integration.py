"""Production research-policy gates with synthetic closed bars and mocked MT5."""
import copy
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import xauusd_fib_volume_pa_ai3 as ai
from strategies.framework import DEFAULT_REGISTRY
from strategies.lifecycle import EventLedger
from test_session_retest_facts import fixture

base = ai.base
logging.disable(logging.CRITICAL)


def market(side="buy", late_confirmation=False):
    m5, clock = fixture(side=side)
    if late_confirmation:
        # Keep the 08:00 opening box, but confirm the first event at local 10:55.
        flat = m5.iloc[0].to_dict()
        flat_bars = [dict(flat, time=int(m5.time.iloc[-3])+300*i) for i in range(1,28)]
        confirmation = m5.iloc[-2:].copy()
        confirmation["time"] += 27*300
        m5 = base.pd.concat([m5.iloc[:-2],base.pd.DataFrame(flat_bars),confirmation],ignore_index=True)
        clock += timedelta(minutes=135)
    m5["tick_volume"] = 100
    stamp = int(clock.timestamp()) + 10800
    close = 101.5 if side == "buy" else 98.5
    m15 = base.pd.DataFrame([dict(time=stamp//900*900-900*(80-i), open=close,
                                high=close+1, low=close-1, close=close, tick_volume=100)
                               for i in range(80)])
    sign = 1 if side == "buy" else -1
    h1 = base.pd.DataFrame([dict(time=stamp//3600*3600-3600*(80-i), open=100+sign*i*.02,
                               high=101+sign*i*.02, low=99+sign*i*.02,
                               close=100+sign*i*.02, tick_volume=100) for i in range(80)])
    return m15, h1, m5, clock


class ResearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.frames = market()
        self.now = self.frames[-1].timestamp()+5
        self.env = patch.dict(os.environ, {"FIB_PA_STRATEGY_STATE_FILE": str(self.directory/"events.json")})
        self.env.start(); self.addCleanup(self.env.stop)
        self.time = patch.object(base.time, "time", return_value=self.now)
        self.time.start(); self.addCleanup(self.time.stop)
        self.notice = patch.object(base, "send_feishu_notification")
        self.notice.start(); self.addCleanup(self.notice.stop)

    def decision(self, side="buy", **kwargs):
        m15, h1, m5, clock = self.frames if side == "buy" else market(side)
        return base.build_fib_volume_pa_decision(m15, h1, m5, 55, now=clock,
                                               server_utc_offset=3, **kwargs)

    def trader(self, ai_mode=False):
        with patch.object(ai, "AI3_STATE_FILE", str(self.directory/"ai3.json")):
            return ai.ThreeAITrader() if ai_mode else base.FibVolumePATrader()

    def quote(self, result):
        ref = float(result.details["entry_price"])
        return SimpleNamespace(bid=ref, ask=ref+.33, time=int(self.now+10800))

    def test_only_report_candidate_is_active_in_all_host_sessions(self):
        snapshot = DEFAULT_REGISTRY.snapshot()
        for session in ("asia", "london", "overlap", "late_us"):
            self.assertEqual([s.id for s in snapshot.active(session)], ["session_retest"])
        contract = snapshot.get("session_retest").manifest()
        self.assertEqual((contract["target_max_reference_r"], contract["max_hold_minutes"],
                          contract["cooldown_seconds"], contract["demo_only"]), (3.,180,600,True))

    def test_actual_local_builder_uses_confirmation_reference_and_three_bar_stop(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                result = self.decision(side)
                self.assertEqual(result.action, side, result.reasoning)
                m15, h1, m5, _ = market(side)
                ref = float(m5.close.iloc[-1])
                self.assertEqual(float(result.details["entry_price"]), ref)
                self.assertEqual(result.details["reference_price_source"], "confirmed_closed_m5_close")
                self.assertTrue(base.stop_covers_m5_extreme(side,result.stop_loss,
                                   base.recent_m5_extreme(m5,side),base.calculate_atr(m15)))
                self.assertAlmostEqual(base.reward_risk_ratio(side,ref,result.stop_loss,result.take_profit),3.,places=2)

    def test_h1_permission_and_durable_event_gate_are_hard_blockers(self):
        with patch.object(base,"trend_from_ema",return_value="bear"):
            result = self.decision()
        self.assertEqual(result.action,"no")
        result = self.decision(entry_gate=lambda event,now:"already used")
        self.assertEqual(result.action,"no")
        self.assertIn("already used",result.reasoning)

    def test_ai_pack_and_finalizer_bind_new_rule_without_old_fib_target(self):
        local = self.decision()
        m15,h1,m5,_ = self.frames
        pack,fib,atr,price = ai.build_feature_pack(m15,h1,m5,local,
            quote=dict(bid=101.6,ask=101.93,time=1))
        self.assertIsNone(fib)
        self.assertEqual(pack["reference_price"]["source"],"confirmed_closed_m5_close")
        self.assertEqual(pack["strategy_evidence"]["buy"]["evidence"]["closed_trigger"]["timeframe"],"M5")
        payload=dict(action="buy",confidence=85,reasoning="first retest",trigger_evidence="M5 retest",
                     invalidation="box reclaim",target_basis="3R room",stop_loss=101.5,take_profit=120.)
        proposal,error=ai.finalize_ai_proposal(payload,price,atr,"bull","neutral",fib,
            df_m15=m15,df_m5=m5,setup="session_retest",strategy_contract=pack["strategy_contract"],
            strategy_evidence=pack["strategy_evidence"],local_stop=local.stop_loss,local_target=local.take_profit)
        self.assertIsNotNone(proposal,error)
        self.assertTrue(base.stop_covers_m5_extreme("buy",proposal.stop_loss,99.,atr))
        self.assertLessEqual(base.reward_risk_ratio("buy",price,proposal.stop_loss,proposal.take_profit),3.001)

    def test_shared_execution_guard_rejects_drift_spread_and_missing_inputs(self):
        result=self.decision()
        self.assertEqual(base.research_execution_error(result,self.quote(result)),"")
        for bid,ask in ((result.stop_loss+.5,result.stop_loss+.83),(110.,110.33),(101.6,105.)):
            self.assertTrue(base.research_execution_error(result,SimpleNamespace(bid=bid,ask=ask)))
        result.details.pop("entry_guard_ema21")
        self.assertTrue(base.research_execution_error(result,self.quote(result)))

    def test_live_account_and_unknown_account_stop_before_order(self):
        trader=self.trader();result=self.decision()
        for account in (None,SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_REAL)):
            with patch.object(base.mt5,"account_info",return_value=account),patch.object(base.mt5,"order_send") as send:
                self.assertFalse(trader.place_order(result))
                send.assert_not_called()

    def test_expired_or_mismatched_first_event_cannot_be_submitted(self):
        trader=self.trader();result=self.decision()
        with patch.object(base.mt5,"account_info",return_value=SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO)):
            self.assertEqual(trader.validate_strategy_entry(result),"")
            with patch.object(base.time,"time",return_value=self.now+300):
                self.assertIn("过期",trader.validate_strategy_entry(result))
            result.details["strategy_event_id"]="other"
            self.assertIn("不一致",trader.validate_strategy_entry(result))

    def test_order_is_reserved_durably_before_send_and_rejection_never_releases(self):
        trader=self.trader();result=self.decision();event=result.details["strategy_event_id"]
        def send_order(request):
            persisted=EventLedger(self.directory/"events.json")
            self.assertIn("consumed",persisted.entry_error(event,self.now))
            self.assertIn("_SR_",request["comment"])
            return SimpleNamespace(retcode=base.mt5.TRADE_RETCODE_REJECT,comment="reject")
        with patch.object(base,"ENABLE_TRADING",True), \
             patch.object(base.mt5,"account_info",return_value=SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO)), \
             patch.object(base.mt5,"positions_get",return_value=()), \
             patch.object(base.mt5,"symbol_info_tick",return_value=self.quote(result)), \
             patch.object(base.mt5,"order_send",side_effect=send_order) as send:
            self.assertFalse(trader.place_order(result))
            self.assertFalse(trader.place_order(result))
            self.assertEqual(send.call_count,1)

    def test_success_binds_frozen_deadline_and_reloads_after_restart(self):
        trader=self.trader();result=self.decision()
        with patch.object(base,"ENABLE_TRADING",True), \
             patch.object(base.mt5,"account_info",return_value=SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO)), \
             patch.object(base.mt5,"positions_get",return_value=()), \
             patch.object(base.mt5,"symbol_info_tick",return_value=self.quote(result)), \
             patch.object(base.mt5,"order_send",return_value=SimpleNamespace(retcode=base.mt5.TRADE_RETCODE_DONE,order=321,price=101.93)), \
             patch.object(trader,"sync_position_close_notifications"):
            self.assertTrue(trader.place_order(result))
        reopened=self.trader()
        self.assertEqual(reopened.strategy_ledger.due_tickets(self.now+10800),(321,))
        self.assertFalse(reopened.strategy_ledger.due_tickets(self.now+10799))

    def test_expiry_runs_even_when_normal_guardian_disabled_and_ignores_foreign_magic(self):
        trader=self.trader();trader.positions_known=True
        self.assertTrue(trader.strategy_ledger.reserve("event",self.now-10800,
                        {"max_hold_minutes":180,"cooldown_seconds":600}))
        self.assertTrue(trader.strategy_ledger.record_position(321,"event",self.now-10800,180,600))
        position=SimpleNamespace(ticket=321,magic=base.MAGIC,symbol=base.SYMBOL,comment="FIB_SR_BUY")
        with patch.object(base,"GUARDIAN_ENABLED",False),patch.object(trader,"get_positions",return_value=[position]), \
             patch.object(trader,"close_position_by_ticket",return_value=True) as close:
            trader.run_guardian();close.assert_called_once()
        position.magic=base.MAGIC+1
        with patch.object(trader,"close_position_by_ticket") as close:
            trader.guard_strategy_lifecycle([position]);close.assert_not_called()

    def test_failed_position_query_does_not_erase_saved_deadline(self):
        trader=self.trader()
        trader.strategy_ledger.reserve("event",self.now,{"max_hold_minutes":180,"cooldown_seconds":600})
        trader.strategy_ledger.record_position(123,"event",self.now,180,600)
        with patch.object(base.mt5,"positions_get",return_value=None),patch.object(base.time,"time",return_value=self.now+40):
            trader.guard_strategy_lifecycle(trader.get_positions())
        self.assertIn("123",trader.strategy_ledger.snapshot()["positions"])

    def test_confirmed_close_starts_cooldown_before_history_notification(self):
        trader=self.trader()
        trader.strategy_ledger.reserve("event",self.now,{"max_hold_minutes":180,"cooldown_seconds":600})
        trader.strategy_ledger.record_position(123,"event",self.now,180,600)
        position=SimpleNamespace(ticket=123,type=base.mt5.POSITION_TYPE_BUY,volume=.01)
        def history(*args):
            self.assertIn("cooldown",trader.strategy_ledger.entry_error("next",self.now))
            return None
        with patch.object(base,"ENABLE_TRADING",True),patch.object(base.time,"sleep"), \
             patch.object(trader,"create_position_snapshot",return_value=SimpleNamespace()), \
             patch.object(base.mt5,"symbol_info_tick",return_value=SimpleNamespace(bid=100.,ask=100.33)), \
             patch.object(base.mt5,"order_send",return_value=SimpleNamespace(retcode=base.mt5.TRADE_RETCODE_DONE)), \
             patch.object(trader,"find_recent_close_deal",side_effect=history),patch.object(trader,"notify_position_closed"):
            self.assertTrue(trader.close_position_by_ticket(position,"test"))
        self.assertNotIn("123",trader.strategy_ledger.snapshot()["positions"])
        self.assertEqual(trader.strategy_ledger.entry_error("next",self.now+600),"")

    def test_restart_recovers_uncertain_ack_using_reserved_order_time(self):
        trader=self.trader();trader.positions_known=True
        trader.strategy_ledger.reserve("event",self.now-10800,
            {"server_utc_offset":3,"max_hold_minutes":180,"cooldown_seconds":600,"action":"buy"})
        position=SimpleNamespace(ticket=432,magic=base.MAGIC,symbol=base.SYMBOL,comment="FIB_SR_BUY",type=base.mt5.POSITION_TYPE_BUY,
                                 time=int(self.now))
        with patch.object(trader,"close_position_by_ticket",return_value=True) as close:
            trader.guard_strategy_lifecycle([position])
            close.assert_called_once()
        self.assertEqual(trader.strategy_ledger.snapshot()["positions"]["432"]["max_hold_minutes"],180)

    def test_no_candidate_or_non_demo_snapshot_stops_before_paid_request(self):
        trader=self.trader(ai_mode=True);local=self.decision()
        demo=SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO)
        with patch.object(trader,"get_context",return_value=self.frames[:3]), \
             patch.object(trader,"evaluate_context",return_value=local), \
             patch.object(base.mt5,"account_info",return_value=demo),patch.object(trader,"get_live_quote") as quote:
            blocked=copy.deepcopy(local)
            for item in blocked.details["strategy_assessments"].values():
                item["blockers"]=["used event"]
            with patch.object(trader,"evaluate_context",return_value=blocked):
                with self.assertRaises(ai.MarketSnapshotError):trader.collect_entry_snapshot("d")
            with patch.object(base.mt5,"account_info",return_value=None):
                with self.assertRaises(ai.MarketSnapshotError):trader.collect_entry_snapshot("d")
            quote.assert_not_called()

    def review_fixture(self):
        proposal = self.decision()
        self.assertEqual(proposal.action,"buy",proposal.reasoning)
        pack, _, _, _ = ai.build_feature_pack(*self.frames[:3],proposal,
            quote=dict(bid=101.6,ask=101.93,time=1))
        return self.trader(ai_mode=True), proposal, pack

    def assert_review_blocked(self, trader, proposal, pack, reason, account="demo"):
        if account == "demo":
            account = SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO)
        with patch.object(trader,"get_context",return_value=self.frames[:3]), \
             patch.object(base.mt5,"account_info",return_value=account), \
             patch.object(trader,"get_live_quote") as quote,patch.object(ai.requests,"post") as post:
            # This is the same per-POST refresh callback used by production AI-2.
            with self.assertRaisesRegex(ai.MarketSnapshotError,reason):
                ai.call_chat("AI-2","offline-model","low","rules","",40,
                    "https://primary.example.test/chat","offline-key",refresh_request=lambda:
                    trader.prepare_review_request(proposal,pack,{},"fixed","offline-review","M5"))
            quote.assert_not_called()
            post.assert_not_called()
        self.assertNotIn("ai2_snapshot_id",proposal.details)

    def test_ai2_expired_confirmation_blocks_before_quote_or_paid_request_with_stalled_bars(self):
        trader,proposal,pack = self.review_fixture()
        closed = proposal.details["strategy_assessments"]["buy"]["evidence"]["retest_bar"]["close_utc"]
        with patch.object(base.time,"time",return_value=closed+300):
            self.assert_review_blocked(trader,proposal,pack,"已过期")

    def test_ai2_consumed_event_blocks_before_quote_or_paid_request(self):
        trader,proposal,pack = self.review_fixture()
        self.assertTrue(trader.strategy_ledger.reserve(proposal.details["strategy_event_id"],self.now))
        self.assert_review_blocked(trader,proposal,pack,"already consumed")

    def test_ai2_close_cooldown_blocks_before_quote_or_paid_request(self):
        trader,proposal,pack = self.review_fixture()
        self.assertTrue(trader.strategy_ledger.reserve("previous-event",self.now-60))
        self.assertTrue(trader.strategy_ledger.record_position(123,"previous-event",self.now-60,180,600))
        self.assertTrue(trader.strategy_ledger.closed_positions(set(),self.now))
        self.assert_review_blocked(trader,proposal,pack,"cooldown active")

    def test_ai2_unknown_and_real_accounts_block_before_quote_or_paid_request(self):
        for account in (None,SimpleNamespace(),SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_REAL)):
            with self.subTest(account=account):
                trader,proposal,pack = self.review_fixture()
                self.assert_review_blocked(trader,proposal,pack,"账户类型未知或实盘",account)

    def test_ai2_local11_window_end_is_exclusive_even_with_same_latest_m5(self):
        self.frames = market(late_confirmation=True)
        trader,proposal,pack = self.review_fixture()
        evidence = proposal.details["strategy_assessments"]["buy"]["evidence"]
        end = datetime.fromisoformat(next(w for w in evidence["windows"] if w["active"])["entry_end_utc"]).timestamp()
        self.assertEqual(end-evidence["retest_bar"]["close_utc"],300)
        with patch.object(base.time,"time",return_value=end):
            self.assert_review_blocked(trader,proposal,pack,"已过期")

    def test_ai2_valid_research_and_legacy_contracts_keep_existing_review_path(self):
        trader,proposal,pack = self.review_fixture()
        for contract in (proposal.details["strategy_contract"],{},
                         {"id":"custom","entry_reference":"closed_m15","one_entry_per_event":False}):
            with self.subTest(contract=contract):
                candidate = copy.deepcopy(proposal)
                candidate.details["strategy_contract"] = contract
                with patch.object(trader,"get_context",return_value=self.frames[:3]), \
                     patch.object(base.mt5,"account_info",return_value=SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO)) as account, \
                     patch.object(trader,"get_live_quote",return_value=pack["live_quote"]) as quote, \
                     patch.object(ai,"append_decision_snapshot"):
                    request = trader.prepare_review_request(candidate,pack,{},"fixed","offline-review","M5")
                    self.assertIn("prompt",request)
                    quote.assert_called_once()
                    if not contract.get("demo_only"):
                        account.assert_not_called()

    def test_ai2_backup_refresh_stops_when_event_expires_after_first_post(self):
        trader,proposal,pack = self.review_fixture()
        closed = proposal.details["strategy_assessments"]["buy"]["evidence"]["retest_bar"]["close_utc"]
        with patch.dict(os.environ,{"AI2_BACKUP_URL":"https://backup.example.test/chat"}), \
             patch.object(trader,"get_context",return_value=self.frames[:3]), \
             patch.object(base.mt5,"account_info",return_value=SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO)), \
             patch.object(trader,"get_live_quote",return_value=pack["live_quote"]) as quote, \
             patch.object(base.time,"time",return_value=self.now) as clock, \
             patch.object(ai,"append_decision_snapshot"),patch.object(ai,"AI_USAGE_DIR",self.temp.name), \
             patch.object(ai.time,"sleep"):
            def timeout(*args,**kwargs):
                clock.return_value = closed+300
                raise ai.requests.ReadTimeout("offline timeout")
            with patch.object(ai.requests,"post",side_effect=timeout) as post:
                with self.assertRaisesRegex(ai.MarketSnapshotError,"已过期"):
                    ai.call_chat("AI-2","offline-model","low","rules","",40,
                        "https://primary.example.test/chat","offline-key",refresh_request=lambda:
                        trader.prepare_review_request(proposal,pack,{},"fixed","offline-review","M5"))
                post.assert_called_once()
                quote.assert_called_once()

    def test_startup_log_reports_each_enabled_contract_reference_policy(self):
        trader = self.trader(ai_mode=True)
        trader._stop.set()  # Exercise startup logging without entering the trading loop.
        snapshot = SimpleNamespace(fingerprint="offline",error="",strategies=[
            SimpleNamespace(id="session_retest",enabled=True,entry_reference="closed_m5"),
            SimpleNamespace(id="legacy",enabled=True,entry_reference="closed_m15"),
            SimpleNamespace(id="disabled",enabled=False,entry_reference="closed_m15")])
        with patch.object(trader,"connect_mt5",return_value=True), \
             patch.object(trader,"reconcile_entry_contexts"),patch.object(trader,"notify_lines"), \
             patch.object(ai,"attach_ai_logger",return_value="offline.log"), \
             patch.object(base.STRATEGIES,"snapshot",return_value=snapshot), \
             patch.object(base.mt5,"shutdown"),patch.object(base.logger,"info") as log:
            trader.run(once=True)
        reference_logs = [call for call in log.call_args_list if "参考入场" in call.args[0]]
        self.assertEqual(len(reference_logs),1)
        message = reference_logs[0].args[0]
        self.assertIn("按启用策略契约",message)
        self.assertIn("'session_retest': 'closed_m5'",message)
        self.assertIn("'legacy': 'closed_m15'",message)
        self.assertNotIn("disabled",message)

    def test_tick_clock_requires_fresh_observed_timestamp_and_context_gets80_m5(self):
        self.assertEqual(base.tick_clock_offset(SimpleNamespace(time=self.now+10800),self.now),3)
        self.assertIsNone(base.tick_clock_offset(SimpleNamespace(time=self.now+10700),self.now))
        trader=self.trader()
        with patch.object(trader,"get_rates",return_value=self.frames[0]) as rates:
            trader.get_context()
        self.assertIn(((base.mt5.TIMEFRAME_M5,80),{"shift":1}),[(c.args,c.kwargs) for c in rates.call_args_list])


if __name__ == "__main__":
    unittest.main()
