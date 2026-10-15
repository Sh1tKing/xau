"""Finite settled facts, identity isolation, durable coverage and temporal limits."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

from strategies import recent_trades as recent
from strategies.settlement import PositionSettlement, SettlementError, settle_position_deals


NOW = 1800000000.


def settlement(position=1, **changes):
    value = PositionSettlement(position, (position * 2, position * 2 + 1),
                               2., -.2, -.1, -.05, .01, 4100., 4102., 5, 123456)
    return replace(value, **changes)


class RecentTradesTests(unittest.TestCase):
    def setUp(self):
        self.key = recent.namespace(123456, "broker-demo", "XAUUSD", 123)
        self.state = recent.new_state(NOW - 100)

    def record(self, state=None, value=None, **changes):
        params = dict(direction="BUY", observed_utc=NOW, close_utc=NOW - 1)
        params.update(changes)
        return recent.record_settlement(self.state if state is None else state,
                                        self.key, value or settlement(), **params)

    def context(self, state, now=NOW):
        return recent.recent_context(state, self.key, now)

    def test_real_fees_and_broker_fields_are_preserved_without_estimating(self):
        state = self.record(entry_context={"route": "m15_tactical", "h1_background": "bear"})
        result = self.context(state)
        row = result["records"][0]
        self.assertAlmostEqual(row["net_profit"], 1.65)
        for field in ("profit", "commission", "swap", "fee", "price_open", "price_close", "volume"):
            self.assertEqual(row[field], getattr(settlement(), field))
        self.assertEqual(row["broker_close_time"], 123456)
        self.assertEqual(row["observed_utc"], NOW)
        self.assertEqual(row["close_utc"], NOW - 1)
        self.assertEqual(row["reason_code"], 5)
        self.assertNotIn("reason", row)
        self.assertEqual(row["entry_context"]["h1_background"], "bear")
        json.dumps(state, allow_nan=False)
        json.dumps(result, allow_nan=False)

    def test_strict_broker_aggregation_is_the_only_input_not_partial_raw_rows(self):
        def row(ticket, entry, volume, profit, commission, price):
            return SimpleNamespace(ticket=ticket, position_id=1, symbol="XAUUSD",
                                   type=0 if entry == 0 else 1, entry=entry, volume=volume,
                                   profit=profit, commission=commission, swap=-.01, fee=-.02,
                                   price=price, reason=5, time=123456 + ticket,
                                   time_msc=(123456 + ticket) * 1000)
        rows = [row(1, 0, .01, 0, -.2, 4100), row(2, 1, .004, 1, -.08, 4101),
                row(3, 1, .006, 3, -.12, 4103)]
        with self.assertRaises(SettlementError):
            settle_position_deals(rows[:2], 1, "XAUUSD", "BUY")
        with self.assertRaises(recent.RecentTradesError):
            self.record(value=rows)
        complete = settle_position_deals(rows, 1, "XAUUSD", "BUY")
        actual = self.context(self.record(value=complete))["records"][0]
        self.assertAlmostEqual(actual["net_profit"], 3.51)
        self.assertAlmostEqual(actual["price_close"], 4102.2)
        self.assertEqual(actual["deal_ids"], [1, 2, 3])

    def test_state_and_public_results_are_independent_copies(self):
        original = deepcopy(self.state)
        context = {"route": "m15_tactical"}
        state = self.record(entry_context=context)
        self.assertEqual(self.state, original)
        context["route"] = "modified"
        public = self.context(state)
        public["records"][0]["entry_context"]["route"] = "modified"
        self.assertEqual(self.context(state)["records"][0]["entry_context"]["route"], "m15_tactical")

    def test_namespace_is_stable_separate_and_absent_from_public_output(self):
        self.assertEqual(self.key, recent.namespace("123456", "broker-demo", "XAUUSD", 123))
        variants = [(999, "broker-demo", "XAUUSD", 123),
                    (123456, "other", "XAUUSD", 123),
                    (123456, "broker-demo", "EURUSD", 123),
                    (123456, "broker-demo", "XAUUSD", 124)]
        state = self.record()
        for args in variants:
            key = recent.namespace(*args)
            self.assertNotEqual(key, self.key)
            self.assertEqual(recent.recent_context(state, key, NOW)["records"], [])
        public = json.dumps(self.context(state))
        self.assertNotIn(self.key, public)
        self.assertNotIn("broker-demo", json.dumps(state))
        self.assertNotIn("account", public)

    def test_same_position_and_deal_ids_can_exist_in_separate_namespaces(self):
        state = self.record()
        second = recent.namespace(999, "broker-demo", "XAUUSD", 123)
        state = recent.record_settlement(state, second, settlement(profit=-3),
                                         direction="sell", observed_utc=NOW, close_utc=NOW)
        self.assertGreater(self.context(state)["records"][0]["net_profit"], 0)
        self.assertLess(recent.recent_context(state, second, NOW)["records"][0]["net_profit"], 0)

    def test_namespace_rejects_missing_or_invalid_identity(self):
        for args in [(None, "s", "XAUUSD", 1), (False, "s", "XAUUSD", 1),
                     (0, "s", "XAUUSD", 1), ("", "s", "XAUUSD", 1),
                     (1, "", "XAUUSD", 1), (1, "s", "", 1), (1, "s", "XAUUSD", True)]:
            with self.subTest(args=args), self.assertRaises(recent.RecentTradesError):
                recent.namespace(*args)

    def test_partition_limit_is_explicit_and_does_not_evict_other_accounts(self):
        state = self.state
        for account in range(1, recent.MAX_PARTITIONS + 1):
            key = recent.namespace(account, "broker-demo", "XAUUSD", 123)
            state = recent.record_settlement(state, key, settlement(), direction="BUY",
                                             observed_utc=NOW, close_utc=NOW)
        original = deepcopy(state)
        extra = recent.namespace(9999, "broker-demo", "XAUUSD", 123)
        with self.assertRaises(recent.RecentTradesError):
            recent.record_settlement(state, extra, settlement(), direction="BUY",
                                     observed_utc=NOW, close_utc=NOW)
        self.assertEqual(state, original)

    def test_duplicates_are_idempotent_even_if_deal_order_and_observation_change(self):
        state = self.record()
        repeat = self.record(state, settlement(deal_ids=(3, 2)), observed_utc=NOW + 99)
        self.assertEqual(state, repeat)

    def test_conflicting_complete_facts_do_not_change_input(self):
        state = self.record()
        original = deepcopy(state)
        variants = [settlement(profit=10), settlement(deal_ids=(2, 5)),
                    settlement(price_close=4110), settlement(reason=4), settlement(time=999)]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(recent.RecentTradesError):
                self.record(state, value)
        with self.assertRaises(recent.RecentTradesError):
            self.record(state, direction="SELL")
        with self.assertRaises(recent.RecentTradesError):
            self.record(state, close_utc=NOW - 2)
        self.assertEqual(state, original)

    def test_deal_identity_cannot_be_reused_by_another_position(self):
        state = self.record()
        with self.assertRaises(recent.RecentTradesError):
            self.record(state, settlement(2, deal_ids=(3, 99)))

    def test_future_unknown_and_expired_times_never_become_recent_closes(self):
        state = self.record(close_utc=None)
        self.assertEqual(self.context(state)["records"], [])
        self.assertEqual(self.context(state)["coverage"]["unknown_close_utc_records"], 1)
        state = self.record(state, settlement(2), observed_utc=NOW + 10, close_utc=NOW + 5)
        self.assertEqual(self.context(state)["records"], [])
        self.assertEqual(len(self.context(state, NOW + 10)["records"]), 1)
        state = self.record(state, settlement(3), close_utc=NOW - recent.WINDOW_SECONDS - 1)
        self.assertEqual(self.context(state)["records"], [])

    def test_window_boundary_is_inclusive_and_observation_is_not_close_time(self):
        state = self.record(close_utc=NOW - recent.WINDOW_SECONDS)
        self.assertEqual(len(self.context(state)["records"]), 1)
        self.assertEqual(self.context(state, NOW + .001)["records"], [])
        self.assertEqual(self.context(state)["coverage"]["started_utc"], NOW - 100)

    def test_future_close_is_rejected_instead_of_substituting_observed_time(self):
        with self.assertRaises(recent.RecentTradesError):
            self.record(close_utc=NOW + .001)

    def test_late_history_uses_real_close_utc_not_arrival_order(self):
        state = self.record(value=settlement(1), close_utc=NOW - 1)
        state = self.record(state, settlement(2), observed_utc=NOW + 5, close_utc=NOW - 50)
        state = self.record(state, settlement(3), observed_utc=NOW + 6,
                            close_utc=NOW - recent.WINDOW_SECONDS - 1)
        self.assertEqual([row["position_id"] for row in self.context(state, NOW + 6)["records"]], [1, 2])

    def test_later_utc_confirmation_is_allowed_without_duplicate_or_hindsight(self):
        state = self.record(close_utc=None)
        state = self.record(state, close_utc=NOW - 1, observed_utc=NOW + 10)
        self.assertEqual(self.context(state)["records"], [])
        self.assertEqual(len(self.context(state, NOW + 10)["records"]), 1)
        self.assertEqual(len(state["partitions"][self.key]["records"]), 1)

    def test_context_enrichment_is_allowed_but_original_context_cannot_change(self):
        state = self.record()
        state = self.record(state, entry_context={"route": "h1_trend"}, observed_utc=NOW + 1)
        with self.assertRaises(recent.RecentTradesError):
            self.record(state, entry_context={"route": "m15_tactical"}, observed_utc=NOW + 2)

    def test_last_six_include_both_sides_wins_losses_and_flat(self):
        state = self.state
        for index, profit in enumerate([-2, 3, -.5, 0, 4, -3, .35, 5], 1):
            state = self.record(state, settlement(index, profit=profit),
                                direction="BUY" if index % 2 else "SELL", close_utc=NOW - 10 + index)
        rows = self.context(state)["records"]
        self.assertEqual([row["position_id"] for row in rows], [8, 7, 6, 5, 4, 3])
        self.assertEqual({row["direction"] for row in rows}, {"BUY", "SELL"})
        self.assertTrue(any(row["net_profit"] < 0 for row in rows))
        self.assertTrue(any(row["net_profit"] > 0 for row in rows))
        self.assertTrue(any(abs(row["net_profit"]) < 1e-8 for row in rows))

    def test_query_is_direction_mirror_invariant(self):
        buy, sell = self.state, self.state
        for index in range(1, 9):
            value = settlement(index, profit=(-1.) ** index)
            reflected = replace(value, price_open=10000 - value.price_open,
                                price_close=10000 - value.price_close)
            buy = self.record(buy, value, direction="BUY", close_utc=NOW - index)
            sell = self.record(sell, reflected, direction="SELL", close_utc=NOW - index)
        left, right = self.context(buy), self.context(sell)
        self.assertEqual(left["coverage"], right["coverage"])
        for a, b in zip(left["records"], right["records"]):
            self.assertEqual(a["net_profit"], b["net_profit"])
            self.assertEqual(a["close_utc"], b["close_utc"])
            self.assertEqual(a["price_close"], 10000 - b["price_close"])
            self.assertEqual((a["direction"], b["direction"]), ("BUY", "SELL"))

    def test_cache_is_bounded_and_preserves_truncation_after_json_restart(self):
        state = self.state
        for index in range(1, 71):
            state = self.record(state, settlement(index), close_utc=NOW - 71 + index)
        part = state["partitions"][self.key]
        self.assertEqual(len(part["records"]), 64)
        self.assertEqual([row["position_id"] for row in part["records"]], list(range(7, 71)))
        restored = json.loads(json.dumps(state, allow_nan=False))
        coverage = self.context(restored)["coverage"]
        self.assertEqual(coverage["started_utc"], NOW - 100)
        self.assertEqual(coverage["truncated_before_utc"], NOW - 65)
        self.assertEqual(coverage["status"], "partial")
        late = self.record(restored, settlement(71), close_utc=NOW - 500)
        self.assertEqual(late, restored)

    def test_nonfinite_malformed_amounts_and_incomplete_objects_are_rejected(self):
        fields = ["profit", "commission", "swap", "fee", "volume", "price_open", "price_close"]
        for field in fields:
            for bad in [float("nan"), float("inf"), True, "1"]:
                with self.subTest(field=field, bad=bad), self.assertRaises(recent.RecentTradesError):
                    self.record(value=settlement(**{field: bad}))
        for value in [settlement(volume=0), settlement(price_open=-1), settlement(deal_ids=(2,)),
                      settlement(deal_ids=(2, 2)), settlement(position_id=True),
                      settlement(reason=-1), settlement(time=-1), {}, None]:
            with self.subTest(value=value), self.assertRaises(recent.RecentTradesError):
                recent.record_settlement(self.state, self.key, value, direction="BUY",
                                         observed_utc=NOW, close_utc=NOW)

    def test_unsupported_directions_and_invalid_times_fail_without_mutation(self):
        for direction in [None, "", "hold", "BUY ", 0, True]:
            with self.subTest(direction=direction), self.assertRaises(recent.RecentTradesError):
                self.record(direction=direction)
        for now in [float("nan"), 0, True, "1800000000", 10**1000]:
            with self.subTest(now=now), self.assertRaises(recent.RecentTradesError):
                recent.new_state(now)
            with self.assertRaises(recent.RecentTradesError):
                self.record(observed_utc=now)
        with self.assertRaises(recent.RecentTradesError):
            self.record(observed_utc=NOW - 101, close_utc=NOW - 102)

    def test_private_unknown_or_unbounded_entry_metadata_is_rejected(self):
        for value in [{"account": "123456"}, {"route": float("nan")}, {"route": {}},
                      {"route": "x" * 513}, {"route": ""}, {"_proof": "host private"}]:
            with self.subTest(value=value), self.assertRaises(recent.RecentTradesError):
                self.record(entry_context=value)

    def test_corrupt_or_missing_cache_is_explicitly_unavailable(self):
        good = self.record()
        bad_net = deepcopy(good)
        bad_net["partitions"][self.key]["records"][0]["net_profit"] = 999
        bad_nan = deepcopy(good)
        bad_nan["partitions"][self.key]["records"][0]["profit"] = float("nan")
        bad_duplicate = deepcopy(good)
        bad_duplicate["partitions"][self.key]["records"] *= 2
        for state in [None, {}, {"version": "old"}, bad_net, bad_nan, bad_duplicate]:
            with self.subTest(state=state):
                context = self.context(state)
                self.assertFalse(context["available"])
                self.assertEqual(context["coverage"]["status"], "cache_corrupt")
                self.assertEqual(context["records"], [])
        self.assertFalse(recent.unavailable_context("cache_write_failed")["available"])
        self.assertEqual(recent.unavailable_context("secret account 123")["coverage"]["status"], "cache_unavailable")
        self.assertEqual(recent.unavailable_context({})["coverage"]["status"], "cache_unavailable")

    def test_new_empty_cache_reports_limited_observation_coverage(self):
        context = self.context(self.state)
        self.assertTrue(context["available"])
        self.assertEqual(context["records"], [])
        self.assertEqual(context["coverage"]["scope"], "observed_only")
        self.assertEqual(context["coverage"]["started_utc"], NOW - 100)
        future = recent.new_state(NOW + 1)
        self.assertEqual(self.context(future)["coverage"]["status"], "cache_from_future")


if __name__ == "__main__":
    unittest.main()
