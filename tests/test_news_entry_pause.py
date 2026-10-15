"""Real AI entry chain and scheduler with injected calendar, HTTP and MT5."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from strategies import news_calendar as n
import test_adaptive_ai_integration as entry_fixture
from test_news_calendar import schedules

ai, base, Response = entry_fixture.ai, entry_fixture.base, entry_fixture.Response

class NewsEntryTests(unittest.TestCase):
    def setUp(self):
        self.f = entry_fixture.AdaptiveAITests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.configure()
        self.trader = self.f.trader
        self.roles = []
        self.calendar = n.NewsCalendar(self.f.directory/"news.json", enabled=True)
        self.calendar.request_refresh = lambda: None
        self.trader.news_calendar = self.calendar
        self.release_at = self.f.now+901
        self.set_release(self.release_at)

    def set_release(self, at):
        self.calendar._frames = schedules(self.f.now-3600, [n.event("cpi", at, n.BLS_URL)])

    def advance(self, seconds, fresh_bars=False):
        self.f.now += seconds
        self.f.tick.time = int(self.f.now+10800)
        if fresh_bars:
            for data, step in zip(self.f.data, (900, 3600, 300)):
                end = int((self.f.now+10800)//step)*step
                data["time"] = [end-(len(data)-i)*step for i in range(len(data))]

    def entry(self, on_post=None):
        def post(url, **kwargs):
            data = kwargs["json"]
            pack = json.loads(data["messages"][-1]["content"].split("\n", 1)[1])
            role = "AI-2" if "proposal" in pack else "AI-1"
            self.roles.append(role)
            value = ({"decision": "APPROVE", "defects": [], "fact_conflicts": [], "reasoning": "固定SLTP及实时风险通过"}
                     if role == "AI-2" else self.f.proposal(data))
            if on_post:
                on_post(role)
            return Response(value)
        with patch.object(ai.requests, "post", side_effect=post) as http:
            stamp = int(self.f.data[0].time.iloc[-1])
            seq = self.trader.claim_task("entry", stamp, 360)
            self.trader.entry_task(stamp, seq)
        return http

    def assert_no_order_or_reservation(self):
        self.assertEqual(self.f.sent, [])
        self.assertEqual(self.trader.strategy_ledger.snapshot().get("events", {}), {})

    def test_starting_in_window_has_zero_http_scopes_reservations(self):
        self.set_release(self.f.now)
        http = self.entry()
        http.assert_not_called()
        self.assert_no_order_or_reservation()
        self.assertFalse(getattr(self.trader, "_analysis_request_scopes", set()))
        self.assertIn("news_pause", [s[1] for s in self.f.snapshots])

    def test_final_snapshot_construction_crosses_boundary_before_scope_claim_or_http(self):
        original = ai.response_json_schema
        def schema(role, pack=None):
            result = original(role, pack)
            if role == "AI-1" and pack is not None:
                self.advance(2)
            return result
        with patch.object(ai, "response_json_schema", side_effect=schema):
            http = self.entry()
        http.assert_not_called()
        self.assert_no_order_or_reservation()
        self.assertFalse(getattr(self.trader, "_analysis_request_scopes", set()))

    def test_ai1_late_response_stops_before_ai2(self):
        http = self.entry(lambda role: self.advance(2) if role == "AI-1" else None)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.roles, ["AI-1"])
        self.assert_no_order_or_reservation()

    def test_ai2_late_approval_stops_before_order(self):
        http = self.entry(lambda role: self.advance(2) if role == "AI-2" else None)
        self.assertEqual(http.call_count, 2)
        self.assert_no_order_or_reservation()

    def test_ai1_retry_entering_window_never_sends_backup(self):
        def fail(role):
            if role == "AI-1":
                self.advance(2)
                raise ai.requests.ReadTimeout("mock")
        with patch.object(ai, "backup_chat_config", return_value={
                "model": "mock-backup", "effort": "low", "url": "https://backup.test/chat", "key": "mock-backup"}):
            http = self.entry(fail)
        self.assertEqual(http.call_count, 1)
        self.assert_no_order_or_reservation()
        self.assertIn("news_pause", [s[1] for s in self.f.snapshots])

    def test_ai2_retry_entering_window_never_sends_backup(self):
        def fail(role):
            if role == "AI-2":
                self.advance(2)
                raise ai.requests.ReadTimeout("mock")
        with patch.object(ai, "backup_chat_config", return_value={
                "model": "mock-backup", "effort": "low", "url": "https://backup.test/chat", "key": "mock-backup"}):
            http = self.entry(fail)
        self.assertEqual(http.call_count, 2)
        self.assert_no_order_or_reservation()

    def test_response_after_entire_30_minute_window_does_not_queue_old_proposal(self):
        http = self.entry(lambda role: self.advance(1802) if role == "AI-1" else None)
        self.assertEqual(http.call_count, 1)
        self.assert_no_order_or_reservation()
        self.assertIn("跨过", next(s[2]["reason"] for s in self.f.snapshots if s[1] == "news_pause"))

    def test_resume_fresh_bars_runs_complete_chain_after_initial_pause_without_lost_scope(self):
        self.set_release(self.f.now)
        self.entry().assert_not_called()
        self.advance(1800, fresh_bars=True)
        http = self.entry()
        summary = [(s[1], s[2].get("reason")) for s in self.f.snapshots]
        self.assertEqual(http.call_count, 2, summary)
        self.assertEqual(len(self.f.sent), 1, summary)
        self.assertEqual(self.f.sent[0]["volume"], .01)
        self.assertLessEqual(abs(self.f.sent[0]["price"]-self.f.sent[0]["sl"]), 8)

    def test_last_broker_gate_catches_window_after_ai2_and_preserves_unconsumed_event(self):
        original = base.FibVolumePATrader.place_order
        def final_broker_gate(trader, result):
            # Inject the boundary at the broker layer, independent of how many
            # earlier source checks the versioned direction policy performs.
            self.advance(2)
            return original(trader, result)
        with patch.object(base.FibVolumePATrader, "place_order", new=final_broker_gate):
            http = self.entry()
        self.assertEqual(http.call_count, 2)
        self.assert_no_order_or_reservation()

    def test_boundary_after_durable_reservation_blocks_send_and_never_rolls_back_event(self):
        original = self.trader.strategy_ledger.reserve
        def reserve(*args, **kwargs):
            result = original(*args, **kwargs)
            self.advance(2)
            return result
        with patch.object(self.trader.strategy_ledger, "reserve", side_effect=reserve) as hold:
            self.entry()
        hold.assert_called_once()
        self.assertEqual(self.f.sent, [])
        self.assertEqual(len(self.trader.strategy_ledger.snapshot()["events"]), 1)

    def test_scheduler_keeps_ai3_local_guard_and_close_listener_alive_during_pause(self):
        self.set_release(self.f.now)
        def stop(_):
            self.trader._stop.set()
        with patch.object(self.trader, "connect_mt5", return_value=True), \
             patch.object(self.trader, "run_guardian") as guard, \
             patch.object(self.trader, "sync_position_close_notifications") as closes, \
             patch.object(self.trader, "latest_bar_time", return_value=int(self.f.now+10800)), \
             patch.object(self.trader, "dispatch_entry") as entry, \
             patch.object(self.trader, "dispatch_m5_entry") as m5, \
             patch.object(self.trader, "dispatch_ai3") as ai3, \
             patch.object(self.trader, "notify_lines"), \
             patch.object(base.mt5, "shutdown"), patch.object(ai.time, "sleep", side_effect=stop):
            self.trader.run()
        entry.assert_not_called()
        m5.assert_not_called()
        guard.assert_called_once()
        closes.assert_called_once()
        ai3.assert_called_once()
        self.assertFalse(self.trader._analysis_request_scopes if hasattr(self.trader, "_analysis_request_scopes") else False)

    def test_pause_does_not_apply_to_ai3_http_request(self):
        self.set_release(self.f.now)
        with patch.object(ai.requests, "post", return_value=Response({"action": "hold", "reasoning": "原SLTP保留"})) as http:
            response = ai.call_chat("AI-3", "mock", "low", "guardian", "current position", 10, "https://unit.test/chat", "mock")
        self.assertTrue(response)
        http.assert_called_once()

    def test_unavailable_calendar_stops_only_new_entry_with_explicit_reason(self):
        self.calendar._frames = []
        self.entry().assert_not_called()
        self.assert_no_order_or_reservation()
        self.assertIn("日历不可用", next(s[2]["reason"] for s in self.f.snapshots if s[1] == "news_pause"))


if __name__ == "__main__":
    unittest.main()
