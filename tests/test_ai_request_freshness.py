"""Offline regressions for fresh retry evidence and delayed execution; no live I/O."""
import copy
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai


class Response:
    status_code, text, headers = 200, "", {}
    def json(self):
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"action":"no"}'}}]}


def frame(stamp=1000):
    return ai.base.pd.DataFrame([{"time": stamp, "open": 100., "high": 102.,
                                 "low": 98., "close": 100., "tick_volume": 100}])


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for scope in (patch.dict(ai.os.environ, {"FIB_PA_NEWS_PAUSE_ENABLED": "false"}, clear=True),
                      patch.object(ai, "_PRIMARY_TIMEOUT_BACKOFF", ai.PrimaryTimeoutBackoff()),
                      patch.dict(ai._CHAT_OPTIONS, {}, clear=True),
                      patch.object(ai, "AI3_STATE_FILE", str(Path(self.temp.name) / "state.json")),
                      patch.object(ai, "AI_USAGE_DIR", self.temp.name),
                      patch.object(ai, "AI1_URL", "https://primary.example.test/chat"),
                      patch.object(ai, "append_decision_snapshot"),
                      patch.object(ai.time, "sleep"),
                      patch.object(ai.base, "send_feishu_notification")):
            scope.start()
            self.addCleanup(scope.stop)
        self.trader = ai.ThreeAITrader()

    def test_every_primary_backup_and_parameter_retry_gets_a_new_quote(self):
        for number in (1, 2, 3):
            with self.subTest(role=number), patch.dict(ai.os.environ, {
                    f"AI{number}_BACKUP_URL": "https://backup.example.test/chat"}):
                quotes = iter((100., 101., 102.))
                def refresh():
                    bid = next(quotes)
                    return {"prompt": json.dumps({"bid": bid}), "snapshot_id": f"s-{bid}",
                            "quote": {"bid": bid, "ask": bid + .1, "time": int(bid), "observed_at": bid}}
                bad_parameter = SimpleNamespace(status_code=400, text="unsupported reasoning_effort",
                                                headers={}, json=lambda: {})
                with patch.object(ai.requests, "post", side_effect=[ai.requests.ReadTimeout("slow"),
                                   bad_parameter, Response()]) as post:
                    ai.call_chat(f"AI-{number}", "model", "low", "rules", "OLD", 40,
                                 "https://primary.example.test/chat", "key", refresh_request=refresh)
                self.assertEqual([json.loads(c.kwargs["json"]["messages"][1]["content"])["bid"]
                                  for c in post.call_args_list], [100., 101., 102.])
                self.assertEqual(post.call_count, 3)
                self.assertTrue(all(len(c.kwargs["json"]["messages"]) == 2 for c in post.call_args_list))
                usage = [json.loads(line) for p in Path(self.temp.name).glob("*.jsonl")
                         for line in p.read_text(encoding="utf-8").splitlines()][-3:]
                self.assertEqual([event["request_quote"]["bid"] for event in usage], [100., 101., 102.])
                self.assertEqual(len({event["snapshot_id"] for event in usage}), 3)

    def test_quote_refresh_failure_never_resends_the_previous_prompt(self):
        with patch.dict(ai.os.environ, {"AI1_BACKUP_URL": "https://backup.example.test/chat"}):
            replies = iter(({"prompt": "fresh", "snapshot_id": "s1", "quote": {}}, None))
            with patch.object(ai.requests, "post", side_effect=ai.requests.ReadTimeout("slow")) as post:
                with self.assertRaises(ai.MarketSnapshotError):
                    ai.call_chat("AI-1", "model", "low", "rules", "OLD", 40,
                                 "https://primary.example.test/chat", "key", refresh_request=lambda: next(replies))
            self.assertEqual(post.call_count, 1)

    def test_stopped_task_does_not_collect_or_post(self):
        with patch.object(ai.requests, "post") as post, patch.object(self.trader, "get_context") as context:
            self.assertEqual(ai.call_chat("AI-1", "model", "low", "rules", "OLD", 40,
                             "https://primary.example.test/chat", "key", still_current=lambda: False,
                             refresh_request=context), "")
        context.assert_not_called()
        post.assert_not_called()

    def test_ai1_normalizes_and_finalizes_the_successful_retry_snapshot(self):
        local = ai.base.StrategyResult("sell", 85, False, "candidate", 104., 92., 0, 85, {"entry_price": 100.})
        pack = {"h1_trend": "bear", "m15_trend": "bear", "regime": "trend",
                "live_quote": {}, "bar_closure": {}, "reference_price": {"price": 100.}}
        ticks = iter(SimpleNamespace(bid=b, ask=b + .1, time=i)
                     for i, b in enumerate((99., 100., 101.), start=1))
        def build(*args, quote=None, **kwargs):
            result = copy.deepcopy(pack)
            result["live_quote"] = quote
            return result, None, 2., 100.
        with patch.dict(ai.os.environ, {"AI1_BACKUP_URL": "https://backup.example.test/chat"}), \
             patch.object(self.trader, "get_context", return_value=(frame(), frame(), frame())), \
             patch.object(self.trader, "evaluate_context", return_value=local), \
             patch.object(ai.base.mt5, "symbol_info_tick", side_effect=lambda _: next(ticks)), \
             patch.object(ai, "build_feature_pack", side_effect=build), \
             patch.object(self.trader, "entry_is_current", return_value=True), \
             patch.object(self.trader, "release_task"), patch.object(self.trader, "notify_lines"), \
             patch.object(ai.requests, "post", side_effect=[ai.requests.ReadTimeout("slow"), Response()]) as post, \
             patch.object(ai, "normalize_ai1_quality", return_value=({"confidence": 80}, "")) as normalize, \
             patch.object(ai, "finalize_ai_proposal", return_value=(None, "stop after normalization")) as finalize:
            self.trader.entry_task(1, 1)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(normalize.call_args.args[1]["live_quote"]["bid"], 101.)
        self.assertEqual(finalize.call_args.args[1], 100.)  # M15 reference is unchanged.

    def review_fixture(self):
        frames = (frame(), frame(), frame())
        pack = {"snapshot_id": "ai1-original", "live_quote": {"bid": 100., "ask": 100.1, "time": 1},
                "bar_closure": ai.closed_context_times(*frames), "atr": 2., "h1_trend": "bear",
                "m15_trend": "bear", "local_action": "sell", "local_reasoning": "candidate",
                "local_stop_loss": 104., "local_take_profit": 92., "reference_price": {"price": 100.}}
        proposal = ai.base.StrategyResult("sell", 80, False, "review me", 104., 92., 0, 85,
                                         {"entry_price": 100., "execution_guard": {"atr": 2.}})
        return frames, pack, proposal

    def test_ai2_uses_fresh_quote_without_overwriting_ai1_evidence_or_plan(self):
        frames, pack, proposal = self.review_fixture()
        pack["local_plan_feasibility"] = {"reference_entry": 99.9}
        proposal.details["reference_entry_price"] = 99.8
        original = copy.deepcopy(pack)
        with patch.object(self.trader, "get_context", return_value=frames), \
             patch.object(ai.base.mt5, "symbol_info_tick", return_value=SimpleNamespace(bid=101., ask=101.1, time=2)):
            request = self.trader.prepare_review_request(proposal, pack, {}, "fixed", "d1", "M5")
        review = json.loads(request["prompt"].split("\n", 1)[1])
        self.assertEqual(review["market_evidence"]["live_quote"]["bid"], 101.)
        self.assertEqual(review["final_plan_feasibility"]["live_risk"], 3.)
        self.assertEqual(review["python_finalization"]["reference_entry_price"], 99.8)
        self.assertEqual(review["market_evidence"]["local_plan_feasibility"]["reference_entry"], 99.9)
        self.assertEqual(review["ai1_snapshot"]["quote"]["bid"], 100.)
        self.assertEqual((proposal.stop_loss, proposal.take_profit), (104., 92.))
        self.assertEqual(pack, original)

    def test_new_closed_bar_stops_review_of_old_proposal(self):
        frames, pack, proposal = self.review_fixture()
        with patch.object(self.trader, "get_context", return_value=(frames[0], frames[1], frame(1300))):
            with self.assertRaises(ai.MarketSnapshotError):
                self.trader.prepare_review_request(proposal, pack, {}, "fixed", "d1", "M5")

    def test_historical_instant_stop_case_is_rejected_before_order_send(self):
        result = ai.base.StrategyResult("sell", 80, False, "approved", 4128.63, 4117.91, 0, 85,
                                       {"execution_guard": {"atr": 2., "ema21": 4126.}})
        with patch.object(self.trader, "current_position_count", return_value=0), \
             patch.object(ai.base, "ENABLE_TRADING", True), \
             patch.object(ai.base.mt5, "symbol_info_tick", return_value=SimpleNamespace(bid=4128.12, ask=4128.45)), \
             patch.object(ai.base.mt5, "order_send", return_value=None) as send:
            self.assertFalse(ai.base.FibVolumePATrader.place_order(self.trader, result))
        send.assert_not_called()

    def test_execution_guard_checks_buy_sell_stop_side_and_live_chase(self):
        for action, sl, tp, bid, ask in (("sell", 104., 92., 103.5, 103.8),
                                       ("buy", 96., 108., 96.2, 96.5)):
            result = ai.base.StrategyResult(action, 80, False, "approved", sl, tp, 80, 80,
                                           {"execution_guard": {"atr": 1., "ema21": 100.}})
            self.assertTrue(self.trader.validate_execution_prices(result, SimpleNamespace(bid=bid, ask=ask)))
        result = ai.base.StrategyResult("buy", 80, False, "approved", 98., 110., 80, 0,
                                       {"execution_guard": {"atr": 1., "ema21": 100.}})
        self.assertTrue(self.trader.validate_execution_prices(result, SimpleNamespace(bid=102., ask=102.1)))

    def test_valid_live_quote_keeps_existing_stop_and_target(self):
        result = ai.base.StrategyResult("sell", 80, False, "approved", 104., 92., 0, 80,
                                       {"execution_guard": {"atr": 2., "ema21": 100.}})
        self.assertEqual(self.trader.validate_execution_prices(result, SimpleNamespace(bid=100., ask=100.2)), "")
        self.assertEqual((result.stop_loss, result.take_profit), (104., 92.))

    def test_ai2_backup_rechecks_price_and_stops_a_now_infeasible_plan(self):
        frames, pack, proposal = self.review_fixture()
        with patch.dict(ai.os.environ, {"AI2_BACKUP_URL": "https://backup.example.test/chat"}), \
             patch.object(self.trader, "get_context", return_value=frames), \
             patch.object(ai.base.mt5, "symbol_info_tick", side_effect=[
                 SimpleNamespace(bid=100., ask=100.1), SimpleNamespace(bid=103.8, ask=103.9)]), \
             patch.object(ai.requests, "post", side_effect=ai.requests.ReadTimeout("slow")) as post:
            with self.assertRaises(ai.MarketSnapshotError):
                ai.call_chat("AI-2", "model", "low", "rules", "", 40,
                             "https://primary.example.test/chat", "key", refresh_request=lambda:
                             self.trader.prepare_review_request(proposal, pack, {}, "fixed", "d1", "M5"))
        self.assertEqual(post.call_count, 1)
        self.assertEqual((proposal.stop_loss, proposal.take_profit), (104., 92.))

    def test_new_closed_bar_after_ai2_approval_prevents_order_send(self):
        frames, pack, proposal = self.review_fixture()
        proposal.details["execution_guard"]["bar_closure"] = pack["bar_closure"]
        with patch.object(self.trader, "get_context", return_value=(frame(1900), frames[1], frames[2])), \
             patch.object(ai.base.mt5, "order_send") as send:
            self.assertFalse(self.trader.place_order(proposal))
        send.assert_not_called()

    def test_open_notification_uses_fill_price_not_request_quote(self):
        result = ai.base.StrategyResult("sell", 80, False, "approved", 104., 92., 0, 80,
                                       {"execution_guard": {"atr": 2., "ema21": 100.}})
        with patch.object(self.trader, "current_position_count", return_value=0), \
             patch.object(self.trader, "sync_position_close_notifications"), \
             patch.object(ai.base, "ENABLE_TRADING", True), \
             patch.object(ai.base.mt5, "symbol_info_tick", return_value=SimpleNamespace(bid=100., ask=100.2)), \
             patch.object(ai.base.mt5, "order_send", return_value=SimpleNamespace(
                 retcode=ai.base.mt5.TRADE_RETCODE_DONE, price=100.12, order=123)), \
             patch.object(ai.base, "send_feishu_notification") as notify:
            self.assertTrue(ai.base.FibVolumePATrader.place_order(self.trader, result))
        self.assertIn("成交价: 100.12", notify.call_args.args[1])
        self.assertIn("提交报价: 100.0", notify.call_args.args[1])

    def guardian_position(self, profit=0):
        return SimpleNamespace(ticket=123, type=ai.base.mt5.POSITION_TYPE_BUY, price_open=100.,
                               time=1000, volume=.01, sl=98., tp=104., profit=profit)

    def test_ai3_retry_refreshes_quote_profit_and_closed_bars(self):
        ticks = iter(SimpleNamespace(bid=p, ask=p + .1, time=t) for p, t in ((100., 1100), (101., 1200), (102., 1300)))
        positions = iter(([self.guardian_position(p)] for p in (0, 1, 2, 2)))
        with patch.dict(ai.os.environ, {"AI3_BACKUP_URL": "https://backup.example.test/chat"}), \
             patch.object(ai, "AI3_URL", "https://primary.example.test/v1/messages"), \
             patch.object(ai, "AI3_KEY", "test-key"), \
             patch.object(self.trader, "get_positions", side_effect=lambda: next(positions)), \
             patch.object(self.trader, "get_rates", return_value=frame()), \
             patch.object(ai.base.mt5, "symbol_info_tick", side_effect=lambda _: next(ticks)), \
             patch.object(self.trader, "ai3_is_current", return_value=True), \
             patch.object(self.trader, "release_task"), patch.object(self.trader, "execute_ai3"), \
             patch.object(ai.requests, "post", side_effect=[ai.requests.ReadTimeout("slow"), Response()]) as post:
            self.trader.ai3_task(1, 1)
        self.assertEqual(post.call_count, 2)
        payloads = [json.loads(c.kwargs["json"]["messages"][-1]["content"].split("\n", 1)[1])
                    for c in post.call_args_list]
        self.assertEqual([p["bid"] for p in payloads], [101., 102.])
        self.assertEqual([p["positions"][0]["profit"] for p in payloads], [1., 2.])
        for sent in payloads:
            self.assertLess(list(sent).index("positions"), list(sent).index("snapshot_id"))
            position_keys = list(sent["positions"][0])
            self.assertLess(position_keys.index("entry_context"), position_keys.index("profit"))
        usage = [json.loads(line) for p in Path(self.temp.name).glob("*.jsonl")
                 for line in p.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(event["input_pack_version"] == ai.AI3_INPUT_PACK_VERSION for event in usage))

    def test_ai3_does_not_retry_for_a_new_replacement_position(self):
        original, replacement = self.guardian_position(), self.guardian_position()
        replacement.ticket = 456
        with patch.dict(ai.os.environ, {"AI3_BACKUP_URL": "https://backup.example.test/chat"}), \
             patch.object(self.trader, "get_positions", side_effect=[[original], [original], [replacement]]), \
             patch.object(self.trader, "get_rates", return_value=frame()), \
             patch.object(ai.base.mt5, "symbol_info_tick", return_value=SimpleNamespace(bid=100., ask=100.1, time=1100)), \
             patch.object(self.trader, "ai3_is_current", return_value=True), \
             patch.object(self.trader, "release_task"), patch.object(self.trader, "execute_ai3") as execute, \
             patch.object(ai.requests, "post", side_effect=ai.requests.ReadTimeout("slow")) as post:
            self.trader.ai3_task(1, 1)
        self.assertEqual(post.call_count, 1)
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
