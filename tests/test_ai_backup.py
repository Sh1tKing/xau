"""Offline AI transport failover; all HTTP calls are mocked."""
import json
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai


class Response:
    def __init__(self, content='{"action":"no"}', status=200, body="", invalid_json=False):
        self.content, self.status_code, self.text = content, status, body
        self.invalid_json = invalid_json

    def json(self):
        if self.invalid_json:
            raise ValueError("not JSON")
        return {"choices": [{"message": {"content": self.content}}]}


class AIBackupTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(ai.os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.budget = patch.object(ai, "AI_HTTP_RETRIES", 5)
        self.budget.start()
        self.addCleanup(self.budget.stop)
        self.sleep = patch.object(ai.time, "sleep")
        self.sleep_mock = self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def configure(self, number):
        values = {
            f"AI{number}_BACKUP_URL": f"https://backup{number}.example.test/chat",
            f"AI{number}_BACKUP_KEY": f"backup-key-{number}",
            f"FIB_PA_AI{number}_BACKUP_MODEL": f"backup-model-{number}",
            f"FIB_PA_AI{number}_BACKUP_EFFORT": "low",
        }
        ai.os.environ.update(values)
        return values

    def call(self, role="AI-1", key="primary-key", url="https://primary.example.test/chat", **kwargs):
        return ai.call_chat(role, "primary-model", "medium", "role instructions", "same market evidence",
                            40, url, key, **kwargs)

    def test_missing_blank_and_quoted_blank_fields_inherit_each_role(self):
        expected = {"model": "m", "effort": "medium", "url": "https://primary.test/chat", "key": "key"}
        for number in (1, 2, 3):
            with self.subTest(role=number):
                self.assertEqual(ai.backup_chat_config(f"AI-{number}", "m", "medium", expected["url"], "key"), expected)
                ai.os.environ.update({f"AI{number}_BACKUP_URL": "  ", f"AI{number}_BACKUP_KEY": '""',
                                      f"FIB_PA_AI{number}_BACKUP_MODEL": "''", f"FIB_PA_AI{number}_BACKUP_EFFORT": ""})
                self.assertEqual(ai.backup_chat_config(f"AI-{number}", "m", "medium", expected["url"], "key"), expected)

    def test_partial_config_inherits_remaining_fields_without_crossing_roles(self):
        ai.os.environ["AI2_BACKUP_URL"] = "https://second.test/chat"
        ai.os.environ["AI1_BACKUP_KEY"] = "first-key"
        second = ai.backup_chat_config("AI-2", "m2", "low", "https://main.test/chat", "key2")
        self.assertEqual(second, {"url": "https://second.test/chat", "key": "key2", "model": "m2", "effort": "low"})

    def test_valid_no_reject_and_hold_never_consult_backup(self):
        for number, content in ((1, '{"action":"no"}'), (2, '{"decision":"REJECT"}'), (3, '{"action":"hold"}')):
            with self.subTest(role=number):
                self.configure(number)
                with patch.object(ai.requests, "post", return_value=Response(content)) as post:
                    self.assertEqual(self.call(f"AI-{number}"), content)
                self.assertEqual(post.call_count, 1)
                self.assertEqual(post.call_args.args[0], "https://primary.example.test/chat")

    def test_each_role_switches_on_network_failure_and_preserves_evidence(self):
        for number, failure in ((1, ai.requests.ReadTimeout("offline")), (2, ai.requests.ConnectionError("offline")),
                                (3, ai.requests.Timeout("offline"))):
            with self.subTest(role=number):
                configured = self.configure(number)
                with patch.object(ai.requests, "post", side_effect=[failure, Response()]) as post:
                    self.assertEqual(json.loads(self.call(f"AI-{number}"))["action"], "no")
                first, second = post.call_args_list
                self.assertEqual(second.args[0], configured[f"AI{number}_BACKUP_URL"])
                self.assertEqual(second.kwargs["headers"]["Authorization"], "Bearer " + configured[f"AI{number}_BACKUP_KEY"])
                self.assertEqual(second.kwargs["json"]["model"], configured[f"FIB_PA_AI{number}_BACKUP_MODEL"])
                self.assertEqual(second.kwargs["json"]["reasoning"], {"effort": "low"})
                self.assertEqual(second.kwargs["json"]["messages"], first.kwargs["json"]["messages"])
                self.assertEqual(second.kwargs["timeout"], 40)

    def test_http_errors_switch_to_backup(self):
        self.configure(2)
        for status in (401, 403, 404, 429, 500, 503):
            with self.subTest(status=status), patch.object(ai.requests, "post", side_effect=[Response(status=status), Response()]) as post:
                self.call("AI-2")
                self.assertEqual(post.call_count, 2)
                self.assertIn("backup2", post.call_args.args[0])

    def test_url_only_backup_uses_primary_key_model_and_effort(self):
        ai.os.environ["AI1_BACKUP_URL"] = "https://alternate.example.test/chat"
        with patch.object(ai.requests, "post", side_effect=[ai.requests.Timeout("offline"), Response()]) as post:
            self.call()
        request = post.call_args
        self.assertEqual(request.args[0], "https://alternate.example.test/chat")
        self.assertEqual(request.kwargs["headers"]["Authorization"], "Bearer primary-key")
        self.assertEqual(request.kwargs["json"]["model"], "primary-model")
        self.assertEqual(request.kwargs["json"]["reasoning"], {"effort": "medium"})

    def test_next_analysis_starts_with_primary_again(self):
        self.configure(1)
        with patch.object(ai.requests, "post", side_effect=[ai.requests.Timeout("offline"), Response(), Response()]) as post:
            self.call()
            self.call()
        self.assertEqual([item.args[0] for item in post.call_args_list], [
            "https://primary.example.test/chat", "https://backup1.example.test/chat", "https://primary.example.test/chat"])

    def test_non_json_or_empty_response_switches_to_backup(self):
        self.configure(1)
        for response in (Response(invalid_json=True), Response(content="")):
            with self.subTest(response=response), patch.object(ai.requests, "post", side_effect=[response, Response()]) as post:
                self.call()
                self.assertEqual(post.call_count, 2)
                self.assertIn("backup1", post.call_args.args[0])

    def test_identical_backup_keeps_original_five_transport_attempts(self):
        with patch.object(ai.requests, "post", side_effect=ai.requests.ReadTimeout("offline")) as post:
            with self.assertRaises(ai.AIRequestError) as caught:
                self.call()
        self.assertEqual(post.call_count, 5)
        self.assertEqual(len({item.args[0] for item in post.call_args_list}), 1)
        self.assertTrue(all(item.kwargs["json"]["reasoning"] == {"effort": "medium"} for item in post.call_args_list))
        self.assertNotIn("没有可见 JSON", caught.exception.detail)

    def test_dead_primary_and_backup_share_five_attempts(self):
        self.configure(1)
        with patch.object(ai.requests, "post", side_effect=ai.requests.ConnectionError("offline")) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call()
        self.assertEqual(post.call_count, 5)
        self.assertIn("primary", post.call_args_list[0].args[0])
        self.assertTrue(all("backup1" in item.args[0] for item in post.call_args_list[1:]))
        self.assertEqual(self.sleep_mock.call_count, 4)

    def test_parameter_recovery_on_backup_uses_same_total_budget(self):
        self.configure(2)
        responses = [ai.requests.Timeout("offline"), Response(status=400, body="unsupported reasoning"), Response()]
        with patch.object(ai.requests, "post", side_effect=responses) as post:
            self.call("AI-2")
        self.assertEqual(post.call_count, 3)
        self.assertIn("reasoning", post.call_args_list[1].kwargs["json"])
        self.assertNotIn("reasoning", post.call_args_list[2].kwargs["json"])

    def test_global_budget_caps_transport_parameter_and_output_recovery(self):
        self.configure(2)
        responses = [ai.requests.Timeout("offline"), Response(status=400, body="unsupported reasoning"), Response(content="")]
        with patch.object(ai, "AI_HTTP_RETRIES", 3), patch.object(ai.requests, "post", side_effect=responses) as post:
            with self.assertRaises(ai.AIRequestError):
                self.call("AI-2")
        self.assertEqual(post.call_count, 3)

    def test_backup_empty_output_can_recover_once_at_low_effort(self):
        self.configure(3)
        ai.os.environ["FIB_PA_AI3_BACKUP_EFFORT"] = "medium"
        with patch.object(ai.requests, "post", side_effect=[Response(status=503), Response(content=""), Response()]) as post:
            self.call("AI-3")
        self.assertEqual([item.kwargs["json"]["reasoning"]["effort"] for item in post.call_args_list], ["medium", "medium", "low"])

    def test_stale_task_never_requests_primary(self):
        self.configure(1)
        with patch.object(ai.requests, "post") as post:
            self.assertEqual(self.call(still_current=lambda: False), "")
        post.assert_not_called()

    def test_stale_task_does_not_switch_after_failed_primary(self):
        self.configure(1)
        alive = [True]
        def expired(*args, **kwargs):
            alive[0] = False
            raise ai.requests.Timeout("offline")
        with patch.object(ai.requests, "post", side_effect=expired) as post:
            self.assertEqual(self.call(still_current=lambda: alive[0]), "")
        self.assertEqual(post.call_count, 1)
        self.sleep_mock.assert_not_called()

    def test_stale_backup_response_is_discarded(self):
        self.configure(1)
        alive = [True]
        attempts = [0]
        def reply(*args, **kwargs):
            attempts[0] += 1
            if attempts[0] == 1:
                raise ai.requests.Timeout("offline")
            alive[0] = False
            return Response()
        with patch.object(ai.requests, "post", side_effect=reply) as post:
            self.assertEqual(self.call(still_current=lambda: alive[0]), "")
        self.assertEqual(post.call_count, 2)

    def test_valid_backup_can_handle_missing_primary_credentials(self):
        self.configure(3)
        with patch.object(ai.requests, "post", return_value=Response()) as post:
            self.call("AI-3", key="")
        self.assertEqual(post.call_count, 1)
        self.assertIn("backup3", post.call_args.args[0])

    def test_missing_both_credentials_never_posts(self):
        with patch.object(ai.requests, "post") as post:
            with self.assertRaises(RuntimeError):
                self.call(key="")
        post.assert_not_called()

    def test_visible_protocol_output_is_left_to_strategy_validation(self):
        self.configure(1)
        content = '{"action":"buy","confidence":100}'
        with patch.object(ai.requests, "post", return_value=Response(content)) as post:
            self.assertEqual(self.call(), content)
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
