"""Logged broker clock lead must not disable otherwise fresh profit protection."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_profit_guardian as support

base = support.base


class GuardianQuoteClockTests(unittest.TestCase):
    def test_actual_successful_post_quotes_are_fresh_for_profit_guard(self):
        sample = json.loads((Path(__file__).parent / "fixtures" /
                             "profit_guard_clock_20261013.json").read_text(encoding="utf-8"))
        for quote in sample["quotes"]:
            with self.subTest(stage=quote["stage"]):
                self.assertTrue(base.fresh_guardian_quote(
                    SimpleNamespace(**quote), quote["observed_at"], sample["server_offset_hours"]))

    def test_lead_and_stale_limits_remain_bounded_with_pinned_offset(self):
        now = 1791861086.125008
        for age, expected in ((-5., True), (-5.001, False), (5., True), (5.001, False),
                              (-30., False), (3600., False)):
            raw = now + 10800 - age
            tick = SimpleNamespace(bid=4180., ask=4180.33, time=int(raw), time_msc=raw * 1000)
            with self.subTest(age=age):
                self.assertEqual(base.fresh_guardian_quote(tick, now, 3.), expected)

    def test_diagnostics_identify_real_clock_and_price_failures(self):
        now = 1791861086.125008
        raw = now + 10800
        good = dict(bid=4180., ask=4180.33, time=int(raw), time_msc=raw * 1000)
        for change, reason in ((dict(ask=4179.), "crossed_quote"),
                               (dict(time_msc=(raw+3)*1000), "timestamp_mismatch"),
                               (dict(time=int(raw+10), time_msc=(raw+10)*1000), "future_quote"),
                               (dict(time=int(raw-10), time_msc=(raw-10)*1000), "stale_quote"),
                               (dict(time_msc=float("nan")), "invalid_clock"),
                               (dict(bid=float("inf")), "invalid_quote")):
            with self.subTest(reason=reason):
                facts = base.guardian_quote_status(SimpleNamespace(**(good | change)), now, 3.)
                self.assertFalse(facts["valid"])
                self.assertEqual(facts["reason"], reason)
                json.dumps(facts, allow_nan=False)
        self.assertFalse(base.fresh_guardian_quote(SimpleNamespace(**good), float("nan"), 3.))
        self.assertEqual(base.guardian_quote_status(SimpleNamespace(**good), now, 20.)["reason"],
                         "unknown_server_offset")


class GuardianClockExecutionTests(unittest.TestCase):
    # Reuse mocked broker setup without inheriting/discovering its test methods.
    setUp = support.ProfitExecutionTests.setUp
    quote = support.ProfitExecutionTests.quote
    poll = support.ProfitExecutionTests.poll
    sell = support.ProfitExecutionTests.sell
    lock = support.ProfitExecutionTests.lock

    def quote_with_broker_lead(self, price, dt=.5):
        self.quote(price, dt)
        self.tick.time_msc += 2635
        self.tick.time = int(self.tick.time_msc / 1000)

    def test_live_lead_allows_both_sides_to_lock_and_rechecks_final_quote(self):
        for sell in (False, True):
            if sell:
                self.sell()
            before = len(self.requests)
            for _ in range(3):
                self.quote_with_broker_lead(95. if sell else 105.)
                self.trader.run_guardian()
            self.assertEqual(len(self.requests), before + 1)
            self.assertAlmostEqual(self.position.sl, 97.94 if sell else 102.06)
            self.assertEqual(self.state.profit_floor.protected_sl, self.position.sl)
            self.assertEqual(self.state.profit_floor.server_offset, 3.)

    def test_ai3_tighten_uses_same_bounded_quote_check(self):
        self.quote_with_broker_lead(105.)
        # POST identity/source validation is independent of this final clock gate.
        self.assertTrue(self.trader.modify_position_sl(self.position,102.,"AI-3 tighten"))
        self.assertEqual(self.position.sl, 102.)
        self.assertEqual(self.state.profit_floor.protected_sl, 102.)

    def test_floor_crossing_close_rechecks_latest_quote_with_broker_lead(self):
        self.lock()
        self.quote_with_broker_lead(102.)
        with patch.object(self.trader, "find_recent_close_deal", return_value=SimpleNamespace()), \
             patch.object(self.trader, "notify_position_closed"):
            self.assertTrue(self.trader.close_position_by_ticket(self.position, "offline floor crossing"))
        self.assertEqual(self.requests[-1]["action"], base.mt5.TRADE_ACTION_DEAL)
        self.assertEqual(self.requests[-1]["position"], 123)

    def test_rejection_resets_confirmation_but_never_changes_saved_floor(self):
        self.quote_with_broker_lead(105.)
        self.trader.run_guardian()
        self.assertGreater(self.state.profit_floor.pending_since, 0.)
        self.quote(105.)
        self.tick.time += 10
        self.tick.time_msc += 10000
        self.trader.run_guardian()
        self.assertEqual(self.state.profit_floor.pending_since, 0.)
        self.assertEqual(self.state.profit_floor.protected_sl, 96.)
        for _ in range(2):
            self.quote_with_broker_lead(105.)
            self.trader.run_guardian()
        self.assertFalse(self.requests)
        self.quote_with_broker_lead(105.)
        self.trader.run_guardian()
        self.assertAlmostEqual(self.position.sl, 102.06)

    def test_repeated_bad_quotes_are_rechecked_while_warning_is_throttled(self):
        with patch.object(base.time, "monotonic", side_effect=lambda: self.now), \
             patch.object(base.logger, "warning") as warning, patch.object(base.logger, "info") as info:
            for dt in (.5, .5, 30.):
                self.quote(105., dt)
                self.tick.time += 10
                self.tick.time_msc += 10000
                self.trader.run_guardian()
            self.assertEqual(warning.call_count, 2)
            args = warning.call_args.args
            message = args[0] % args[1:]
            self.assertIn("reason=future_quote", message)
            self.assertIn("raw_time_msc=", message)
            self.assertIn("offset_hours=3.0", message)
            self.assertIn("age_sec=-10.0", message)
            self.assertFalse(self.requests)
            self.quote_with_broker_lead(105.)
            self.trader.run_guardian()
            self.assertTrue(any("锁盈行情恢复" in call.args[0] for call in info.call_args_list))
            self.assertFalse(self.requests)  # A fresh one-second confirmation starts now.

    def test_identity_rejection_cannot_be_hidden_by_clock_tolerance(self):
        self.quote_with_broker_lead(105.)
        different = SimpleNamespace(**(vars(self.position) | {"identifier": 999}))
        with patch.object(base.logger, "warning") as warning:
            self.trader.run_profit_guardian(different, self.state, self.tick, self.atr)
        args = warning.call_args.args
        message = args[0] % args[1:]
        self.assertIn("reason=identity_changed", message)
        self.assertIn("identity_fields=['identifier']", message)
        self.assertFalse(self.requests)
        self.assertEqual(self.state.profit_floor.protected_sl, 96.)

    def test_ai3_snapshot_distinguishes_actual_sl_intent_and_quote_failure(self):
        self.retcode = 10016  # Broker has not accepted the desired stop.
        self.lock()
        self.quote(105.)
        self.tick.time += 10
        self.tick.time_msc += 10000
        snapshot = self.trader.collect_ai3_snapshot()
        item = json.loads(snapshot["prompt"].split("\n", 1)[1])["positions"][0]
        guard = item["profit_guard"]
        self.assertEqual(guard["broker_sl"], 96.)
        self.assertEqual(guard["protected_sl"], 96.)
        self.assertGreater(guard["desired_sl"], 100.)
        self.assertFalse(guard["quote_status"]["valid"])
        self.assertEqual(guard["quote_status"]["reason"], "future_quote")


if __name__ == "__main__":
    unittest.main()
