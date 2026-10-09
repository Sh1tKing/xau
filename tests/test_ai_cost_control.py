"""Cost and transport regressions; no live API, terminal, or trading process."""
import copy
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai


class Response:
    def __init__(self, content='{"action":"no"}', status=200, body="", finish="stop", usage=None):
        self.status_code, self.text = status, body
        self.headers = {"x-request-id": "request-123"}
        self.result = {"id": "response-456", "model": "returned-model", "usage": usage,
                       "choices": [{"finish_reason": finish, "message": {"content": content}}]}

    def json(self):
        return self.result


class CostControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for scope in (patch.dict(ai.os.environ, {}, clear=True),
                      patch.dict(ai._CHAT_OPTIONS, {}, clear=True),
                      patch.object(ai, "AI_USAGE_DIR", self.temp.name),
                      patch.object(ai, "AI_READ_TIMEOUT_RETRIES", 0),
                      patch.object(ai, "AI_HTTP_RETRIES", 5),
                      patch.object(ai.time, "sleep")):
            scope.start()
            self.addCleanup(scope.stop)

    def call(self, role="AI-1", model="claude-test", **kwargs):
        return ai.call_chat(role, model, "medium", "instructions", "market snapshot", 40,
                            "https://primary.example.test/v1/chat/completions", "SECRET-KEY", **kwargs)

    def events(self):
        return [json.loads(line) for path in Path(self.temp.name).glob("*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()]

    def backup(self):
        ai.os.environ.update({"AI1_BACKUP_URL": "https://backup.example.test/chat/completions",
                              "AI1_BACKUP_KEY": "BACKUP-SECRET"})

    def test_read_timeout_is_one_post_with_unknown_usage(self):
        with patch.object(ai.requests, "post", side_effect=ai.requests.ReadTimeout("read timed out")) as post:
            with self.assertRaises(ai.AIRequestError) as caught:
                self.call(request_context={"decision_id": "d1", "trigger": "M5", "bar_time": 123})
        self.assertEqual(post.call_count, 1)
        self.assertIn("停止本通道", caught.exception.detail)
        event = self.events()[0]
        self.assertEqual(event["outcome"], "read_timeout")
        self.assertEqual((event["decision_id"], event["trigger"]), ("d1", "M5"))
        self.assertIsNone(event["usage"])
        self.assertIsNone(event["cost"])

    def test_each_backup_can_have_its_own_timeout_and_blank_inherits(self):
        for role, number in (("AI-1", 1), ("AI-2", 2), ("AI-3", 3)):
            name = f"FIB_PA_AI{number}_BACKUP_TIMEOUT"
            for value in ("", " ", '""', "invalid", "0", "-1"):
                with self.subTest(role=role, value=value), patch.dict(ai.os.environ, {name: value}):
                    self.assertEqual(ai.chat_request_timeout(role, 90, True), 90)
            with patch.dict(ai.os.environ, {name: '"40"'}):
                self.assertEqual(ai.chat_request_timeout(role, 90, True), 40)
                self.assertEqual(ai.chat_request_timeout(role, 90, False), 90)

    def test_timeout_only_setting_does_not_create_an_independent_backup(self):
        with patch.dict(ai.os.environ, {"FIB_PA_AI2_BACKUP_TIMEOUT": "40"}), \
             patch.object(ai.requests, "post", side_effect=ai.requests.ReadTimeout("read timed out")) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call(role="AI-2")
        self.assertEqual(post.call_count, 1)

    def test_ai2_primary_and_backup_use_90_and_40_seconds_with_same_evidence(self):
        with patch.dict(ai.os.environ, {"AI2_BACKUP_URL": "https://api.deepseek.com/chat/completions",
                                       "AI2_BACKUP_KEY": "BACKUP-SECRET", "FIB_PA_AI2_BACKUP_MODEL": "deepseek-v4-pro",
                                       "FIB_PA_AI2_BACKUP_EFFORT": "low", "FIB_PA_AI2_BACKUP_TIMEOUT": "40"}), \
             patch.object(ai.requests, "post", side_effect=[ai.requests.ReadTimeout("read timed out"),
                          Response('{"decision":"REJECT","defects":["hard defect"],"reasoning":"reviewed","fact_conflicts":[]}')]) as post:
            raw = ai.call_chat("AI-2", "grok-4.7", "medium", "independent review", "full evidence", 90,
                               "https://primary.example.test/chat/completions", "SECRET-KEY")
        self.assertEqual(json.loads(raw)["decision"], "REJECT")
        first, second = post.call_args_list
        self.assertEqual([first.kwargs["timeout"], second.kwargs["timeout"]], [90, 40])
        self.assertEqual(second.kwargs["json"]["reasoning_effort"], "low")
        self.assertEqual(first.kwargs["json"]["messages"], second.kwargs["json"]["messages"])
        self.assertEqual([event["read_timeout_sec"] for event in self.events()], [90, 40])

    def test_body_read_timeout_keeps_received_headers_and_no_complete_body(self):
        def slow_body(*args, **kwargs):
            response = Response()
            hook = kwargs.get("hooks", {}).get("response")
            if hook:
                hook(response)
            raise ai.requests.ConnectionError("Read timed out")
        with patch.object(ai.requests, "post", side_effect=slow_body) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call()
        self.assertEqual(post.call_count, 1)
        event = self.events()[0]
        self.assertEqual(event["http_status"], 200)
        self.assertEqual(event["request_id"], "request-123")
        self.assertIsNotNone(event["headers_elapsed_sec"])
        self.assertFalse(event["body_complete"])
        self.assertEqual(event["outcome"], "read_timeout")
        self.assertIsNone(event["usage"])

    def test_two_slow_routes_do_not_generate_five_paid_copies(self):
        self.backup()
        with patch.object(ai.requests, "post", side_effect=ai.requests.ReadTimeout("read timed out")) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call()
        self.assertEqual(post.call_count, 2)
        events = self.events()
        self.assertEqual([row["channel"] for row in events], ["primary", "backup"])
        self.assertEqual(len({row["call_id"] for row in events}), 1)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["messages"], post.call_args_list[1].kwargs["json"]["messages"])

    def test_wrapped_read_timeout_is_not_retried_as_a_connect_failure(self):
        with patch.object(ai.requests, "post", side_effect=ai.requests.ConnectionError("Read timed out")) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call()
        self.assertEqual(post.call_count, 1)

    def test_connect_timeout_still_uses_shared_connection_retry_budget(self):
        with patch.object(ai.requests, "post", side_effect=ai.requests.ConnectTimeout("connect timed out")) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call()
        self.assertEqual(post.call_count, 5)
        self.assertTrue(all(row["outcome"] == "connection_failure" for row in self.events()))

    def test_token_limits_and_chat_effort_parameter_for_all_roles(self):
        for role in ("AI-1", "AI-2", "AI-3"):
            for model, token_field in (("gpt-5.5", "max_completion_tokens"), ("grok-4.6", "max_tokens"), ("deepseek-flash", "max_tokens")):
                with self.subTest(role=role, model=model), patch.object(ai.requests, "post", return_value=Response()) as post:
                    self.call(role, model)
                    data = post.call_args.kwargs["json"]
                    self.assertEqual(data[token_field], ai.AI_MAX_TOKENS[role])
                    self.assertEqual(data["reasoning_effort"], "medium")
                    self.assertNotIn("reasoning", data)

    def test_token_parameter_fallback_keeps_limit_and_is_remembered(self):
        responses = [Response(status=400, body="Unsupported parameter: max_completion_tokens. Use max_tokens."), Response(), Response()]
        with patch.object(ai.requests, "post", side_effect=responses) as post:
            self.call(model="gpt-test")
            self.call(model="gpt-test")
        first, second, third = [call.kwargs["json"] for call in post.call_args_list]
        self.assertEqual(first["max_completion_tokens"], second["max_tokens"])
        self.assertEqual(second["max_tokens"], third["max_tokens"])
        self.assertEqual([row["outcome"] for row in self.events()], ["unsupported_token_limit", "success", "success"])

    def test_both_limit_fields_rejected_never_send_an_unbounded_request(self):
        responses = [Response(status=400, body="Unsupported max_tokens"), Response(status=400, body="Unsupported max_completion_tokens")]
        with patch.object(ai.requests, "post", side_effect=responses) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call()
        self.assertEqual(post.call_count, 2)
        self.assertTrue(all(any(key in call.kwargs["json"] for key in ("max_tokens", "max_completion_tokens")) for call in post.call_args_list))

    def test_unsupported_effort_is_remembered_without_dropping_token_limit(self):
        with patch.object(ai.requests, "post", side_effect=[Response(status=400, body="unsupported reasoning_effort"), Response(), Response()]) as post:
            self.call()
            self.call()
        requests = [call.kwargs["json"] for call in post.call_args_list]
        self.assertIn("reasoning_effort", requests[0])
        self.assertNotIn("reasoning_effort", requests[1])
        self.assertNotIn("reasoning_effort", requests[2])
        self.assertTrue(all(data["max_tokens"] == ai.AI_MAX_TOKENS["AI-1"] for data in requests))

    def test_truncated_even_parseable_json_is_not_executed_or_rejudged(self):
        self.backup()
        with patch.object(ai.requests, "post", return_value=Response('{"action":"buy"}', finish="length")) as post:
            with self.assertRaises(ai.AIRequestError) as caught:
                self.call()
        self.assertEqual(post.call_count, 1)
        self.assertIn("不完整决策", caught.exception.detail)
        self.assertEqual(self.events()[0]["outcome"], "truncated")

    def test_other_400_errors_do_not_silently_disable_reasoning(self):
        with patch.object(ai.requests, "post", side_effect=[Response(status=400, body="max_tokens must exceed reasoning budget"), Response()]) as post:
            self.call()
        self.assertEqual(post.call_args_list[0].kwargs["json"]["reasoning_effort"], "medium")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["reasoning_effort"], "low")
        self.assertFalse(any(options.get("omit_reasoning") for options in ai._CHAT_OPTIONS.values()))

    def test_usage_preserves_inclusive_input_and_missing_values(self):
        usage = {"prompt_tokens": 1400, "completion_tokens": 300, "total_tokens": 1700,
                 "prompt_tokens_details": {"cached_tokens": 1100},
                 "completion_tokens_details": {"reasoning_tokens": 150}}
        with patch.object(ai.requests, "post", return_value=Response(usage=usage)):
            self.call(request_context={"decision_id": "same-bar", "trigger": "M15"})
        event = self.events()[0]
        self.assertEqual(event["usage"]["input_tokens"], 1400)
        self.assertEqual(event["usage"]["cache_read_tokens"], 1100)
        self.assertEqual(event["usage"]["reasoning_tokens"], 150)
        self.assertIsNone(event["usage"]["cache_write_tokens"])
        self.assertEqual(event["request_id"], "request-123")
        self.assertEqual(event["response_id"], "response-456")
        self.assertNotIn("SECRET", json.dumps(event))

    def test_cache_miss_is_not_mislabeled_as_cache_creation(self):
        normalized = ai.normalized_chat_usage({"usage": {"prompt_cache_hit_tokens": 100,
                                                         "prompt_cache_miss_tokens": 20}})
        self.assertEqual(normalized["cache_read_tokens"], 100)
        self.assertEqual(normalized["uncached_input_tokens"], 20)
        self.assertIsNone(normalized["cache_write_tokens"])

    def test_stale_response_usage_is_recorded_before_discarding(self):
        alive = [True]
        def reply(*args, **kwargs):
            alive[0] = False
            return Response(usage={"completion_tokens": 50})
        with patch.object(ai.requests, "post", side_effect=reply):
            self.assertEqual(self.call(still_current=lambda: alive[0]), "")
        self.assertEqual(self.events()[0]["outcome"], "stale_response")
        self.assertEqual(self.events()[0]["usage"]["output_tokens"], 50)

    def test_calls_remain_stateless_and_do_not_replay_prior_output(self):
        with patch.object(ai.time, "time_ns", return_value=123456789), \
             patch.object(ai.requests, "post", return_value=Response()) as post:
            self.call()
            self.call()
        first, second = [call.kwargs["json"]["messages"] for call in post.call_args_list]
        self.assertEqual(first, second)
        self.assertEqual([message["role"] for message in first], ["system", "user"])
        self.assertNotEqual(self.events()[0]["call_id"], self.events()[1]["call_id"])
        self.assertTrue(all(event["call_id"].startswith("AI-1-") for event in self.events()))

    def test_snapshot_trace_ids_keep_role_and_decision_prefix_with_stalled_clock(self):
        with patch.object(ai.time, "time_ns", return_value=123456789):
            traces = {prefix: [ai.request_trace_id(prefix) for _ in range(2)]
                      for prefix in ("decision-ai1", "decision-ai2", "ai3")}
        self.assertEqual(len({trace for values in traces.values() for trace in values}),6)
        for prefix, values in traces.items():
            self.assertTrue(all(trace.startswith(prefix+"-") for trace in values))

    def test_usage_write_failure_does_not_block_valid_model_response(self):
        with patch.object(ai.os, "makedirs", side_effect=OSError("disk full")), patch.object(ai.requests, "post", return_value=Response()):
            self.assertIn("action", self.call())

    def self_test_state(self):
        return (ai.requests.post,ai.backup_chat_config,ai.base.logger,ai.AI_HTTP_RETRIES,
                ai.AI_HTTP_RETRY_PAUSE,ai.AI_READ_TIMEOUT_RETRIES,ai.AI_USAGE_DIR)

    def test_retry_self_test_ignores_configured_messages_backups_and_restores_patches(self):
        backups = {f"AI{number}_BACKUP_URL":"https://configured.example.test/v1/messages"
                   for number in (1,2,3)}
        backups.update({f"AI{number}_BACKUP_KEY":"configured-key" for number in (1,2,3)})
        with patch.dict(ai.os.environ,backups), \
             patch.object(ai.requests,"post",side_effect=AssertionError("real HTTP forbidden")) as post, \
             patch.object(ai,"record_ai_usage") as usage,patch.object(ai.base.mt5,"initialize") as terminal:
            before = self.self_test_state()
            self.assertEqual(ai.backup_chat_config("AI-1","m","low","http://example.test","key")["url"],
                             backups["AI1_BACKUP_URL"])
            ai.run_http_retry_self_test()
            self.assertEqual(self.self_test_state(),before)
            self.assertEqual(ai.backup_chat_config("AI-1","m","low","http://example.test","key")["url"],
                             backups["AI1_BACKUP_URL"])
            post.assert_not_called()
            terminal.assert_not_called()
            events = [call.args[0] for call in usage.call_args_list]
            self.assertEqual(len(events),9)
            self.assertTrue(all(event["host"] == "example.test" and event["protocol"] == "chat" for event in events))
            self.assertEqual([event["finish_reason"] for event in events if event["outcome"] == "success"],["stop"])

    def test_retry_self_test_restores_all_patches_and_temp_directory_after_failure(self):
        directories = []
        def fail(*args,**kwargs):
            directories.append(ai.AI_USAGE_DIR)
            raise RuntimeError("injected self-test failure")
        with patch.dict(ai.os.environ,{"AI1_BACKUP_URL":"https://configured.example.test/v1/messages"}), \
             patch.object(ai.requests,"post",side_effect=AssertionError("real HTTP forbidden")) as post, \
             patch.object(ai,"call_chat",side_effect=fail),patch.object(ai.base.mt5,"initialize") as terminal:
            before = self.self_test_state()
            environment = dict(ai.os.environ)
            with self.assertRaisesRegex(RuntimeError,"injected self-test failure"):
                ai.run_http_retry_self_test()
            self.assertEqual(self.self_test_state(),before)
            self.assertEqual(dict(ai.os.environ),environment)
            post.assert_not_called()
            terminal.assert_not_called()
        self.assertEqual(len(directories),1)
        self.assertFalse(Path(directories[0]).exists())


