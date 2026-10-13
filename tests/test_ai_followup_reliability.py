"""Real 15:16 duplicate card and 15:12 Grok reasoning conflict, offline only."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
import xauusd_fib_volume_pa_ai3 as ai
from strategies import quality
from test_ai_response_contract import Response
import test_ai_review_reliability as review_tests


class FollowupQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = json.loads((Path(__file__).parent / "fixtures/ai_followup_20261013_151602.json")
                                 .read_text(encoding="utf-8"))

    def test_real_duplicate_rejection_names_both_locations_without_repair(self):
        pack, raw = self.archive["request_pack"], self.archive["raw"]
        before = copy.deepcopy(raw)
        # Wire-schema success does not establish semantic uniqueness.
        self.assertEqual(ai.parse_json_object(self.archive["raw_text"])[1], "")
        self.assertEqual(list(Draft202012Validator(ai.response_json_schema("AI-1", pack)).iter_errors(raw)), [])
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertIn("重复扣分", error)
        self.assertIn("/structure/reservations/0", error)
        self.assertIn("/counter_evidence/observations/0", error)
        self.assertIn("cause=opposing_structure", error)
        self.assertEqual(ai.ai1_result_status(result, error, None), "contract_invalid")
        self.assertEqual(result["action"], "buy")
        self.assertNotIn("quality_score", result)
        self.assertEqual(raw, before)

    def test_independently_authored_single_owner_keeps_five_grades_and_prices(self):
        # A new fixture, NOT a production repair of the archived response.
        raw = copy.deepcopy(self.archive["raw"])
        raw["quality_assessment"]["dimensions"]["structure"]["reservations"] = []
        pack = self.archive["request_pack"]
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertEqual(error, "")
        self.assertEqual(result["quality_score"], 80)
        self.assertEqual(result["action"], "buy")
        self.assertEqual((result["stop_loss"], result["take_profit"]), (4186.58, 4200.1))
        self.assertEqual([d["level"] for d in result["quality_assessment"]["dimensions"].values()],
                         ["adequate"] * 5)

    def test_factual_mentions_in_multiple_dimension_reasons_are_not_duplicate_issues(self):
        raw = copy.deepcopy(self.archive["raw"])
        raw["quality_assessment"]["dimensions"]["structure"]["reservations"] = []
        raw["quality_assessment"]["dimensions"]["structure"]["reason"] = "H1多头；M15阴线尚非反转"
        self.assertEqual(ai.normalize_ai1_quality(raw, self.archive["request_pack"])[1], "")

    def test_null_group_reference_order_does_not_bypass_duplicate_check(self):
        raw = copy.deepcopy(self.archive["raw"])
        dims = raw["quality_assessment"]["dimensions"]
        refs = dims["structure"]["evidence_refs"][:2]
        dims["structure"]["reservations"][0]["evidence_refs"] = refs
        dims["counter_evidence"]["observations"][0]["evidence_refs"] = list(reversed(refs))
        self.assertIn("重复扣分", ai.normalize_ai1_quality(raw, self.archive["request_pack"])[1])

    def test_explicit_group_duplicate_still_invalid_with_distinct_references(self):
        raw = copy.deepcopy(self.archive["raw"])
        dims = raw["quality_assessment"]["dimensions"]
        first = dims["structure"]["reservations"][0]
        second = dims["counter_evidence"]["observations"][0]
        first["event_group"] = second["event_group"] = "m15-1791885600"
        second["evidence_refs"] = dims["counter_evidence"]["evidence_refs"]
        self.assertIn("重复扣分", ai.normalize_ai1_quality(raw, self.archive["request_pack"])[1])

    def test_schema_and_prompt_describe_single_issue_owner_without_claiming_enforcement(self):
        policy = quality.ISSUE_DEDUPLICATION_POLICY
        self.assertIn("reason/evidence_refs", policy)
        self.assertIn("M15", policy)
        self.assertIn(policy, ai.AI1_INSTRUCTIONS)
        schema = ai.response_json_schema("AI-1", self.archive["request_pack"])
        self.assertEqual(schema["properties"]["quality_assessment"]["description"], policy)
        Draft202012Validator.check_schema(schema)


class FollowupReasoningTests(unittest.TestCase):
    setUp = review_tests.ReviewReliabilityTests.setUp
    call = review_tests.ReviewReliabilityTests.call
    backup = review_tests.ReviewReliabilityTests.backup
    events = review_tests.ReviewReliabilityTests.events
    excessive = review_tests.ReviewReliabilityTests.excessive
    def test_known_grok_disabled_is_rejected_before_refresh_or_paid_post(self):
        for model in ("grok-4.5", "grok-4.6", "grok-4.7"):
            for url in ("https://hapiopen.cc/v1/messages", "https://api.x.ai/v1/chat/completions"):
                with self.subTest(model=model, url=url), \
                     patch.dict(ai.os.environ, {"FIB_PA_AI2_THINKING": "disabled"}), \
                     patch.object(ai.requests, "post", return_value=Response()) as post:
                    refresh = unittest.mock.Mock(return_value={"prompt": "fresh"})
                    with self.assertRaises(ai.AIRequestError) as caught:
                        ai.call_chat("AI-2", model, "low", "rules", "market", 120, url, "mock",
                                     refresh_request=refresh)
                    self.assertIn("不支持关闭思考", caught.exception.detail)
                    self.assertIn("THINKING", caught.exception.detail)
                    post.assert_not_called()
                    refresh.assert_not_called()

    def test_conflicted_primary_uses_existing_disabled_capable_backup_with_fresh_data(self):
        self.backup()
        with patch.dict(ai.os.environ, {"FIB_PA_AI2_THINKING": "disabled"}), \
             patch.object(ai.requests, "post", return_value=Response()) as post:
            refresh = unittest.mock.Mock(return_value={"prompt": "new-quote"})
            self.call(model="grok-4.7", refresh_request=refresh)
        self.assertEqual(post.call_count, 1)
        refresh.assert_called_once()
        self.assertTrue(post.call_args.args[0].startswith("https://api.deepseek.com/"))
        data = post.call_args.kwargs["json"]
        self.assertEqual(data["thinking"], {"type": "disabled"})
        self.assertEqual(data["messages"][0]["content"], "new-quote")
        self.assertEqual(data["max_tokens"], 4096)
        self.assertEqual(self.events()[0]["attempt"], 1)
        self.assertEqual(ai.os.environ.get("FIB_PA_AI2_THINKING"), None)

    def test_both_conflicted_routes_do_not_post_or_refresh(self):
        self.backup()
        with patch.dict(ai.os.environ, {"FIB_PA_AI2_THINKING": "disabled",
                                      "FIB_PA_AI2_BACKUP_MODEL": "grok-4.6"}), \
             patch.object(ai.requests, "post", return_value=Response()) as post:
            with self.assertRaises(ai.AIRequestError) as caught:
                self.call(model="grok-4.7")
            self.assertIn("grok-4.7", caught.exception.detail)
            self.assertIn("grok-4.6", caught.exception.detail)
        post.assert_not_called()
        self.assertEqual(self.events(), [])

    def test_blank_thinking_really_sends_configured_low_and_keeps_messages_cap(self):
        with patch.object(ai.requests, "post", return_value=Response()) as post:
            self.call(model="grok-4.7")
        data = post.call_args.kwargs["json"]
        self.assertNotIn("thinking", data)
        self.assertEqual(data["output_config"]["effort"], "low")
        self.assertEqual(data["max_tokens"], 4096)
        self.assertEqual(self.events()[0]["reasoning_effort_sent"], "low")

    def test_explicit_effort_is_not_silently_omitted_after_rejection(self):
        for error in ("output_config.effort unsupported", "output_config unsupported"):
            with self.subTest(error=error), patch.dict(ai._CHAT_OPTIONS, {}, clear=True), \
                 patch.object(ai.requests, "post", side_effect=[Response(error=error), Response()]) as post:
                with self.assertRaises(ai.AIRequestError) as caught:
                    self.call(model="grok-4.7")
                self.assertIn("档位", caught.exception.detail)
                self.assertEqual(post.call_count, 1)
                self.assertNotIn("omit_reasoning", ai._CHAT_OPTIONS.get(("https://primary.test/v1/messages", "grok-4.7"), {}))

    def test_effort_rejection_can_switch_backup_instead_of_using_provider_default_high(self):
        self.backup()
        with patch.object(ai.requests, "post", side_effect=[Response(error="output_config.effort unsupported"), Response()]) as post:
            self.call(model="grok-4.7")
        self.assertEqual(post.call_count, 2)
        self.assertTrue(post.call_args.args[0].startswith("https://api.deepseek.com/"))

    def test_format_only_recovery_still_sends_low_on_each_fresh_post(self):
        refresh = unittest.mock.Mock(side_effect=[{"prompt": "new-1"}, {"prompt": "new-2"}])
        with patch.object(ai.requests, "post", side_effect=[Response(error="output_config.format unsupported"), Response()]) as post:
            self.call(model="grok-4.7", refresh_request=refresh)
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["json"]["output_config"]["effort"], "low")
            self.assertEqual(call.kwargs["json"]["max_tokens"], 4096)
        self.assertEqual(post.call_args.kwargs["json"]["messages"][0]["content"], "new-2")

    def test_existing_omit_effort_cache_cannot_turn_grok_low_into_implicit_high(self):
        with patch.dict(ai._CHAT_OPTIONS, {("https://primary.test/v1/messages", "grok-4.7"): {"omit_reasoning": True}}), \
             patch.object(ai.requests, "post", return_value=Response()) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call(model="grok-4.7")
        post.assert_not_called()

    def test_9847_generated_tokens_still_hit_cost_budget_without_claiming_mapping_failure(self):
        response = self.excessive(output=9847)
        with patch.object(ai.requests, "post", return_value=response) as post:
            with self.assertRaises(ai.AIRequestError) as caught:
                self.call(model="grok-4.7")
            with self.assertRaises(ai.AIRequestError):
                self.call(model="grok-4.7")
        self.assertEqual(post.call_count, 1)
        self.assertIn("总生成预算", caught.exception.detail)
        self.assertIn("可能只限正文", caught.exception.detail)
        self.assertEqual(self.events()[0]["usage"]["output_tokens"], 9847)
        self.assertIsNone(self.events()[0]["usage"]["reasoning_tokens"])
