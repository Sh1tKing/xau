"""Deferred UTC confirmation using exact host callbacks and fake broker reads."""
import ast
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from strategies import recent_trades as recent
import test_recent_trades_integration as helpers

NOW, TICKET = helpers.NOW, helpers.TICKET


class SettlementClockTests(unittest.TestCase):
    cache = helpers.RecentTradesIntegrationTests.cache

    def make(self):
        trader, snapshot = helpers.RecentTradesIntegrationTests.make(self)
        scope = trader._recent_namespace.__globals__
        source = Path(__file__).resolve().parents[1] / "xauusd_fib_volume_pa_trader.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "tick_clock_offset")
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), scope)
        return trader, snapshot

    def setUp(self):
        helpers.RecentTradesIntegrationTests.setUp(self)
        self.broker.symbol_info_tick.return_value = SimpleNamespace(time=NOW+10800)
        self.future = replace(self.settled, time=int(NOW+10802))

    def raw(self, trader=None):
        return json.loads(Path((trader or self.trader)._recent_trade_path).read_text(encoding="utf-8"))

    def row(self, state=None):
        return next(iter((state or self.raw())["partitions"].values()))["records"][0]

    def advance(self, seconds):
        self.clock.time.return_value = NOW+seconds
        self.broker.symbol_info_tick.return_value = SimpleNamespace(time=NOW+seconds+10800)

    def test_new_future_close_is_saved_then_confirmed_without_duplicate_notice(self):
        self.assertTrue(self.trader.notify_position_closed(TICKET, self.snapshot, self.future))
        state = self.raw()
        self.assertIsNone(self.row(state)["close_utc"])
        self.assertTrue(state.get("pending_close_utc"))
        self.assertEqual(self.cache()["records"], [])
        self.advance(3)
        self.trader.sync_position_close_notifications()
        row = self.cache()["records"][0]
        self.assertEqual(row["close_utc"], NOW+2)
        self.assertEqual(row["observed_utc"], NOW+3)
        self.assertEqual(row["broker_close_time"], self.future.time)
        self.assertAlmostEqual(row["net_profit"], 3.65)
        self.assertNotIn("pending_close_utc", self.raw())
        self.assertEqual(self.notify.call_count, 1)
        self.broker.history_deals_get.assert_not_called()

    def test_context_read_confirms_due_pending_and_does_not_send_notice(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        self.advance(2)
        self.assertEqual(self.cache()["records"][0]["close_utc"], NOW+2)
        self.notify.assert_not_called()

    def test_restart_resumes_original_pending_before_deadline(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        self.advance(3)
        restored, _ = self.make()
        result = restored.recent_trade_context()
        self.assertEqual(result["records"][0]["close_utc"], NOW+2)
        self.assertEqual(result["coverage"]["started_utc"], NOW)
        self.notify.assert_not_called()

    def test_not_visible_to_earlier_observation_even_after_confirmation(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        self.advance(3)
        self.cache()
        key = self.snapshot.settlement_namespace
        self.assertEqual(recent.recent_context(self.raw(), key, NOW+2.5)["records"], [])

    def test_five_second_lead_boundary_is_allowed_but_more_is_not(self):
        for lead, allowed in ((5, True), (6, False)):
            with self.subTest(lead=lead):
                state = recent.record_settlement(recent.new_state(NOW), self.snapshot.settlement_namespace,
                    replace(self.settled, time=int(NOW+10800+lead)), direction="BUY",
                    observed_utc=NOW, close_utc=None, verified_offset=3)
                self.assertEqual(bool(state.get("pending_close_utc")), allowed)

    def test_missing_or_initially_changed_offset_never_creates_pending(self):
        for tick in (None, SimpleNamespace(time=NOW+7200), SimpleNamespace(time=NOW+10760)):
            with self.subTest(tick=tick):
                self.broker.symbol_info_tick.return_value = tick
                state = recent.new_state(NOW)
                self.trader._recent_trade_state = state
                self.trader.record_recent_settlement(self.snapshot, self.future)
                self.assertNotIn("pending_close_utc", self.raw())

    def test_fresh_offset_change_cancels_pending_instead_of_reinterpreting(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        self.advance(3)
        self.broker.symbol_info_tick.return_value = SimpleNamespace(time=NOW+3+7200)
        self.assertEqual(self.cache()["records"], [])
        self.assertNotIn("pending_close_utc", self.raw())
        self.advance(4)
        self.assertEqual(self.cache()["records"], [])

    def test_missing_quote_waits_until_a_fresh_quote_is_available(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        self.advance(3)
        self.broker.symbol_info_tick.return_value = None
        self.assertEqual(self.cache()["records"], [])
        self.assertTrue(self.raw().get("pending_close_utc"))
        self.advance(4)
        self.assertEqual(self.cache()["records"][0]["close_utc"], NOW+2)

    def test_expiry_is_fixed_and_repeated_delivery_cannot_extend_it(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        initial = deepcopy(self.raw()["pending_close_utc"])
        self.advance(1)
        self.trader.record_recent_settlement(self.snapshot, self.future)
        self.assertEqual(self.raw()["pending_close_utc"], initial)
        self.advance(121)
        self.assertEqual(self.cache()["records"], [])
        self.assertNotIn("pending_close_utc", self.raw())

    def test_restart_after_expiry_does_not_backfill(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        self.advance(121)
        restored, _ = self.make()
        self.assertEqual(restored.recent_trade_context()["records"], [])
        self.assertIsNone(self.row(self.raw(restored))["close_utc"])

    def test_backward_clock_cancels_pending(self):
        state = recent.record_settlement(recent.new_state(NOW-100), self.snapshot.settlement_namespace,
            self.future, direction="BUY", observed_utc=NOW, close_utc=None, verified_offset=3)
        result = recent.confirm_pending_utc(state, self.snapshot.settlement_namespace,
                                            observed_utc=NOW-1, verified_offset=3)
        self.assertNotIn("pending_close_utc", result)
        self.assertIsNone(self.row(result)["close_utc"])

    def test_account_switch_does_not_resolve_or_transfer_another_partition(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        before = self.raw()
        self.advance(3)
        self.broker.account_info.return_value = SimpleNamespace(login=9876, server="other-demo")
        self.assertEqual(self.cache()["records"], [])
        self.assertEqual(self.raw(), before)

    def test_account_switch_during_tick_read_does_not_persist_confirmation(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        before = self.raw()
        self.advance(3)
        def tick(*args):
            self.broker.account_info.return_value = SimpleNamespace(login=9876, server="other-demo")
            return SimpleNamespace(time=NOW+10803)
        self.broker.symbol_info_tick.side_effect = tick
        self.trader.sync_recent_trade_times()
        self.assertEqual(self.raw(), before)

    def test_legacy_unknown_record_is_not_given_new_provenance(self):
        key = self.snapshot.settlement_namespace
        state = recent.record_settlement(recent.new_state(NOW), key, self.future,
            direction="BUY", observed_utc=NOW, close_utc=None)
        # Repeating an existing record cannot manufacture a missing origin marker.
        state = recent.record_settlement(state, key, self.future,
            direction="BUY", observed_utc=NOW+1, close_utc=None, verified_offset=3)
        self.assertNotIn("pending_close_utc", state)
        result = recent.confirm_pending_utc(state, key, observed_utc=NOW+3, verified_offset=3)
        self.assertEqual(result, state)

    def test_pending_facts_never_appear_in_public_context(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        context = json.dumps(self.cache())
        for text in ("pending_close_utc", "record_sha256", self.snapshot.settlement_namespace, "offset_hours"):
            self.assertNotIn(text, context)

    def test_tampered_pending_identity_or_record_is_rejected(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        original = self.raw()
        key = self.snapshot.settlement_namespace
        for field, value in (("offset_hours", 2), ("position_id", TICKET+1),
                             ("record_sha256", "0"*64), ("expires_utc", NOW+999999),
                             ("version", "old"), ("observed_utc", NOW-1)):
            with self.subTest(field=field):
                broken = deepcopy(original)
                broken["pending_close_utc"][key][0][field] = value
                self.assertFalse(recent.recent_context(broken, key, NOW)["available"])

    def test_confirmation_write_failure_preserves_financial_record_and_pending(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        before = self.raw()
        self.advance(3)
        with patch.object(os, "replace", side_effect=OSError("isolated write failure")):
            context = self.cache()
        self.assertFalse(context["available"])
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.trader._recent_trade_state, before)
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()),
                         ["recent_settled_trades.json"])

    def test_confirmation_is_idempotent_after_json_round_trip(self):
        key = self.snapshot.settlement_namespace
        self.trader.record_recent_settlement(self.snapshot, self.future)
        state = recent.confirm_pending_utc(self.raw(), key, observed_utc=NOW+3, verified_offset=3)
        repeat = recent.confirm_pending_utc(json.loads(json.dumps(state)), key,
                                            observed_utc=NOW+10, verified_offset=3)
        self.assertEqual(state, repeat)

    def test_pending_capacity_follows_record_eviction(self):
        key = self.snapshot.settlement_namespace
        state = recent.new_state(NOW)
        for index in range(66):
            value = replace(self.future, position_id=TICKET+index,
                            deal_ids=(90001+index*2, 90002+index*2))
            state = recent.record_settlement(state, key, value, direction="BUY",
                observed_utc=NOW, close_utc=None, verified_offset=3)
        self.assertEqual(len(state["partitions"][key]["records"]), 64)
        self.assertEqual(len(state["pending_close_utc"][key]), 64)
        self.assertEqual(min(row["position_id"] for row in state["pending_close_utc"][key]), TICKET+2)
        final = recent.confirm_pending_utc(state, key, observed_utc=NOW+3, verified_offset=3)
        self.assertNotIn("pending_close_utc", final)
        self.assertTrue(recent.recent_context(final, key, NOW+3)["available"])

    def test_exact_expiry_can_confirm_and_after_expiry_cannot(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        state, key = self.raw(), self.snapshot.settlement_namespace
        on_time = recent.confirm_pending_utc(state, key, observed_utc=NOW+120, verified_offset=3)
        too_late = recent.confirm_pending_utc(state, key, observed_utc=NOW+120.001, verified_offset=3)
        self.assertEqual(self.row(on_time)["close_utc"], NOW+2)
        self.assertIsNone(self.row(too_late)["close_utc"])

    def test_conflicting_settlement_cannot_replace_pending_provenance(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        state, key = self.raw(), self.snapshot.settlement_namespace
        before = deepcopy(state)
        with self.assertRaises(recent.RecentTradesError):
            recent.record_settlement(state, key, replace(self.future, profit=123.), direction="BUY",
                observed_utc=NOW+1, close_utc=None, verified_offset=3)
        self.assertEqual(state, before)

    def test_invalid_offset_and_duplicate_pending_provenance_are_rejected(self):
        self.trader.record_recent_settlement(self.snapshot, self.future)
        state, key = self.raw(), self.snapshot.settlement_namespace
        for offset in (True, float("nan"), float("inf"), "3"):
            with self.subTest(offset=offset), self.assertRaises(recent.RecentTradesError):
                recent.confirm_pending_utc(state, key, observed_utc=NOW+3, verified_offset=offset)
        state["pending_close_utc"][key] *= 2
        self.assertFalse(recent.recent_context(state, key, NOW)["available"])