class PromptCompactionTests(unittest.TestCase):
    def pack(self):
        bars = [{"time": 1000+i*900, "open": 100., "high": 103., "low": 99., "close": 102., "tick_volume": 80,
                 "candle_parts": {"body": 2., "range": 4., "upper": 1., "lower": 1.}} for i in range(32)]
        return {"symbol": "XAUUSD", "setup": "pullback", "regime": "trend", "bar_closure": {"m15_last_closed_bar_time": 1000},
                "m15_bars": bars, "m5_bars": bars[-8:], "h1_bars": bars[-8:],
                "rules": {"max_stop_abs": 8., "min_rr": 1.5}, "quality_rubric": ai.quality_rubric(),
                "details": {"setup": "pullback", "regime": "trend", "pa_score": 20, "location": .8},
                "evidence_windows": {"m15_structure_lookback": 24, "m15_structure_bar_times": [bar["time"] for bar in bars[-24:]],
                                     "breakout_bar_times": [bar["time"] for bar in bars[-20:]], "m5_rejection_bar_times": [bar["time"] for bar in bars[-3:]]},
                "local_reasoning": "Hard rejection must not be erased", "computed_evidence": {"available": False, "reason": "unknown",
                    "obstacles": {"sell": [{"price": 103., "classification": "unknown", "origin_bar": bars[0]}]},
                    "m5_timing": {"sell": {"protected_stop": None, "blocked": False}}}}

    def test_compaction_preserves_prices_evidence_unknowns_and_source(self):
        original = self.pack()
        saved = copy.deepcopy(original)
        compact = ai.compact_market_pack(original)
        self.assertEqual(original, saved)
        self.assertEqual(compact["computed_evidence"], original["computed_evidence"])
        self.assertEqual(compact["local_reasoning"], original["local_reasoning"])
        self.assertEqual(compact["rules"], original["rules"])
        for name in ("m15_bars", "m5_bars", "h1_bars"):
            self.assertEqual(len(compact[name]), len(original[name]))
            for before, after in zip(original[name], compact[name]):
                self.assertEqual({key: value for key, value in before.items() if key != "candle_parts"}, after)
                derived = ai.base.candle_parts(after)
                self.assertEqual({key: derived[key] for key in before["candle_parts"]}, before["candle_parts"])
        self.assertEqual(compact["details"]["pa_score"], 20)
        self.assertTrue(ai._evidence_pointer_exists(compact, "/m15_bars/0/close"))
        self.assertFalse(ai._evidence_pointer_exists(compact, "/m15_bars/0/candle_parts/body"))
        self.assertLess(len(json.dumps(compact)), len(json.dumps(original)) * .85)

    def test_non_duplicate_or_missing_data_is_not_erased(self):
        original = self.pack()
        original["details"]["setup"] = "other"
        original["evidence_windows"]["m15_structure_bar_times"] = [123]
        del original["m15_bars"][0]["open"]
        compact = ai.compact_market_pack(original)
        self.assertEqual(compact["details"]["setup"], "other")
        self.assertEqual(compact["evidence_windows"]["m15_structure_bar_times"], [123])
        self.assertIn("candle_parts", compact["m15_bars"][0])

    def test_candle_math_with_more_precision_than_serialized_ohlc_is_retained(self):
        original = self.pack()
        original["m15_bars"][0]["candle_parts"]["body"] = 2.003
        compact = ai.compact_market_pack(original)
        self.assertEqual(compact["m15_bars"][0]["candle_parts"]["body"], 2.003)

    def test_stable_rules_and_rubric_precede_changing_timestamps(self):
        order = list(ai.compact_market_pack(self.pack()))
        self.assertLess(order.index("quality_rubric"), order.index("bar_closure"))
        self.assertLess(order.index("rules"), order.index("m15_bars"))

    def test_review_keeps_final_python_prices_original_thesis_and_market(self):
        review = {"proposal": {"action": "sell", "stop_loss": 103., "take_profit": 95., "reasoning": "duplicate",
                               "details": {"quality_assessment": "large repeated card"}},
                  "ai1_raw_proposal": {"action": "sell", "stop_loss": 104., "take_profit": 95., "reasoning": "original thesis",
                                       "invalidation": "M15 closed beyond 103", "fact_conflicts": [], "unknowns": ["older history"],
                                       "quality_assessment": "full card"},
                  "python_finalization": {"final_stop_loss": 103., "final_take_profit": 95., "reason": "M5 protection"},
                  "market_evidence": self.pack(), "setup": "pullback", "max_stop_abs": 8., "min_rr": 1.5}
        saved = copy.deepcopy(review)
        compact = ai.compact_review_pack(review)
        self.assertEqual(review, saved)
        self.assertEqual(compact["proposal"]["stop_loss"], 103.)
        self.assertEqual(compact["ai1_raw_proposal"]["stop_loss"], 104.)
        self.assertEqual(compact["python_finalization"], saved["python_finalization"])
        self.assertEqual(compact["ai1_raw_proposal"]["unknowns"], ["older history"])
        self.assertEqual(compact["market_evidence"]["computed_evidence"], saved["market_evidence"]["computed_evidence"])
        self.assertNotIn("quality_assessment", compact["ai1_raw_proposal"])
        self.assertNotIn("details", compact["proposal"])

    def test_m5_dispatch_is_identified_without_changing_local_filter(self):
        trader = object.__new__(ai.ThreeAITrader)
        trader.get_context = Mock(return_value=(None, None, None))
        trader.dispatch_entry = Mock()
        approved = SimpleNamespace(action="buy", confidence=80, stop_loss=97.)
        with patch.object(trader, "evaluate_context", return_value=approved):
            trader.dispatch_m5_entry(123)
        trader.dispatch_entry.assert_called_once_with(123, trigger="M5")

    def test_entry_pipeline_reviews_final_prices_with_shared_decision_and_trigger(self):
        frame = ai.base.pd.DataFrame([dict(time=1800000000+i*900, open=100+i*.1,
                                         high=100.5+i*.1, low=99.5+i*.1, close=100.1+i*.1,
                                         tick_volume=100) for i in range(80)])
        local = ai.base.StrategyResult("no", 43, False, "local below threshold", 0., 0., 43, 0, {"setup": "pullback"})
        proposal = ai.base.StrategyResult("buy", 80, False, "original thesis", 107., 113., 60, 0, {})
        raw = {"action": "buy", "stop_loss": 106., "take_profit": 113., "reasoning": "original thesis",
               "trigger_evidence": "closed M15 trigger", "invalidation": "M15 closed below structure",
               "target_basis": "given pivot", "unknowns": [], "fact_conflicts": [], "wait_basis": [],
               "quality_assessment": {"version": "entry-quality-v2", "direction": "buy", "dimensions": {
                   key: {"level": "adequate", "reason": "supported", "evidence_refs": ["/h1_trend"], "reservations": []}
                    for key in ai.QUALITY_DIMENSIONS}}}
        sent_requests = []
        def refreshed_chat(*args, **kwargs):
            sent_requests.append(kwargs["refresh_request"]())
            return (json.dumps(raw) if args[0] == "AI-1" else
                    '{"decision":"APPROVE","defects":[],"reasoning":"verified","fact_conflicts":[]}')
        with tempfile.TemporaryDirectory() as folder, patch.object(ai, "AI3_STATE_FILE", str(Path(folder)/"state.json")):
            trader = ai.ThreeAITrader()
            with patch.object(trader, "get_context", return_value=(frame, frame, frame)), \
                 patch.object(ai.base, "build_fib_volume_pa_decision", return_value=local), \
                 patch.object(ai.base.mt5, "symbol_info_tick", return_value=SimpleNamespace(bid=108.3, ask=108.4, time=1800009000)), \
                 patch.object(trader, "entry_is_current", return_value=True), \
                 patch.object(ai, "call_chat", side_effect=refreshed_chat) as chat, \
                 patch.object(ai, "finalize_ai_proposal", return_value=(proposal, "Python final protection")), \
                 patch.object(ai, "append_decision_snapshot") as snapshot, \
                 patch.object(trader, "place_order", return_value=False) as place, \
                 patch.object(trader, "notify_lines"), patch.object(trader, "release_task"):
                trader.entry_task(123, 1, "M5")
            self.assertEqual(chat.call_count, 2)
            first, second = chat.call_args_list
            self.assertEqual(first.kwargs["request_context"], second.kwargs["request_context"])
            self.assertEqual(second.kwargs["request_context"]["trigger"], "M5")
            review = json.loads(sent_requests[1]["prompt"].split('\n', 1)[1])
            self.assertEqual(review["proposal"]["stop_loss"], 107.)
            self.assertEqual(review["ai1_raw_proposal"]["stop_loss"], 106.)
            self.assertEqual(review["ai1_raw_proposal"]["invalidation"], raw["invalidation"])
            self.assertNotIn("quality_assessment", review["ai1_raw_proposal"])
            self.assertTrue(any(call.args[1] == "ai2_input" for call in snapshot.call_args_list))
            place.assert_called_once()


if __name__ == "__main__":
    unittest.main()
