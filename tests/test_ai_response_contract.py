"""Offline protocol and JSON regressions; no live HTTP or trading."""
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai


class Response:
    def __init__(self, native=True, text='{"action":"no"}', finish=None, error=None, usage=None):
        self.status_code = 400 if error else 200
        self.text = error or ""
        self.headers = {"request-id": "native-request"}
        self.body = ({"error": {"message": error}} if error else
                     {"type": "message", "id": "msg-test", "model": "claude-test",
                      "content": [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": text}],
                      "stop_reason": finish or "end_turn", "usage": usage} if native else
                     {"choices": [{"finish_reason": finish or "stop", "message": {"content": text}}], "usage": usage})

    def json(self):
        return self.body


class ResponseContractTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for scope in (patch.dict(ai.os.environ, {}, clear=True), patch.dict(ai._CHAT_OPTIONS, {}, clear=True),
                      patch.dict(ai._CAP_BLOCKED_ROUTES, {}, clear=True),
                      patch.object(ai, "AI_USAGE_DIR", self.folder.name), patch.object(ai.time, "sleep")):
            scope.start()
            self.addCleanup(scope.stop)

    def events(self):
        return [json.loads(line) for p in Path(self.folder.name).glob("*.jsonl")
                for line in p.read_text(encoding="utf-8").splitlines()]

    def call(self, role="AI-1", url="https://relay.test/v1/messages", **kwargs):
        return ai.call_chat(role, "claude-test", "medium", "system rules", "fresh market", 60, url, "SECRET", **kwargs)

    def test_native_request_and_response_use_messages_protocol(self):
        usage = {"input_tokens": 100, "output_tokens": 300, "cache_read_input_tokens": 200,
                 "cache_creation_input_tokens": 50}
        with patch.object(ai.requests, "post", return_value=Response(usage=usage)) as post:
            self.assertEqual(self.call(), '{"action":"no"}')
        request = post.call_args.kwargs
        self.assertEqual(request["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(request["headers"]["Authorization"], "Bearer SECRET")
        data = request["json"]
        self.assertEqual(data["system"], "system rules")
        self.assertEqual(data["messages"], [{"role": "user", "content": "fresh market"}])
        self.assertEqual(data["max_tokens"], ai.AI_MAX_TOKENS["AI-1"])
        self.assertEqual(data["output_config"]["effort"], "medium")
        self.assertEqual(data["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("reasoning_effort", data)
        event = self.events()[0]
        self.assertEqual((event["protocol"], event["finish_reason"]), ("anthropic", "end_turn"))
        self.assertEqual(event["request_id"], "native-request")
        self.assertEqual(event["usage"]["input_tokens"], 350)
        self.assertEqual(event["usage"]["uncached_input_tokens"], 100)
        self.assertEqual(event["usage"]["total_tokens"], 650)
        self.assertNotIn("SECRET", json.dumps(event))

    def test_protocol_follows_each_route_not_model_name(self):
        with patch.dict(ai.os.environ, {"AI1_BACKUP_URL": "https://backup.test/v1/chat/completions",
                                       "FIB_PA_AI1_BACKUP_MODEL": "gpt-test"}), \
             patch.object(ai.requests, "post", side_effect=[ai.requests.ReadTimeout("slow"), Response(native=False)]) as post:
            self.call()
        first, second = [c.kwargs for c in post.call_args_list]
        self.assertIn("system", first["json"])
        self.assertNotIn("system", second["json"])
        self.assertEqual(second["json"]["messages"][0]["role"], "system")
        self.assertIn("max_completion_tokens", second["json"])
        self.assertNotIn("anthropic-version", second["headers"])
        self.assertEqual([e["protocol"] for e in self.events()], ["anthropic", "chat"])

    def test_official_deepseek_messages_sends_auth_effort_and_no_ignored_schema(self):
        with patch.object(ai.requests, "post", return_value=Response()) as post:
            ai.call_chat("AI-2", "deepseek-v4-pro", "low", "Return a complete JSON object", "fresh market",
                         40, "https://api.deepseek.com/anthropic/v1/messages", "SECRET")
        request = post.call_args.kwargs
        self.assertEqual(request["headers"]["x-api-key"], "SECRET")
        self.assertEqual(request["json"]["output_config"], {"effort": "low"})
        self.assertEqual(request["json"]["model"], "deepseek-v4-pro")
        self.assertEqual(request["json"]["max_tokens"], ai.AI_MAX_TOKENS["AI-2"])
        self.assertEqual(request["json"]["messages"][0]["role"], "user")
        self.assertNotIn("response_format", request["json"])
        self.assertEqual(self.events()[0]["json_mode_sent"], "prompt")
        self.assertNotIn("SECRET", json.dumps(self.events()))

    def test_deepseek_relay_messages_still_attempts_schema(self):
        with patch.object(ai.requests, "post", return_value=Response()) as post:
            ai.call_chat("AI-2", "deepseek-v4-pro", "low", "rules", "market", 40,
                         "https://relay.test/v1/messages", "SECRET")
        self.assertIn("format", post.call_args.kwargs["json"]["output_config"])
        self.assertEqual(self.events()[0]["json_mode_sent"], "schema")

    def test_schema_profile_matches_exact_official_endpoint(self):
        self.assertFalse(ai.messages_supports_schema("https://API.DEEPSEEK.COM/anthropic/v1/messages/?a=b"))
        for url in ("https://api.deepseek.com.fake.test/anthropic/v1/messages",
                    "https://relay.test/anthropic/v1/messages", "https://api.deepseek.com/v1/messages"):
            with self.subTest(url=url):
                self.assertTrue(ai.messages_supports_schema(url))

    def test_gpt_and_grok_messages_keep_native_max_tokens(self):
        for model in ("gpt-6.1-sol", "grok-4.7"):
            with self.subTest(model=model), patch.object(ai.requests, "post", return_value=Response()) as post:
                ai.call_chat("AI-2", model, "low", "rules", "market", 60,
                             "https://relay.test/v1/messages", "SECRET")
                data = post.call_args.kwargs["json"]
                self.assertIn("max_tokens", data)
                self.assertNotIn("max_completion_tokens", data)
                self.assertNotIn("reasoning_effort", data)
                self.assertEqual(data["output_config"]["effort"], "low")

    def test_messages_primary_to_official_backup_refreshes_and_shares_budget(self):
        snapshots = iter(("quote1", "quote2"))
        with patch.dict(ai.os.environ, {"AI2_BACKUP_URL": "https://api.deepseek.com/anthropic/v1/messages",
                                       "AI2_BACKUP_KEY": "BACKUP", "FIB_PA_AI2_BACKUP_MODEL": "deepseek-v4-pro",
                                       "FIB_PA_AI2_BACKUP_EFFORT": "low", "FIB_PA_AI2_BACKUP_TIMEOUT": "40"}), \
             patch.object(ai, "AI_HTTP_RETRIES", 2), \
             patch.object(ai.requests, "post", side_effect=[ai.requests.ReadTimeout("slow"), Response()]) as post:
            ai.call_chat("AI-2", "grok-4.7", "low", "rules", "old market", 120,
                         "https://relay.test/v1/messages", "SECRET",
                         refresh_request=lambda: {"prompt": next(snapshots)})
        first, second = [c.kwargs for c in post.call_args_list]
        self.assertEqual((first["timeout"], second["timeout"]), (120, 40))
        self.assertEqual(second["headers"]["x-api-key"], "BACKUP")
        self.assertEqual([c["json"]["messages"][0]["content"] for c in (first, second)], ["quote1", "quote2"])
        self.assertEqual(second["json"]["output_config"], {"effort": "low"})
        self.assertEqual([e["protocol"] for e in self.events()], ["anthropic", "anthropic"])
        self.assertEqual([e["request_budget"] for e in self.events()], [2, 2])

    def test_official_messages_read_timeout_does_not_repeat(self):
        with patch.object(ai, "AI_READ_TIMEOUT_RETRIES", 0), \
             patch.object(ai.requests, "post", side_effect=ai.requests.ReadTimeout("slow")) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call("AI-3", "https://api.deepseek.com/anthropic/v1/messages")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.events()[0]["outcome"], "read_timeout")

    def test_official_messages_invalid_json_is_not_repaired_or_regenerated(self):
        raw = '{"action":"close"'
        with patch.object(ai.requests, "post", return_value=Response(text=raw)) as post:
            self.assertEqual(self.call("AI-3", "https://api.deepseek.com/anthropic/v1/messages"), raw)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.events()[0]["outcome"], "invalid_decision_json")

    def test_example_gold_routes_are_all_messages(self):
        path = Path(__file__).resolve().parents[1] / ".env.example"
        env = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                   if "=" in line and not line.lstrip().startswith("#"))
        for key in ("GPTSTORE_BASE_URL", "AI1_URL", "AI2_URL", "AI3_URL"):
            self.assertEqual(ai.request_protocol(env[key]), "anthropic", key)

    def test_chat_primary_can_switch_to_native_backup(self):
        with patch.dict(ai.os.environ, {"AI2_BACKUP_URL": "https://backup.test/v1/messages"}), \
             patch.object(ai.requests, "post", side_effect=[ai.requests.ReadTimeout("slow"), Response()]) as post:
            self.call("AI-2", "https://main.test/v1/chat/completions")
        self.assertIn("system", post.call_args.kwargs["json"])

    def test_ai1_schema_and_template_put_wait_basis_at_top_level(self):
        schema = ai.response_json_schema("AI-1")
        self.assertIn("wait_basis", schema["required"])
        self.assertNotIn("wait_basis", schema["properties"]["quality_assessment"]["properties"])
        self.assertEqual(set(schema["properties"]["quality_assessment"]["properties"]["dimensions"]["required"]),
                         set(ai.QUALITY_DIMENSIONS))
        example = ai.ai1_json_template()
        self.assertIn("wait_basis", json.loads(json.dumps(example)))
        self.assertNotIn("wait_basis", example["quality_assessment"])

    def test_messages_schema_recovery_preserves_effort_and_cap(self):
        with patch.object(ai.requests, "post", side_effect=[Response(error="Unsupported output_config.format"),
                          Response(), Response()]) as post:
            self.call()
            self.call()
        requests = [c.kwargs["json"] for c in post.call_args_list]
        self.assertIn("format", requests[0]["output_config"])
        self.assertNotIn("format", requests[1]["output_config"])
        self.assertNotIn("format", requests[2]["output_config"])
        self.assertTrue(all(d["output_config"]["effort"] == "medium" for d in requests))
        self.assertTrue(all(d["max_tokens"] == ai.AI_MAX_TOKENS["AI-1"] for d in requests))

    def test_chat_schema_and_json_mode_recovery_share_budget(self):
        with patch.object(ai.requests, "post", side_effect=[Response(error="json_schema not supported"),
                          Response(error="Unsupported response_format"), Response(native=False)]) as post:
            self.call(url="https://relay.test/v1/chat/completions")
        requests = [c.kwargs["json"] for c in post.call_args_list]
        self.assertEqual(requests[0]["response_format"]["type"], "json_schema")
        self.assertEqual(requests[1]["response_format"]["type"], "json_object")
        self.assertNotIn("response_format", requests[2])
        self.assertTrue(all(d["max_tokens"] == ai.AI_MAX_TOKENS["AI-1"] for d in requests))

    def test_native_effort_recovery_preserves_schema(self):
        with patch.object(ai.requests, "post", side_effect=[Response(error="Unsupported output_config.effort"), Response()]) as post:
            self.call()
        data = post.call_args.kwargs["json"]
        self.assertNotIn("effort", data["output_config"])
        self.assertIn("format", data["output_config"])

    def test_native_truncation_never_switches_or_parses_a_trade(self):
        with patch.dict(ai.os.environ, {"AI1_BACKUP_URL": "https://backup.test/chat"}), \
             patch.object(ai.requests, "post", return_value=Response(text='{"action":"buy"}', finish="max_tokens")) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.events()[0]["outcome"], "truncated")

    def test_output_over_reported_cap_is_rejected_without_more_generation(self):
        with patch.object(ai.requests, "post", return_value=Response(usage={"output_tokens": 8328})) as post:
            with self.assertRaisesRegex(ai.AIRequestError, "8328"):
                self.call()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.events()[0]["outcome"], "token_limit_exceeded")

    def test_invalid_decision_json_has_diagnostic_and_no_paid_repair(self):
        raw = '{"action":"no","quality_assessment":{"wait_basis":[]}'
        with patch.object(ai.requests, "post", return_value=Response(text=raw)) as post:
            self.assertEqual(self.call(), raw)
        self.assertEqual(post.call_count, 1)
        event = self.events()[0]
        self.assertEqual(event["outcome"], "invalid_decision_json")
        self.assertIn("语法", event["json_error"])
        parsed, error = ai.parse_json_object(raw)
        self.assertIsNone(parsed)
        self.assertIn("列", error)

    def test_parser_accepts_complete_fence_but_rejects_duplicate_and_nonfinite(self):
        self.assertEqual(ai.extract_json_object('```json\n{"action":"no"}\n```'), {"action": "no"})
        for raw in ('{"action":"buy","action":"no"}', '{"stop_loss":NaN}', '{"a":1}{"b":2}', '{"a":1',
                    '{"stop_loss":1e9999}', '[{"action":"buy"}]'):
            with self.subTest(raw=raw):
                parsed, error = ai.parse_json_object(raw)
                self.assertIsNone(parsed)
                self.assertTrue(error)

    def test_wrong_wait_basis_nesting_is_explicitly_rejected(self):
        payload = ai.ai1_json_template()
        payload["quality_assessment"]["wait_basis"] = payload.pop("wait_basis")
        _, error = ai.normalize_ai1_quality(payload, {})
        self.assertIn("字段层级错误", error)

    def test_native_mandatory_token_limit_cannot_turn_into_chat_parameter(self):
        with patch.object(ai.requests, "post", return_value=Response(error="Unsupported max_tokens")) as post:
            with self.assertRaisesRegex(ai.AIRequestError, "必填max_tokens"):
                self.call()
        self.assertEqual(post.call_count, 1)

    def test_unsupported_whole_output_config_drops_only_optional_config(self):
        with patch.object(ai.requests, "post", side_effect=[Response(error="output_config: Extra inputs are not permitted"), Response()]) as post:
            self.call()
        data = post.call_args.kwargs["json"]
        self.assertNotIn("output_config", data)
        self.assertIn("max_tokens", data)

    def test_schema_recovery_refreshes_quote_and_obeys_total_budget(self):
        snapshots = iter(("fresh1", "fresh2", "fresh3"))
        with patch.object(ai, "AI_HTTP_RETRIES", 2), patch.object(ai.requests, "post", side_effect=[
                Response(error="Unsupported output_config.format"), Response(error="Unsupported output_config.effort")]) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call(refresh_request=lambda: {"prompt": next(snapshots)})
        self.assertEqual(post.call_count, 2)
        self.assertEqual([c.kwargs["json"]["messages"][0]["content"] for c in post.call_args_list], ["fresh1", "fresh2"])


if __name__ == "__main__":
    unittest.main()
