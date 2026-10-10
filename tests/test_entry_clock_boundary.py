"""Clock-boundary scheduling regressions; every terminal/HTTP/notification path mocked."""
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import xauusd_fib_volume_pa_ai3 as ai
from strategies.framework import Snapshot
from test_session_retest_integration import market

base = ai.base
logging.disable(logging.CRITICAL)


class EntryClockBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        original = base.STRATEGIES.snapshot()
        legacy_snapshot = Snapshot(original.fingerprint, tuple(
            replace(strategy, enabled=strategy.id == "session_retest") for strategy in original.strategies))
        registry_patch = patch.object(base.STRATEGIES, "snapshot", return_value=legacy_snapshot)
        registry_patch.start(); self.addCleanup(registry_patch.stop)
        self.frames = market()
        self.boundary = self.frames[-1].timestamp()
        self.m5 = int(self.boundary+10800)
        self.m15 = self.m5//900*900
        self.env = patch.dict(os.environ,{"FIB_PA_STRATEGY_STATE_FILE":str(Path(self.temp.name)/"events.json")})
        self.env.start(); self.addCleanup(self.env.stop)

    def tick(self, observed, lag=0):
        return SimpleNamespace(time=int(observed+10800-lag),bid=101.6,ask=101.93)

    def test_pure_readiness_waits_minus2_minus1_without_accepting_future(self):
        for skew in (-2,-1,0,1):
            observed = self.boundary+skew
            facts = base.closed_entry_readiness(self.m15,self.m5,self.tick(observed),observed)
            self.assertEqual(facts["ready"],skew >= 0)
            self.assertEqual(facts["m5_boundary_utc"],self.boundary)
            self.assertEqual(facts["server_utc_offset"],3)
        facts = base.closed_entry_readiness(self.m15,self.m5+300,self.tick(self.boundary),self.boundary)
        self.assertFalse(facts["ready"])
        self.assertEqual(facts["reason"],"local_clock_before_closed_entry_boundary")

    def test_strategy_still_rejects_future_confirmation_before_exact_close(self):
        for skew in (-2,-1,0,1):
            result = base.build_fib_volume_pa_decision(*self.frames[:3],55,
                now=datetime.fromtimestamp(self.boundary+skew,base.BEIJING_TZ),server_utc_offset=3)
            if skew < 0:
                self.assertEqual(result.action,"no")
                blockers = result.details["strategy_assessments"]["buy"]["blockers"]
                self.assertIn("unclosed_or_future_m5_evidence",blockers)
            else:self.assertEqual((result.action,result.confidence),("buy",85))

    def test_unknown_stale_invalid_tick_and_marker_clocks_fail_closed(self):
        for tick in (None,SimpleNamespace(),SimpleNamespace(time=float("nan")),
                     self.tick(self.boundary,31),self.tick(self.boundary,-31)):
            facts = base.closed_entry_readiness(self.m15,self.m5,tick,self.boundary)
            self.assertFalse(facts["ready"])
            self.assertEqual(facts["reason"],"unknown_or_stale_tick_clock")
        for marker in (0,None,float("nan"),float("inf"),True,self.m5+.5):
            facts = base.closed_entry_readiness(self.m15,marker,self.tick(self.boundary),self.boundary)
            self.assertFalse(facts["ready"])
            self.assertEqual(facts["reason"],"invalid_current_bar_marker")
        for observed in (None,"bad",float("nan"),float("inf"),0,True):
            facts = base.closed_entry_readiness(self.m15,self.m5,self.tick(self.boundary),observed)
            self.assertFalse(facts["ready"])
            self.assertEqual(facts["reason"],"invalid_observed_clock")

    def test_readiness_keeps_existing_offset_tolerance_and_legacy_marker_path(self):
        for offset in (0,3,5.5,-4):
            tick = SimpleNamespace(time=self.boundary+offset*3600)
            facts = base.closed_entry_readiness(self.boundary+offset*3600-600,
                self.boundary+offset*3600,tick,self.boundary)
            self.assertTrue(facts["ready"])
            self.assertEqual(facts["server_utc_offset"],offset)
        self.assertIsNone(base.tick_clock_offset(self.tick(self.boundary,31),self.boundary))

    def run_loop(self, ai_mode, once, skews, ticks=None, marker_delta=0, evaluate_m5=False, strategy_snapshot=None):
        index = [0]
        observed = lambda: self.boundary+skews[index[0]]
        results, guardians, closes, ai3, m5_calls = [], [], [], [], []
        with patch.object(ai,"AI3_STATE_FILE",str(Path(self.temp.name)/"ai3.json")):
            trader = ai.ThreeAITrader() if ai_mode else base.FibVolumePATrader()
        original_ledger = trader.strategy_ledger.snapshot()
        claims = []
        def evaluate(*args,**kwargs):
            result = base.build_fib_volume_pa_decision(*self.frames[:3],55,
                now=datetime.fromtimestamp(observed(),base.BEIJING_TZ),server_utc_offset=3,
                strategy_snapshot=strategy_snapshot)
            results.append((observed(),result))
            return result
        def poll(*args):
            index[0] += 1
            if index[0] >= len(skews):
                raise KeyboardInterrupt()
        def raw_marker(timeframe):
            if timeframe == base.mt5.TIMEFRAME_M5:
                delta = marker_delta[index[0]] if isinstance(marker_delta,list) else marker_delta
                return self.m5+delta
            if timeframe == base.mt5.TIMEFRAME_M1:
                return self.m5
            return self.m15
        def tick(*args):
            return ticks[index[0]] if ticks is not None else self.tick(observed())
        with ExitStack() as scope:
            def mock(target,name,**kwargs):
                return scope.enter_context(patch.object(target,name,**kwargs))
            mock(trader,"connect_mt5",return_value=True)
            mock(trader,"run_guardian",side_effect=lambda:guardians.append(observed()))
            mock(trader,"sync_position_close_notifications",side_effect=lambda:closes.append(observed()))
            mock(base.time,"time",side_effect=observed)
            mock(base.time,"sleep",side_effect=poll)
            mock(base.mt5,"symbol_info_tick",side_effect=tick)
            shutdown = mock(base.mt5,"shutdown")
            initialize = mock(base.mt5,"initialize")
            order = mock(base.mt5,"order_send")
            post = mock(ai.requests,"post",side_effect=AssertionError("real HTTP forbidden"))
            notice = mock(base,"send_feishu_notification")
            log = mock(base.logger,"info")
            readiness = mock(trader,"entry_clock_ready",wraps=trader.entry_clock_ready)
            if ai_mode:
                mock(trader,"latest_bar_time",side_effect=raw_marker)
                mock(trader,"reconcile_entry_contexts")
                mock(trader,"notify_lines")
                mock(ai,"attach_ai_logger",return_value="offline.log")
                def claim(*args):
                    claims.append(observed())
                    return 1
                mock(trader,"claim_task",side_effect=claim)
                mock(trader,"entry_task",side_effect=evaluate)
                mock(trader,"dispatch_entry",side_effect=evaluate)
                def m5_entry(bar):
                    m5_calls.append((observed(),bar))
                    if evaluate_m5:evaluate()
                mock(trader,"dispatch_m5_entry",side_effect=m5_entry)
                mock(trader,"dispatch_ai3",side_effect=lambda bar:ai3.append((observed(),bar)))
            else:
                mock(base.mt5,"copy_rates_from_pos",side_effect=lambda symbol,tf,*args:[{"time":raw_marker(tf)}])
                mock(trader,"evaluate_once",side_effect=evaluate)
            self.assertEqual(trader.run(once=once),0)
            initialize.assert_not_called(); order.assert_not_called(); post.assert_not_called(); notice.assert_not_called()
            shutdown.assert_called_once()
            deferred = [call for call in log.call_args_list if call.args[0].startswith("入场收盘等待=")]
            self.last_readiness_checks = readiness.call_count
            self.last_claim_times = claims
            self.assertEqual(trader.strategy_ledger.snapshot(),original_ledger)
        return results,guardians,closes,ai3,m5_calls,deferred

    def test_base_and_ai_once_recheck_same_raw_bar_after_boundary_without_consuming_it(self):
        for ai_mode in (False,True):
            with self.subTest(ai_mode=ai_mode):
                results,guardian,closes,_,_,deferred = self.run_loop(ai_mode,True,[-2,-1,-1,0])
                self.assertEqual(len(results),1)
                self.assertEqual(results[0][0],self.boundary)
                self.assertEqual((results[0][1].action,results[0][1].confidence),("buy",85))
                self.assertEqual(len(guardian),4); self.assertEqual(len(closes),4)
                self.assertEqual(len(deferred),1)
                self.assertEqual(deferred[0].args[3:5],(self.m15,self.m5))
                if ai_mode:self.assertEqual(self.last_claim_times,[self.boundary])

    def test_normal_loops_wait_then_evaluate_once_and_ai3_runs_while_deferred(self):
        for ai_mode in (False,True):
            with self.subTest(ai_mode=ai_mode):
                results,guardian,closes,ai3,m5_calls,deferred = self.run_loop(ai_mode,False,[-2,-1,0,1])
                self.assertEqual(len(results),1)
                self.assertEqual((results[0][1].action,results[0][1].confidence),("buy",85))
                self.assertEqual(len(guardian),4); self.assertEqual(len(closes),4)
                self.assertEqual(len(deferred),1)
                if ai_mode:
                    self.assertEqual(ai3,[(self.boundary-2,self.m5)])
                    self.assertEqual(m5_calls,[(self.boundary,self.m5)])

    def test_unknown_or_stale_clock_does_not_consume_bar_before_clock_recovers(self):
        for ai_mode in (False,True):
            with self.subTest(ai_mode=ai_mode):
                ticks = [None,self.tick(self.boundary,31),self.tick(self.boundary)]
                results,guardian,_,_,_,deferred = self.run_loop(ai_mode,True,[0,0,0],ticks=ticks)
                self.assertEqual(len(results),1)
                self.assertEqual(results[0][1].action,"buy")
                self.assertEqual(len(guardian),3)
                self.assertEqual(len(deferred),1)

    def test_genuinely_future_current_bar_stays_unconsumed_in_both_loops(self):
        for ai_mode in (False,True):
            with self.subTest(ai_mode=ai_mode):
                results,guardian,_,ai3,m5_calls,deferred = self.run_loop(ai_mode,False,[0,1,2],marker_delta=300)
                self.assertEqual(results,[]); self.assertEqual(m5_calls,[])
                self.assertEqual(len(guardian),3); self.assertEqual(len(deferred),1)
                if ai_mode:self.assertTrue(ai3)

    def test_only_new_m5_waits_then_rechecks_same_marker_with_m15_already_processed(self):
        for ai_mode in (False,True):
            with self.subTest(ai_mode=ai_mode):
                results,guardian,_,_,m5_calls,deferred = self.run_loop(ai_mode,False,[-2,-1,0,1],
                    marker_delta=[-600,0,0,0],evaluate_m5=True)
                self.assertEqual(len(results),2)
                self.assertEqual(results[0][1].action,"no")
                self.assertEqual((results[1][0],results[1][1].action,results[1][1].confidence),
                                 (self.boundary,"buy",85))
                self.assertEqual(len(guardian),4); self.assertEqual(len(deferred),1)
                if ai_mode:self.assertEqual(m5_calls,[(self.boundary,self.m5)])

    def test_m15_covers_same_m5_marker_without_repeated_readiness_or_dispatch(self):
        self.boundary += 300
        self.m5 += 300
        self.m15 = self.m5
        results,_,_,_,m5_calls,deferred = self.run_loop(True,False,[0,1,2])
        self.assertEqual(len(results),1)
        self.assertEqual(m5_calls,[]); self.assertEqual(deferred,[])
        self.assertEqual(self.last_readiness_checks,1)

    def test_settled_legacy_contract_still_evaluates_with_m15_reference(self):
        legacy = replace(base.STRATEGIES.snapshot().get("pullback"),enabled=True)
        snapshot = Snapshot("offline-legacy",(legacy,))
        for ai_mode in (False,True):
            with self.subTest(ai_mode=ai_mode):
                results,_,_,_,_,deferred = self.run_loop(ai_mode,True,[1],strategy_snapshot=snapshot)
                self.assertEqual(len(results),1); self.assertEqual(deferred,[])
                contract = results[0][1].details["strategy_contract"]
                self.assertEqual(contract["id"],"pullback")
                self.assertEqual(contract["entry_reference"],"closed_m15")
                self.assertFalse(contract["demo_only"])

    def test_m5_no_action_log_exposes_both_strategy_blockers_without_notification(self):
        with patch.object(ai,"AI3_STATE_FILE",str(Path(self.temp.name)/"ai3.json")):
            trader = ai.ThreeAITrader()
        result = base.StrategyResult("no",0,False,"selected outsidewindow",0,0,0,0,
            {"strategy_assessments":{"buy":{"blockers":["outsidewindow","H1 mismatch"]},
                                     "sell":{"blockers":["first_breakout_opposite_action","unclosed_or_future_m5_evidence"]}}})
        with patch.object(trader,"get_context",return_value=self.frames[:3]), \
             patch.object(trader,"evaluate_context",return_value=result),patch.object(base.logger,"info") as log, \
             patch.object(trader,"dispatch_entry") as dispatch,patch.object(base,"send_feishu_notification") as notice:
            trader.dispatch_m5_entry(self.m5)
        message = log.call_args.args[0]
        for text in ("outsidewindow","H1 mismatch","first_breakout_opposite_action","unclosed_or_future_m5_evidence"):
            self.assertIn(text,message)
        dispatch.assert_not_called(); notice.assert_not_called()


if __name__ == "__main__":
    unittest.main()
