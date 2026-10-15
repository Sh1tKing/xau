"""Pure direction mechanism tests; no broker, AI, env or trading state."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

from strategies.direction import (DEFAULT_POLICY, ENGINE_DIGEST, LEGACY_VERSION,
                                  evaluate_context, normalize_policy, public_context)


NOW = 1800000000


def sources(event=True):
    m15 = [{"time": NOW-(32-i)*900, "open": 100., "high": 101., "low": 99., "close": 100.}
           for i in range(32)]
    m5 = [{"time": NOW-(80-i)*300, "open": 104., "high": 104.5, "low": 103.5, "close": 104.}
          for i in range(80)]
    if event:
        m15[-3].update(open=100., high=105., low=99., close=104.)
        for b in m15[-2:]:
            b.update(open=104., high=105., low=100., close=104.)
    return m15, m5


def mirror(rows):
    return [dict(time=b["time"], open=300-b["open"], high=300-b["low"],
                 low=300-b["high"], close=300-b["close"]) for b in rows]


def evaluate(m15=None, m5=None, **kwargs):
    a, b = sources()
    args = dict(h1_trend="bear", policy=DEFAULT_POLICY, now=NOW,
                server_utc_offset=0, tick_size=.01, bid=104.)
    args.update(kwargs)
    return evaluate_context(a if m15 is None else m15, b if m5 is None else m5, **args)


class DirectionPermissionTests(unittest.TestCase):
    def test_registered_policy_and_legacy_default(self):
        self.assertEqual(normalize_policy(), {"version": LEGACY_VERSION})
        self.assertEqual(normalize_policy(DEFAULT_POLICY), DEFAULT_POLICY)
        for policy in ({}, {"version": "unknown"}, {"version": DEFAULT_POLICY["version"]},
                       dict(DEFAULT_POLICY, failure_closes_m5=True),
                       dict(DEFAULT_POLICY, break_margin_atr=float("nan")),
                       dict(DEFAULT_POLICY, leg_ttl_m15=7), dict(DEFAULT_POLICY, extra=1), []):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                normalize_policy(policy)

    def test_opposite_h1_buy_and_sell_are_mirrors(self):
        a, b = sources()
        buy = evaluate(a, b)
        sell = evaluate(mirror(a), mirror(b), h1_trend="bull", bid=196.)
        self.assertEqual(buy["m15_leg"]["state"], "up")
        self.assertEqual(sell["m15_leg"]["state"], "down")
        for left, right in (("buy", "sell"), ("sell", "buy")):
            self.assertEqual(buy["permissions"][left]["allowed"], sell["permissions"][right]["allowed"])
            self.assertEqual(buy["permissions"][left]["route"], sell["permissions"][right]["route"])
        self.assertTrue(buy["permissions"]["buy"]["allowed"])
        self.assertFalse(buy["permissions"]["sell"]["allowed"])
        self.assertEqual(buy["permissions"]["buy"]["anchor"]["price"] + sell["permissions"]["sell"]["anchor"]["price"], 300.)

    def test_same_h1_direction_uses_one_ordinary_route(self):
        result = evaluate(h1_trend="bull")
        self.assertEqual(result["permissions"]["buy"]["route"], "h1_trend")
        self.assertIsNone(result["permissions"]["buy"]["anchor"])
        self.assertFalse(result["permissions"]["sell"]["allowed"])

    def test_no_event_is_legal_none_not_unknown(self):
        a, b = sources(False)
        result = evaluate(a, b)
        self.assertEqual(result["m15_leg"]["state"], "none")
        self.assertTrue(result["permissions"]["sell"]["allowed"])

    def test_legacy_does_not_open_counter_h1(self):
        result = evaluate(policy=None)
        self.assertEqual(result["version"], LEGACY_VERSION)
        self.assertFalse(result["permissions"]["buy"]["allowed"])
        self.assertTrue(result["permissions"]["sell"]["allowed"])

    def test_confirmation_not_in_old_two_boundaries(self):
        result = evaluate()
        self.assertEqual(result["m15_leg"]["boundaries"], {"upper": 101., "lower": 99.})
        self.assertGreater(result["m15_leg"]["confirmation"]["close"], 101.)

    def test_wick_only_and_exact_margin_not_events(self):
        for close in (100., 101.02, 101.5):
            a, b = sources(False)
            a[-1].update(high=108., close=close)
            # Force tick-based epsilon=.5 so exact equality is unambiguous.
            result = evaluate(a, b, tick_size=.25)
            self.assertEqual(result["m15_leg"]["state"], "none")

    def test_strict_break_exceeds_tick_margin(self):
        a, b = sources(False)
        a[-1].update(high=102., close=101.500001)
        self.assertEqual(evaluate(a, b, tick_size=.25)["m15_leg"]["state"], "up")

    def test_atr14_matches_historical_rolling_tr_not_latest_atr(self):
        a, b = sources()
        # Current bar expands substantially without a new close breakout.
        a[-1].update(high=130., low=98.)
        result = evaluate(a, b)
        conf = result["m15_leg"]["confirmation"]
        expected = (13*2 + 6)/14
        self.assertAlmostEqual(conf["atr14"], expected)
        self.assertAlmostEqual(result["m15_leg"]["epsilon"], expected*.1)

    def test_future_append_does_not_change_past_result(self):
        a, b = sources()
        original = evaluate(a, b)
        a += [dict(time=NOW, open=100., high=1000., low=1., close=900.)]
        b += [dict(time=NOW, open=100., high=1000., low=1., close=900.)]
        self.assertEqual(evaluate(a, b), original)

    def test_forming_right_neighbor_cannot_confirm_pivot(self):
        a, b = sources()
        b[-1]["low"] = 102.
        b.append(dict(time=NOW, open=104., high=105., low=103., close=104.))
        self.assertEqual(evaluate(a, b)["permissions"]["buy"]["anchor"]["kind"], "origin")

    def test_unknown_required_data_is_failclosed(self):
        for kwargs in ({"tick_size": 0}, {"tick_size": True}, {"bid": float("inf")},
                       {"h1_trend": "neutral"}, {"h1_trend": None}, {"now": True},
                       {"now": datetime(2026, 1, 1)}, {"server_utc_offset": 15},
                       {"server_utc_offset": .001}, {"server_utc_offset": True}):
            result = evaluate(**kwargs)
            self.assertEqual(result["m15_leg"]["state"], "unknown", kwargs)
            self.assertFalse(any(p["allowed"] for p in result["permissions"].values()))

    def test_invalid_ohlc_numbers_and_order_failclosed(self):
        for mutation in (lambda a: a[-1].update(high=1), lambda a: a[-1].update(close=True),
                         lambda a: a[-1].update(low=float("nan")), lambda a: a[-1].update(open=-1),
                         lambda a: a[-1].update(time=a[-2]["time"]),
                         lambda a: a[-1].update(time=a[-1]["time"]+1)):
            a, b = sources()
            mutation(a)
            self.assertEqual(evaluate(a, b)["m15_leg"]["state"], "unknown")

    def test_stale_or_offset_mismatch_failclosed(self):
        a, b = sources()
        for x, y, kwargs in ((a[:-1], b, {}), (a, b[:-1], {}), (a, b, {"server_utc_offset": 3})):
            self.assertEqual(evaluate(x, y, **kwargs)["m15_leg"]["state"], "unknown")

    def test_gaps_in_warmup_or_failure_history_failclosed(self):
        a, b = sources()
        del a[-15]
        self.assertEqual(evaluate(a, b)["m15_leg"]["state"], "unknown")
        a, b = sources()
        del b[-12]
        self.assertEqual(evaluate(a, b)["m15_leg"]["state"], "unknown")

    def test_atr_warmup_and_m5_window_insufficiency_failclosed(self):
        a, b = sources()
        self.assertEqual(evaluate(a[-19:], b)["m15_leg"]["state"], "unknown")
        self.assertNotEqual(evaluate(a[-20:], b)["m15_leg"]["state"], "unknown")
        self.assertEqual(evaluate(a, b[-10:])["m15_leg"]["state"], "unknown")

    def test_two_closes_restore_h1_and_persist_beyond_last_two(self):
        a, b = sources()
        for row in b[-6:-4]:
            row.update(close=100., low=99.8)
        result = evaluate(a, b, bid=100.)
        p = result["permissions"]
        self.assertTrue(p["sell"]["allowed"])
        self.assertEqual(p["sell"]["route"], "h1_recovery")
        self.assertEqual(p["sell"]["anchor"]["bar_times"], [b[-6]["time"], b[-5]["time"]])
        self.assertEqual(p["sell"]["anchor"]["price"], 104.5)
        self.assertFalse(p["buy"]["allowed"])
        self.assertEqual([row["close"] for row in b[-2:]], [104., 104.])

    def test_failure_recovery_is_exact_mirror(self):
        a, b = sources()
        for row in b[-6:-4]:
            row.update(close=100., low=99.8)
        sell = evaluate(a, b, bid=100.)["permissions"]["sell"]
        buy = evaluate(mirror(a), mirror(b), h1_trend="bull", bid=200.)["permissions"]["buy"]
        self.assertTrue(buy["allowed"])
        self.assertEqual(buy["route"], sell["route"])
        self.assertEqual(buy["anchor"]["price"] + sell["anchor"]["price"], 300.)

    def test_failed_event_cannot_reactivate_after_quote_recovery(self):
        a, b = sources()
        for row in b[-6:-4]:
            row.update(close=100., low=99.8)
        for bid in (104., 101.-evaluate()["m15_leg"]["epsilon"]):
            result = evaluate(a, b, bid=bid)
            self.assertFalse(any(p["allowed"] for p in result["permissions"].values()))

    def test_one_close_nonconsecutive_and_preconfirmation_not_failure(self):
        for positions in ((-6,), (-6, -4), (-8, -7), (-7, -6)):
            a, b = sources()
            for i in positions:
                b[i].update(close=100., low=99.8)
            result = evaluate(a, b)
            self.assertIsNone(result["m15_leg"].get("failure"))
            self.assertTrue(result["permissions"]["buy"]["allowed"])

    def test_quote_crossing_suspends_tactical_without_m5_recovery(self):
        boundary = 101.-evaluate()["m15_leg"]["epsilon"]
        self.assertTrue(evaluate(bid=boundary)["permissions"]["buy"]["allowed"])
        result = evaluate(bid=boundary-.01)
        self.assertFalse(any(p["allowed"] for p in result["permissions"].values()))

    def test_pivot_before_breakout_is_canonical_with_confirmed_right(self):
        a, b = sources()
        b[-10]["low"] = 102.
        result = evaluate(a, b)
        anchor = result["permissions"]["buy"]["anchor"]
        self.assertEqual(anchor["kind"], "local_pullback")
        self.assertEqual(anchor["price"], 102.)
        self.assertLess(anchor["confirmed_at"], result["m15_leg"]["confirmation"]["closed_at_utc"])
        mirrored = evaluate(mirror(a), mirror(b), h1_trend="bull", bid=196.)["permissions"]["sell"]["anchor"]
        self.assertEqual(mirrored["kind"], anchor["kind"])
        self.assertEqual(mirrored["price"] + anchor["price"], 300.)

    def test_latest_valid_pivot_not_stop_fitting_search(self):
        a, b = sources()
        b[-10]["low"] = 102.
        b[-3]["low"] = 103.
        anchor = evaluate(a, b, bid=120.)["permissions"]["buy"]["anchor"]
        self.assertEqual(anchor["price"], 103.)
        self.assertGreater(120.-anchor["price"], 8.)
        self.assertEqual(anchor["bar_times"][1], b[-3]["time"])

    def test_equal_pivot_and_pivot_too_close_to_origin_not_accepted(self):
        a, b = sources()
        b[-3]["low"] = b[-2]["low"] = 102.
        self.assertEqual(evaluate(a, b)["permissions"]["buy"]["anchor"]["kind"], "origin")
        a, b = sources()
        b[-3]["low"] = 99.01
        self.assertEqual(evaluate(a, b)["permissions"]["buy"]["anchor"]["kind"], "origin")

    def test_pivot_outside_formation_or_unclosed_not_accepted(self):
        a, b = sources()
        b[-20]["low"] = 102.
        self.assertEqual(evaluate(a, b)["permissions"]["buy"]["anchor"]["kind"], "origin")

    def test_later_wick_or_quote_break_cannot_restore_old_pivot(self):
        a, b = sources()
        b[-10]["low"] = 102.
        b[-1]["low"] = 101.9
        self.assertEqual(evaluate(a, b)["permissions"]["buy"]["anchor"]["kind"], "origin")
        a, b = sources()
        b[-10]["low"] = 102.
        self.assertEqual(evaluate(a, b, bid=101.9)["permissions"]["buy"]["anchor"]["kind"], "origin")

    def test_latest_failed_m15_event_does_not_resurrect_older(self):
        a, b = sources()
        a[-1].update(close=100., low=99.8)
        result = evaluate(a, b, bid=100.)
        self.assertEqual(result["m15_leg"]["state"], "none")
        self.assertEqual(result["m15_leg"]["reason"], "M15 closing structure failure")
        self.assertTrue(result["permissions"]["sell"]["allowed"])

    def test_only_last_six_events_and_ttl_boundary(self):
        a, b = sources(False)
        a[-6].update(high=105., close=104.)
        for row in a[-5:]:
            row.update(open=104., high=105., low=100., close=104.)
        for n in range(2):
            b.append(dict(time=NOW+n*300, open=104., high=104.5, low=103.5, close=104.))
        before = evaluate(a, b, now=NOW+899)
        self.assertEqual(before["m15_leg"]["state"], "up")
        self.assertEqual(before["m15_leg"]["expires_at_utc"], NOW+900)
        # Advance complete sources to exact 90-minute expiry; old event exits
        # the six-bar selection window, so it cannot renew itself.
        a.append(dict(time=NOW, open=104., high=105., low=100., close=104.))
        b.append(dict(time=NOW+600, open=104., high=104.5, low=103.5, close=104.))
        after = evaluate(a, b, now=NOW+900)
        self.assertEqual(after["m15_leg"]["state"], "none")
        self.assertTrue(after["permissions"]["sell"]["allowed"])

    def test_hash_stability_excludes_incidental_fields_clock_and_bid(self):
        a, b = sources()
        old = evaluate(a, b)
        for row in a+b:
            row.update(candle_parts={"private": "incidental"}, tick_volume=123)
        new = evaluate(a, b, now=NOW+10, bid=104.1)
        self.assertEqual(old, new)
        self.assertEqual(old["engine_digest"], ENGINE_DIGEST)
        b[-1]["close"] = 104.1
        self.assertNotEqual(old["source_digest"], evaluate(a, b)["source_digest"])

    def test_source_window_caps_are_identical_for_long_and_capped_inputs(self):
        a, b = sources()
        olda = [dict(row, time=row["time"]-32*900) for row in a]
        oldb = [dict(row, time=row["time"]-80*300) for row in b]
        self.assertEqual(evaluate(a, b), evaluate(olda+a, oldb+b))

    def test_public_context_excludes_host_and_permission_private_fields(self):
        result = evaluate()
        result["host_private"] = {"arbitrary": "secret"}
        result["m15_leg"]["host_private"] = "secret"
        result["m15_leg"]["confirmation"]["host_private"] = "secret"
        public = public_context(result)
        self.assertNotIn("host_private", public)
        self.assertNotIn("permissions", public)
        self.assertNotIn("host_private", public["m15_leg"])
        self.assertNotIn("host_private", public["m15_leg"]["confirmation"])
        self.assertEqual(json.loads(json.dumps(public, allow_nan=False)), public)
        public["m15_leg"]["state"] = "corrupted"
        self.assertEqual(result["m15_leg"]["state"], "up")

    def test_server_offset_and_aware_clock(self):
        a, b = sources()
        shifted = lambda rows: [dict(row, time=row["time"]+10800) for row in rows]
        result = evaluate(shifted(a), shifted(b), server_utc_offset=3,
                          now=datetime.fromtimestamp(NOW, timezone.utc))
        self.assertEqual(result["m15_leg"]["state"], "up")
        self.assertEqual(result["m15_leg"]["confirmation"]["closed_at_utc"], NOW-1800)

    def test_exact_failure_close_boundary_does_not_confirm(self):
        a, b = sources()
        level = evaluate()["m15_leg"]["failure_level"]
        for row in b[-6:-4]:
            row.update(close=level, low=level-.1)
        self.assertIsNone(evaluate(a, b)["m15_leg"]["failure"])

    def test_new_real_m15_event_resets_failed_old_event(self):
        a, b = sources()
        for row in b[-6:-4]:
            row.update(close=100., low=99.8)
        old = evaluate(a, b)
        a[-1].update(high=107., close=106.)
        new = evaluate(a, b, bid=106.)
        self.assertIsNone(new["m15_leg"]["failure"])
        self.assertNotEqual(old["m15_leg"]["event_id"], new["m15_leg"]["event_id"])
        self.assertTrue(new["permissions"]["buy"]["allowed"])

    def test_failed_newest_event_never_restores_valid_older_event(self):
        a, b = sources(False)
        a[-6].update(high=106., close=105.)
        for row in a[-5:-3]:
            row.update(open=105., high=106., low=104., close=105.)
        a[-3].update(open=105., high=106., low=102., close=103.)
        for row in a[-2:]:
            row.update(open=104., high=106., low=103., close=104.8)
        result = evaluate(a, b)
        self.assertEqual(result["m15_leg"]["direction"], "down")
        self.assertEqual(result["m15_leg"]["state"], "none")
        self.assertEqual(result["m15_leg"]["confirmation"]["bar_time"], a[-3]["time"])

    def test_zero_atr_fails_closed_instead_of_fallback(self):
        a, b = sources(False)
        for row in a:
            row.update(open=100., high=100., low=100., close=100.)
        self.assertEqual(evaluate(a, b)["m15_leg"]["state"], "unknown")

    def test_decimal_grid_exact_break_margin_cannot_create_event(self):
        a, b = sources(False)
        shift = 3058.22
        for row in a+b:
            for key in ("open", "high", "low", "close"):
                row[key] = round(shift+.02*row[key], 2)
        a[-1].update(high=round(shift+2.05, 2), close=round(shift+2.04, 2))
        self.assertEqual(a[-1]["close"], 3060.26)
        for first, second, h1, bid in ((a, b, "bear", 3060.26),
                                      (self.decimal_mirror(a), self.decimal_mirror(b), "bull", 6939.74)):
            result = evaluate(first, second, h1_trend=h1, bid=bid)
            self.assertEqual(result["m15_leg"]["state"], "none")

    @staticmethod
    def decimal_mirror(rows):
        return [dict(time=row["time"], open=round(10000-row["open"], 2),
                     high=round(10000-row["low"], 2), low=round(10000-row["high"], 2),
                     close=round(10000-row["close"], 2)) for row in rows]

    def test_decimal_grid_tactical_quote_equality_is_mirrored(self):
        a, b = sources()
        for row in a+b:
            for key in ("open", "high", "low", "close"):
                row[key] = round(6768.99+.02*row[key], 2)
        buy = evaluate(a, b, bid=6770.99)
        sell = evaluate(self.decimal_mirror(a), self.decimal_mirror(b), h1_trend="bull", bid=3229.01)
        self.assertEqual(buy["m15_leg"]["failure_level"], 6770.99)
        self.assertEqual(sell["m15_leg"]["failure_level"], 3229.01)
        self.assertTrue(buy["permissions"]["buy"]["allowed"])
        self.assertTrue(sell["permissions"]["sell"]["allowed"])

    def test_decimal_grid_failure_close_equality_never_restores_h1(self):
        a, b = sources()
        for row in a+b:
            for key in ("open", "high", "low", "close"):
                row[key] = round(3260.99+.02*row[key], 2)
        for row in b[-6:-4]:
            row.update(close=3262.99, low=3262.98)
        for first, second, h1, bid, side in (
                (a, b, "bear", 3262.99, "buy"),
                (self.decimal_mirror(a), self.decimal_mirror(b), "bull", 6737.01, "sell")):
            result = evaluate(first, second, h1_trend=h1, bid=bid)
            self.assertIsNone(result["m15_leg"]["failure"])
            self.assertTrue(result["permissions"][side]["allowed"])

    def test_decimal_grid_m15_equality_does_not_invalidate_event(self):
        a, b = sources()
        for row in a+b:
            for key in ("open", "high", "low", "close"):
                row[key] = round(6768.99+.02*row[key], 2)
        a[-1].update(close=6770.99, low=6770.98)
        first = evaluate(a, b, bid=6770.99)
        second = evaluate(self.decimal_mirror(a), self.decimal_mirror(b), h1_trend="bull", bid=3229.01)
        self.assertEqual(first["m15_leg"]["state"], "up")
        self.assertEqual(second["m15_leg"]["state"], "down")

    def test_derived_infinite_margin_is_json_safe_unknown(self):
        result = evaluate(tick_size=1e308)
        self.assertEqual(result["m15_leg"]["state"], "unknown")
        json.dumps(result, allow_nan=False)
        with self.assertRaises(ValueError):
            normalize_policy(dict(DEFAULT_POLICY, break_margin_ticks=10**1000))


if __name__ == "__main__":
    unittest.main()
