"""Real duplicate fixture and output accounting boundaries; no HTTP or MT5."""
import copy
import json
import logging
from pathlib import Path
import unittest
from unittest.mock import patch

logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai
from strategies import quality
from test_ai_entry_gate_v2 import quality_sample
from test_ai_response_contract import Response


class IssueFirstTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = json.loads((Path(__file__).parent / "fixtures/ai1_quality_20261014_141048.json")
                                 .read_text(encoding="utf-8"))

    def test_real_duplicate_stays_rejected_without_regrading_or_deleting_facts(self):
        raw = copy.deepcopy(self.archive["raw"])
        before = copy.deepcopy(raw)
        result, error = ai.normalize_ai1_quality(raw, self.archive["request_pack"])
        self.assertEqual(error, self.archive["expected_error"])
        self.assertEqual(raw, before)
        self.assertEqual(result["action"], "no")
        self.assertNotIn("quality_score", result)

    def test_schema_and_template_generate_issues_before_dimension_grades(self):
        schema = ai.response_json_schema("AI-1", self.archive["request_pack"])
        fields = schema["properties"]["quality_assessment"]["properties"]
        self.assertLess(list(fields).index("issues"), list(fields).index("dimensions"))
        template = ai.ai1_json_template()["quality_assessment"]
        self.assertLess(list(template).index("issues"), list(template).index("dimensions"))
        rubric = ai.quality_rubric(True)
        self.assertEqual(rubric["grading_order_version"], quality.GRADING_ORDER_VERSION)
        self.assertIn("trigger", rubric["grading_order_policy"])
        self.assertIn("counter_evidence", rubric["grading_order_policy"])
        self.assertIn("先写issues", ai.AI1_INSTRUCTIONS)

    def authored_watch(self, independent_trigger=False):
        # An independent, synthetic reply, never a rewrite of the archived bad reply.
        pack, raw = quality_sample("sell")
        pack["m5_bars"] = [{"time": 2, "open": 100, "high": 104, "low": 99, "close": 103}]
        raw.update(action="no", stop_loss=None, take_profit=None,
                   wait_basis=[{"code": "material_counter_evidence", "reason": "独立已确认的反向结构",
                                "evidence_refs": ["/m5_bars/0"]}])
        dimensions = {name: {"level": "adequate", "reason": "本维度独立证据",
                             "evidence_refs": ["/h1_trend"]} for name in ai.QUALITY_DIMENSIONS}
        dimensions["trigger"]["reason"] = "本方向触发已成立；同根M5反向事实由反证维度评估"
        dimensions["counter_evidence"]["level"] = "weak"
        issues = [{"dimension": "counter_evidence", "kind": "reservation", "cause": "opposing_structure",
                   "event_group": "m5-confirmed-reversal", "reason": "独立重大反证",
                   "evidence_refs": ["/m5_bars/0"]}]
        if independent_trigger:
            dimensions["trigger"]["level"] = "weak"
            dimensions["trigger"]["reason"] = "另一个独立事件的本方向触发不足"
            issues.append({"dimension": "trigger", "kind": "reservation", "cause": "trigger_weakness",
                           "event_group": "own-trigger-event", "reason": "本方向的独立触发不足",
                           "evidence_refs": ["/h1_trend"]})
        raw["quality_assessment"] = {"version": ai.LEDGER_QUALITY_VERSION, "direction": "sell",
                                     "issues": issues, "dimensions": dimensions}
        return pack, raw

    def test_one_counter_event_can_be_described_in_trigger_without_duplicate_deduction(self):
        pack, raw = self.authored_watch()
        before = copy.deepcopy(raw)
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertEqual(error, "")
        self.assertEqual(raw, before)
        self.assertEqual(result["quality_score"], 72)
        self.assertEqual(ai.ai1_result_status(result, "", None), "watch")
        self.assertEqual(len(result["quality_assessment"]["issues"]), 1)

    def test_independent_trigger_weakness_keeps_its_own_deduction(self):
        pack, raw = self.authored_watch(independent_trigger=True)
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertEqual(error, "")
        self.assertEqual(result["quality_score"], 64)
        self.assertEqual(result["action"], "no")
        self.assertEqual(len(result["quality_assessment"]["issues"]), 2)


class OutputAccountingTests(unittest.TestCase):
    def body(self, visible=True, thinking=4096, output=4096):
        return {"type": "message", "stop_reason": "max_tokens",
                "content": ([{"type": "text", "text": '{"action":"no","reasoning":'}] if visible else [])
                + [{"type": "thinking", "thinking": "PRIVATE_NOT_TO_BE_RECORDED"}],
                "usage": {"input_tokens": 4, "cache_creation_input_tokens": 31358,
                          "cache_read_input_tokens": 0, "output_tokens": output,
                          "output_tokens_details": {"thinking_tokens": thinking}}}

    def test_reported_all_thinking_with_visible_json_is_inconsistent_not_zero_answer_tokens(self):
        body = self.body()
        original = copy.deepcopy(body)
        usage = ai.normalized_chat_usage(body)
        self.assertTrue(usage["reasoning_usage_inconsistent"])
        self.assertIsNone(usage["non_reasoning_output_tokens"])
        self.assertEqual(usage["output_tokens"], 4096)
        self.assertEqual(usage["reasoning_tokens"], 4096)
        self.assertEqual(body, original)

    def test_genuine_thinking_only_output_can_still_report_zero_answer_tokens(self):
        usage = ai.normalized_chat_usage(self.body(visible=False))
        self.assertFalse(usage["reasoning_usage_inconsistent"])
        self.assertEqual(usage["non_reasoning_output_tokens"], 0)

    def test_positive_reported_difference_remains_available(self):
        usage = ai.normalized_chat_usage(self.body(thinking=1406, output=3083))
        self.assertFalse(usage["reasoning_usage_inconsistent"])
        self.assertEqual(usage["non_reasoning_output_tokens"], 1677)

    def test_truncated_accounting_conflict_still_makes_one_post_and_fails(self):
        response = Response(text='{"action":"no","reasoning":', finish="max_tokens")
        response.body = self.body()
        with patch.dict(ai.os.environ, {}, clear=True), \
                patch.dict(ai._CHAT_OPTIONS, {}, clear=True), \
                patch.dict(ai._CAP_BLOCKED_ROUTES, {}, clear=True), \
                patch.object(ai, "AI_HTTP_RETRIES", 5), \
                patch.object(ai, "record_ai_usage") as record, \
                patch.object(ai.requests, "post", return_value=response) as post:
            with self.assertRaises(ai.AIRequestError) as failure:
                ai.call_chat("AI-1", "claude-test", "medium", "offline rules", "offline input", 120,
                             "https://relay.test/v1/messages", "mock-key")
        self.assertEqual(post.call_count, 1)
        self.assertIn("token上限", str(failure.exception))
        self.assertIn("计量冲突", str(failure.exception))
        event = record.call_args.args[0]
        self.assertEqual(event["outcome"], "truncated")
        self.assertEqual(event["usage_reported"]["output_tokens_details"]["thinking_tokens"], 4096)
        self.assertIsNone(event["usage"]["non_reasoning_output_tokens"])
        self.assertNotIn("PRIVATE_NOT_TO_BE_RECORDED", json.dumps(event))


if __name__ == "__main__":
    unittest.main()
