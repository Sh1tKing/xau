"""Strategy lifecycle, frozen AI contracts and final order gates; entirely offline."""
import copy
from dataclasses import replace
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai
import xauusd_fib_pa_backtest as backtest
from strategies.framework import Assessment, Context, Registry, StrategyError, DEFAULT_REGISTRY

base = ai.base


def definition(strategy_id="custom", score=90, target=1.8, enabled=True, blockers=()):
    return f'''from strategies.framework import Strategy, Assessment
def evaluate(c):
    return Assessment({score} if c.action == "buy" else 0, {{}}, evidence={{"source": "custom", "bar_time": int(c.df_m15.time.iloc[-1])}}, blockers={blockers!r})
STRATEGY = Strategy(id={strategy_id!r}, name="测试策略", version="1", sessions={{"asia":0,"london":0,"overlap":0,"late_us":0}}, evaluate=evaluate, target_r={target}, enabled={enabled}, m5_boost=False, ai_rules={{"entry":"测试入场","review":"测试审核","guardian":"旧版本护盘"}})
'''


def frame(n=80):
    return base.pd.DataFrame([dict(time=1800000000+i*900, open=100., high=101., low=99.,
                                  close=100., tick_volume=100, spread=10) for i in range(n)])


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "custom.py"
        self.path.write_text(definition(), encoding="utf-8")
        self.registry = Registry(self.directory)
        self.market = frame()
        self.context = Context("buy", self.market, self.market, self.market, None, 1., "bull", "bull", 20., "asia", base.strategy_api())
        self.now = datetime(2026, 10, 9, 10, tzinfo=base.BEIJING_TZ)

    def decision(self, **kwargs):
        with patch.object(base, "STRATEGIES", self.registry), \
             patch.object(base, "calculate_atr", return_value=1.), \
             patch.object(base, "trend_from_ema", return_value="bull"), \
             patch.object(base, "entry_fib_structure", return_value=None), \
             patch.object(base, "classify_market_regime", return_value=("trend", "趋势", {"location": .5})):
            return base.build_fib_volume_pa_decision(self.market, self.market, self.market, 55, now=self.now, **kwargs)

    def trader(self):
        with patch.object(ai, "AI3_STATE_FILE", str(self.directory / "state.json")):
            trader = ai.ThreeAITrader()
        self.addCleanup(patch.stopall)
        return trader

    def test_auto_add_remove_and_source_hash_reload_even_same_mtime_size(self):
        first = self.registry.snapshot()
        self.assertFalse(first.error)
        self.assertIs(first, self.registry.snapshot())
        original_stat = self.path.stat()
        self.path.write_text(definition(target=3.0), encoding="utf-8")
        os.utime(self.path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        self.assertEqual(original_stat.st_size, self.path.stat().st_size)
        second = self.registry.snapshot()
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(second.get("custom").target_r, 3.)
        (self.directory / "second.py").write_text(definition("second"), encoding="utf-8")
        self.assertEqual(len(self.registry.snapshot().active("asia")), 2)
        self.path.unlink()
        self.assertEqual([s.id for s in self.registry.snapshot().active("asia")], ["second"])

    def test_syntax_error_never_uses_last_good_version_and_fix_recovers(self):
        old = self.registry.snapshot()
        self.path.write_text("STRATEGY = (", encoding="utf-8")
        self.assertTrue(self.registry.snapshot().error)
        self.assertFalse(self.registry.snapshot().strategies)
        with self.assertRaises(StrategyError):
            self.registry.require_current(old.fingerprint)
        self.assertIn("strategy_error", self.decision().details)
        self.path.write_text(definition(), encoding="utf-8")
        self.assertFalse(self.registry.snapshot().error)

    def test_duplicate_id_and_invalid_declarations_fail_closed(self):
        for text in (definition(target=8.), definition().replace('"review":"测试审核",', ''),
                     definition().replace('sessions={"asia":0', 'sessions={"typo":0'),
                     definition().replace('evaluate=evaluate', 'evaluate=None')):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                self.assertTrue(self.registry.snapshot().error)
        self.path.write_text(definition(), encoding="utf-8")
        (self.directory / "duplicate.py").write_text(definition(), encoding="utf-8")
        self.assertIn("重复", self.registry.snapshot().error)

    def test_disabled_or_missing_session_stops_new_ai_requests(self):
        self.path.write_text(definition(enabled=False), encoding="utf-8")
        trader = self.trader()
        with patch.object(base, "STRATEGIES", self.registry), \
             patch.object(trader, "get_context", return_value=(self.market,)*3), \
             patch.object(trader, "get_live_quote") as quote, patch.object(ai.requests, "post") as post:
            with self.assertRaises(ai.MarketSnapshotError):
                trader.collect_entry_snapshot("d")
        post.assert_not_called()
        quote.assert_not_called()

    def test_new_id_selected_without_core_setup_switch_or_prompt_edits(self):
        result = self.decision()
        self.assertEqual(result.action, "buy", result.reasoning)
        self.assertEqual(result.details["setup"], "custom")
        self.assertEqual(result.details["strategy_contract"]["ai_rules"]["entry"], "测试入场")
        pack, _, _, _ = ai.build_feature_pack(self.market, self.market, self.market, result)
        self.assertEqual(pack["strategy_evidence"]["buy"]["evidence"]["source"], "custom")
        self.assertEqual(pack["strategy_fingerprint"], self.registry.snapshot().fingerprint)
        self.assertFalse(pack["rules"]["pullback_tight_range_is_no"])

    def test_strategy_blockers_are_enforced_locally_and_for_ai_autonomous_proposals(self):
        self.path.write_text(definition(blockers=("未完成回踩",)), encoding="utf-8")
        result = self.decision()
        self.assertEqual(result.action, "no")
        self.assertIn("未完成回踩", result.reasoning)
        payload = dict(action="buy", confidence=90, stop_loss=98, take_profit=104,
                       trigger_evidence="触发", invalidation="失效", target_basis="目标")
        proposal, reason = ai.finalize_ai_proposal(payload, 100, 1, "bull", "bull", None,
                        setup="custom", strategy_contract=result.details["strategy_contract"],
                        strategy_evidence=result.details["strategy_assessments"])
        self.assertIsNone(proposal)
        self.assertIn("未完成回踩", reason)

    def test_target_r_is_shared_by_local_plan_ai_clamp_and_prompt(self):
        self.path.write_text(definition(target=3.0), encoding="utf-8")
        result = self.decision()
        self.assertEqual(result.action, "buy")
        self.assertAlmostEqual((result.take_profit-100)/(100-result.stop_loss), 3.)
        pack, *_ = ai.build_feature_pack(self.market, self.market, self.market, result)
        self.assertEqual(pack["rules"]["price_finalization"]["target_max_reference_r"], 3.)
        self.assertEqual(ai.clamp_proposal("buy",100,98,120,1,target_r=3.), (98.,106.))
        self.assertEqual(ai.clamp_proposal("buy",100,90,120,1,target_r=3.), (0.,0.))
        self.assertEqual(base.calculate_targets("buy",100,1,None,target_r=8), (0.,0.))

    def test_bad_assessment_or_runtime_failure_blocks_entry(self):
        for expression in ('None', 'Assessment(101, {})', 'Assessment(90, {}, evidence={"price":float("nan")})',
                           'Assessment(90, {}, blockers=["bad"])', '1/0'):
            self.path.write_text(definition().replace('return Assessment(90 if c.action == "buy" else 0, {}, evidence={"source": "custom", "bar_time": int(c.df_m15.time.iloc[-1])}, blockers=())', f'return {expression}'), encoding="utf-8")
            with self.subTest(expression=expression):
                result = self.decision()
                self.assertEqual(result.action, "no")
                self.assertIn("strategy_error", result.details)

    def test_declared_trigger_must_reference_real_closed_input_bar(self):
        strategy=self.registry.snapshot().get("custom")
        for timeframe, stamp in (("M15",int(self.market.time.iloc[-1])), ("M1",int(self.market.time.iloc[-1])), ("M15",123)):
            rule=replace(strategy, evaluate=lambda c: Assessment(90, {}, evidence={"closed_trigger":{"confirmed":True,"timeframe":timeframe,"bar_time":stamp}}))
            if timeframe=="M15" and stamp!=123:
                evidence=rule.assess(self.context).evidence
                self.assertTrue(ai.has_confirmed_trigger({"strategy_evidence":{"buy":{"evidence":evidence}}},"buy"))
            else:
                with self.assertRaises(StrategyError):
                    rule.assess(self.context)

    def test_assessment_json_is_frozen_and_parameter_mutation_is_rejected(self):
        strategy=self.registry.snapshot().get("custom")
        mutable={"price":100.}
        rule=replace(strategy,evaluate=lambda c:Assessment(90,{},evidence=mutable))
        result=rule.assess(self.context)
        mutable["price"]=200.
        self.assertEqual(result.evidence["price"],100.)
        def bad(c):
            rule.parameters["changed"]=True
            return Assessment(90,{})
        rule=replace(strategy,evaluate=bad,parameters={})
        with self.assertRaises(StrategyError):
            rule.assess(self.context)

    def test_wrong_h1_still_blocks_new_custom_strategy(self):
        with patch.object(base,"STRATEGIES",self.registry), patch.object(base,"calculate_atr",return_value=1.), \
             patch.object(base,"trend_from_ema",return_value="bear"), patch.object(base,"entry_fib_structure",return_value=None):
            result=base.build_fib_volume_pa_decision(self.market,self.market,self.market,55,now=self.now)
        self.assertEqual(result.action,"no")
        self.assertIn("H1",result.reasoning)

    def test_edited_strategy_stops_ai2_before_new_quote_or_post(self):
        fingerprint=self.registry.snapshot().fingerprint
        self.path.write_text(definition(score=80),encoding="utf-8")
        trader=self.trader()
        with patch.object(base,"STRATEGIES",self.registry), patch.object(trader,"get_live_quote") as quote, patch.object(ai.requests,"post") as post:
            with self.assertRaises(ai.MarketSnapshotError):
                trader.prepare_review_request(None,{"strategy_fingerprint":fingerprint},None,"","d","M15")
        quote.assert_not_called();post.assert_not_called()

    def test_edit_while_ai1_generates_stops_before_quality_or_ai2(self):
        trader=self.trader()
        quote=dict(bid=100.,ask=100.1,time=1,observed_at=1.)
        def delayed(*args,**kwargs):
            kwargs["refresh_request"]()
            self.path.write_text(definition(score=80),encoding="utf-8")
            return '{"action":"buy"}'
        with patch.object(base,"STRATEGIES",self.registry), patch.object(base,"calculate_atr",return_value=1.), \
             patch.object(base,"trend_from_ema",return_value="bull"), patch.object(base,"entry_fib_structure",return_value=None), \
             patch.object(trader,"get_context",return_value=(self.market,)*3), patch.object(trader,"get_live_quote",return_value=quote), \
             patch.object(trader,"entry_is_current",return_value=True), patch.object(trader,"release_task"), \
             patch.object(ai,"append_decision_snapshot") as snapshots, patch.object(ai,"normalize_ai1_quality") as normalize, \
             patch.object(ai,"call_chat",side_effect=delayed) as chat, patch.object(trader,"place_order") as place:
            trader.entry_task(1,1)
        self.assertEqual(chat.call_count,1)
        normalize.assert_not_called();place.assert_not_called()
        self.assertIn("entry_stopped",[call.args[1] for call in snapshots.call_args_list])

    def test_stale_strategy_stops_order_before_any_mt5_read(self):
        result=self.decision()
        self.path.write_text(definition(score=80),encoding="utf-8")
        trader=base.FibVolumePATrader()
        with patch.object(base,"STRATEGIES",self.registry), patch.object(base.mt5,"symbol_info_tick") as tick, patch.object(base.mt5,"order_send") as send, patch.object(trader,"current_position_count") as positions:
            self.assertFalse(trader.place_order(result))
        tick.assert_not_called();send.assert_not_called();positions.assert_not_called()

    def test_version_checked_again_immediately_before_order_send(self):
        result=self.decision()
        trader=base.FibVolumePATrader()
        def change_during_quote(*args):
            self.path.write_text(definition(score=80),encoding="utf-8")
            return SimpleNamespace(bid=100.,ask=100.,time=1)
        with patch.object(base,"STRATEGIES",self.registry), patch.object(base,"ENABLE_TRADING",True), \
             patch.object(trader,"current_position_count",return_value=0), \
             patch.object(base.mt5,"symbol_info_tick",side_effect=change_during_quote), \
             patch.object(base.mt5,"order_send") as send:
            self.assertFalse(trader.place_order(result))
        send.assert_not_called()

    def test_entry_context_keeps_old_strategy_contract_after_edit(self):
        trader=self.trader();result=self.decision()
        position=SimpleNamespace(ticket=123,identifier=123,symbol="XAUUSD",type=base.mt5.POSITION_TYPE_BUY,price_open=100.,sl=result.stop_loss,tp=result.take_profit)
        with patch.object(trader,"save_entry_contexts"):
            trader.record_entry_context(result,position)
        saved=copy.deepcopy(trader.entry_contexts["123"])
        result.details["strategy_contract"]["ai_rules"]["guardian"]="变更"
        self.path.write_text(definition().replace('旧版本护盘','新版本护盘'),encoding="utf-8")
        self.registry.snapshot()
        self.assertEqual(trader.entry_contexts["123"],saved)
        self.assertEqual(saved["strategy_contract"]["ai_rules"]["guardian"],"旧版本护盘")

    def test_broken_new_definition_does_not_block_ai3_old_position_guardian(self):
        trader=self.trader();result=self.decision()
        position=SimpleNamespace(ticket=123,identifier=123,symbol="XAUUSD",type=base.mt5.POSITION_TYPE_BUY,
                                 price_open=100.,sl=result.stop_loss,tp=result.take_profit,time=1800000000,volume=.01,profit=0.)
        with patch.object(trader,"save_entry_contexts"):
            trader.record_entry_context(result,position)
        self.path.write_text('STRATEGY = (',encoding="utf-8")
        self.assertTrue(self.registry.snapshot().error)
        captured={}
        def prompt(_,payload):
            captured.update(payload)
            return json.dumps(payload)
        with patch.object(base,"STRATEGIES",self.registry), patch.object(self.registry,"snapshot") as load, \
             patch.object(ai,"AI3_STATE_FILE",str(self.directory/'state.json')), \
             patch.object(trader,"get_positions",return_value=[position]), patch.object(trader,"get_rates",return_value=self.market), \
             patch.object(base.mt5,"symbol_info_tick",return_value=SimpleNamespace(bid=100.,ask=100.1,time=1800003600)), \
             patch.object(ai,"prompt_json",side_effect=prompt), patch.object(ai,"call_chat",return_value='{"action":"hold","ticket":123}') as chat, \
             patch.object(trader,"ai3_is_current",return_value=True), patch.object(trader,"release_task"), patch.object(trader,"execute_ai3"):
            trader.ai3_task(1,1)
        load.assert_not_called()
        self.assertEqual(chat.call_count,1)
        self.assertEqual(captured["positions"][0]["entry_context"]["strategy_contract"]["ai_rules"]["guardian"],"旧版本护盘")

    def test_compaction_keeps_single_manifest_and_does_not_mutate_evidence(self):
        result=self.decision()
        pack,*_=ai.build_feature_pack(self.market,self.market,self.market,result)
        before=copy.deepcopy(pack)
        compact=ai.compact_market_pack(pack)
        self.assertEqual(pack,before)
        self.assertNotIn("strategy_contract",compact["details"])
        self.assertNotIn("strategy_assessments",compact["details"])
        self.assertEqual(compact["strategy_evidence"],pack["strategy_evidence"])

    def test_builtin_priority_and_disabled_example(self):
        snapshot=DEFAULT_REGISTRY.snapshot()
        self.assertFalse(snapshot.error)
        self.assertEqual([s.id for s in snapshot.active("asia")],["pullback","range_edge"])
        self.assertEqual([s.id for s in snapshot.active("overlap")],["breakout","pullback"])
        for strategy in snapshot.strategies:
            self.assertEqual(strategy.target_r,1.8)
        shutil.copyfile(Path(__file__).resolve().parents[1]/'docs/strategies/strategy_template.py',self.directory/'template.py')
        self.assertFalse(self.registry.snapshot().error)
        self.assertNotIn("my_setup",[s.id for s in self.registry.snapshot().active("asia")])

    def test_builtin_evidence_follows_edited_parameters_and_remains_pinned(self):
        origin=Path(__file__).resolve().parents[1]/'strategies/definitions/range_edge.py'
        self.path.write_text(origin.read_text(encoding="utf-8").replace('lookback=24','lookback=12'),encoding="utf-8")
        snapshot=self.registry.snapshot();strategy=snapshot.get("range_edge")
        assessment=strategy.assess(self.context)
        self.assertEqual(len(assessment.evidence["window_bar_times"]),12)
        result=base.StrategyResult("no",0,False,"",0,0,0,0,{"setup":"range_edge","strategy_contract":strategy.manifest(),"strategy_assessments":{"buy":{"score":0,"evidence":assessment.evidence,"blockers":[]}}})
        self.path.write_text(origin.read_text(encoding="utf-8").replace('lookback=24','lookback=10'),encoding="utf-8")
        self.registry.snapshot()
        with patch.object(base,"trend_from_ema",return_value="bull"):
            pack,*_=ai.build_feature_pack(self.market,self.market,self.market,result)
        self.assertEqual(len(pack["computed_evidence"]["range_edge"]["window_bar_times"]),12)
        self.assertEqual(pack["strategy_contract"]["parameters"]["lookback"],12)

    def test_backtest_freezes_one_definition_for_entire_run(self):
        data={name:self.market for name in backtest.FRAMES}
        meta=dict(server_offset=3,point=.01,contract_size=100,spread_now=10)
        with patch.object(base,"STRATEGIES",self.registry):
            test=backtest.Backtest(data,meta,55)
        fingerprint=test.strategy_snapshot.fingerprint
        self.path.write_text(definition(score=80),encoding="utf-8")
        self.assertNotEqual(fingerprint,self.registry.snapshot().fingerprint)
        result=self.decision(strategy_snapshot=test.strategy_snapshot)
        self.assertEqual(result.confidence,90)
        self.assertEqual(result.details["strategy_fingerprint"],fingerprint)


if __name__ == "__main__":
    unittest.main()
