"""Frozen session-retest facts; synthetic closed bars, no terminal or network."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.session_retest import session_retest_facts


def fixture(day="2026-07-21", zone="Europe/London", side="buy", offset=3):
    start = datetime.fromisoformat(day + "T08:00:00").replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    rows = []
    for i in range(-14, 8):
        stamp = int((start + timedelta(minutes=5*i)).timestamp() + offset*3600)
        row = {"time": stamp, "open": 100., "high": 101., "low": 99., "close": 100.}
        if i == 6:
            row.update(open=100.5, high=102., low=100.4, close=101.8)
        if i == 7:
            row.update(open=101.7, high=102., low=101.1, close=101.6)
        rows.append(row)
    frame = pd.DataFrame(rows)
    if side == "sell":
        frame = mirror(frame)
    decision = start + timedelta(minutes=40)
    return frame, decision


def mirror(frame):
    other = frame.copy()
    other["open"], other["close"] = 200-frame.open, 200-frame.close
    other["high"], other["low"] = 200-frame.low, 200-frame.high
    return other


def add_bar(frame, **values):
    row = {"time": int(frame.time.iloc[-1])+300, "open": 101.6, "high": 102., "low": 101.1, "close": 101.6}
    row.update(values)
    return pd.concat([frame, pd.DataFrame([row])], ignore_index=True)


def facts(frame, decision, side="buy", offset=3, params=None):
    return session_retest_facts(frame, decision, offset, side, params or {})


class SessionRetestFactsTests(unittest.TestCase):
    def test_buy_sell_mirrors_raw_times_and_three_bar_protection(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                frame, decision = fixture(side=side)
                result = facts(frame, decision, side)
                self.assertEqual(result["blockers"], [])
                self.assertEqual(result["closed_trigger"], {"confirmed": True, "timeframe": "M5", "bar_time": int(frame.time.iloc[-1])})
                self.assertEqual(result["reference_close"], float(frame.close.iloc[-1]))
                extreme = float(frame.low.iloc[-3:].min() if side == "buy" else frame.high.iloc[-3:].max())
                self.assertEqual(result["protection_extreme"], extreme)
                self.assertEqual(len(result["opening_bars"]), 6)
                self.assertEqual(len(result["protection_bars"]), 3)
                self.assertEqual(result["breakout_bar"]["time"], int(frame.time.iloc[-2]))
                self.assertEqual(result["retest_bar"]["close_utc"], decision.timestamp())
                self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_dst_winter_summer_and_uk_us_transition_weeks(self):
        for day, london_hour, ny_hour in (("2026-01-20",8,13), ("2026-07-21",7,12),
                                         ("2026-03-20",8,12), ("2026-10-28",8,12)):
            for zone, hour in (("Europe/London", london_hour), ("America/New_York", ny_hour)):
                with self.subTest(day=day, zone=zone):
                    frame, decision = fixture(day, zone)
                    result = facts(frame, decision)
                    self.assertTrue(result["closed_trigger"]["confirmed"])
                    self.assertEqual(decision.hour, hour)
                    self.assertEqual(result["zone"], zone)
                    self.assertIn(zone + "/" + day, result["event_id"])

    def test_explicit_offset_zero_fractional_and_negative(self):
        for offset in (0, 3, 5.5, -4):
            with self.subTest(offset=offset):
                frame, decision = fixture(offset=offset)
                self.assertTrue(facts(frame, decision, offset=offset)["closed_trigger"]["confirmed"])

    def test_timezone_and_offset_errors_block_without_guessing(self):
        frame, decision = fixture()
        for offset in (None, float("nan"), float("inf"), 15, -15, True, "bad"):
            with self.subTest(offset=offset):
                self.assertFalse(facts(frame, decision, offset=offset)["closed_trigger"]["confirmed"])
        self.assertIn("decision_time_zone_missing", facts(frame, decision.replace(tzinfo=None))["blockers"])
        with patch("strategies.session_retest.ZoneInfo", side_effect=ZoneInfoNotFoundError("missing")):
            self.assertIn("time_zone_missing", facts(frame, decision)["blockers"])

    def test_missing_opening_bar_and_missing_exact_start_reject(self):
        frame, decision = fixture()
        for index in (14, 16, 19):
            with self.subTest(index=index):
                result = facts(frame.drop(index), decision)
                self.assertIn("incomplete_opening_window", result["blockers"])
                self.assertFalse(result["closed_trigger"]["confirmed"])

    def test_duplicate_unsorted_nonfinite_and_invalid_ohlc_reject(self):
        frame, decision = fixture()
        corrupt = [pd.concat([frame.iloc[:15], frame.iloc[14:]], ignore_index=True),
                   frame.iloc[::-1].reset_index(drop=True)]
        for column in ("time", "open", "high", "low", "close"):
            for value in (float("nan"), float("inf")):
                altered = frame.astype(float).copy()
                altered.loc[0, column] = value
                corrupt.append(altered)
        bad_ohlc = frame.copy()
        bad_ohlc.loc[0, "low"] = 104
        corrupt.append(bad_ohlc)
        bad_time = frame.copy()
        bad_time.loc[0, "time"] += 1
        corrupt.append(bad_time)
        for data in corrupt:
            self.assertTrue(facts(data, decision)["blockers"])
            self.assertFalse(facts(data, decision)["closed_trigger"]["confirmed"])

    def test_atr_equals_lab_trailing_true_range_mean(self):
        frame, decision = fixture()
        frame.loc[0:3, ["open", "high", "low", "close"]] += 2
        previous = frame.close.shift()
        tr = pd.concat([frame.high-frame.low, (frame.high-previous).abs(), (frame.low-previous).abs()], axis=1).max(axis=1)
        expected = tr.rolling(14).mean()
        result = facts(frame, decision)
        self.assertTrue(result["closed_trigger"]["confirmed"])
        self.assertAlmostEqual(result["opening_atr"], float(expected.iloc[19]))
        self.assertAlmostEqual(result["m5_atr"], float(expected.iloc[-1]))

    def test_signal_time_and_side_match_exact_lab_loop(self):
        from docs.experiments import gold_strategy_lab as lab
        for zone in ("Europe/London", "America/New_York"):
            for side in ("buy", "sell"):
                with self.subTest(zone=zone, side=side):
                    raw, decision = fixture(zone=zone, side=side)
                    m5 = raw.copy()
                    m5["time"] -= 3*3600
                    m5["spread_abs"] = .2
                    h1_time = int(decision.timestamp())//3600*3600 - 3600
                    h1 = pd.DataFrame([{"time":h1_time, "open":100., "high":102., "low":98., "close":100., "spread_abs":.2}])
                    m15 = h1.copy()
                    m15["time"] = h1_time + 2700
                    signals = lab.generate_signals({"M5":m5, "M15":m15, "H1":h1})["session_retest"]
                    self.assertEqual(len(signals), 1)
                    actual = facts(raw, decision, side)
                    self.assertTrue(actual["closed_trigger"]["confirmed"])
                    self.assertEqual(signals[0].time, actual["retest_bar"]["close_utc"])
                    self.assertEqual(signals[0].side, 1 if side == "buy" else -1)
                    self.assertAlmostEqual(signals[0].atr, actual["m5_atr"])
                    self.assertAlmostEqual(signals[0].reference, actual["reference_close"] + (.2 if side == "buy" else 0))

    def test_insufficient_atr_history_and_width_bounds_reject(self):
        frame, decision = fixture()
        self.assertIn("insufficient_m5_atr_history", facts(frame.iloc[12:], decision)["blockers"])
        for params in ({"min_width_atr":2}, {"max_width_atr":.9, "min_width_atr":.1}):
            self.assertIn("opening_width_outside_atr_bounds", facts(frame, decision, params=params)["blockers"])

    def test_no_retest_does_not_use_breakout_bar_as_retest(self):
        frame, decision = fixture()
        result = facts(frame.iloc[:-1], decision-timedelta(minutes=5))
        self.assertIn("no_first_retest", result["blockers"])
        self.assertIsNone(result["retest_bar"])

    def test_no_breakout_and_invalidation_before_later_retest(self):
        frame, decision = fixture()
        inside = frame.copy()
        inside.loc[20:, ["open", "high", "low", "close"]] = [100.,101.,99.,100.]
        self.assertIn("no_first_breakout", facts(inside, decision)["blockers"])
        for side in ("buy", "sell"):
            invalid = frame.copy()
            invalid.loc[21, ["open", "high", "low", "close"]] = [101.,101.5,99.,100.]
            invalid = add_bar(invalid)
            if side == "sell":
                invalid = mirror(invalid)
            result = facts(invalid, decision+timedelta(minutes=5), side)
            self.assertIn("first_breakout_invalidated", result["blockers"])
            self.assertIsNone(result["retest_bar"])

    def test_first_retest_old_is_missed_and_never_replaced(self):
        frame, decision = fixture()
        original = facts(frame, decision)
        expanded = facts(add_bar(frame), decision+timedelta(minutes=5))
        self.assertIn("first_retest_missed", expanded["blockers"])
        self.assertEqual(expanded["retest_bar"], original["retest_bar"])
        self.assertEqual(expanded["breakout_bar"], original["breakout_bar"])

    def test_confirmation_freshness_includes_close_excludes_next_close(self):
        frame, decision = fixture()
        self.assertTrue(facts(frame, decision+timedelta(seconds=299))["closed_trigger"]["confirmed"])
        self.assertIn("first_retest_expired", facts(frame, decision+timedelta(seconds=300))["blockers"])
        self.assertIn("unclosed_or_future_m5_evidence", facts(frame, decision-timedelta(seconds=1))["blockers"])
        self.assertIn("unclosed_or_future_m5_evidence", facts(add_bar(frame), decision)["blockers"])

    def test_first_opposite_breakout_cannot_be_filtered_for_later_buy(self):
        frame, decision = fixture(side="sell")
        frame = add_bar(frame, open=100., high=102., low=99.8, close=101.8)
        frame = add_bar(frame)
        result = facts(frame, decision+timedelta(minutes=10), "buy")
        self.assertIn("first_breakout_opposite_action", result["blockers"])
        self.assertEqual(result["breakout_bar"]["action"], "sell")

    def test_six_bar_timeout_and_no_chasing_seventh_bar(self):
        frame, decision = fixture()
        frame = frame.iloc[:-1].copy()
        for _ in range(6):
            frame = add_bar(frame, open=102., high=103., low=102., close=102.5)
        result = facts(frame, decision+timedelta(minutes=25))
        self.assertIn("first_retest_timeout", result["blockers"])
        seventh = add_bar(frame)
        self.assertIn("first_retest_timeout", facts(seventh, decision+timedelta(minutes=30))["blockers"])
        self.assertIsNone(facts(seventh, decision+timedelta(minutes=30))["retest_bar"])

    def test_candidate_gap_prevents_accepting_later_retest(self):
        frame, decision = fixture()
        gap = add_bar(frame.iloc[:-1], time=int(frame.time.iloc[-1])+300)
        result = facts(gap, decision+timedelta(minutes=5))
        self.assertIn("missing_or_stale_candidate_sequence", result["blockers"])

    def test_strict_breakout_and_hold_inclusive_touch(self):
        frame, decision = fixture()
        parameters = {"break_atr":0, "retest_atr":0, "hold_atr":0}
        no_break = frame.copy()
        no_break.loc[20:, "close"] = 101.
        no_break.loc[21, "low"] = 100.8
        self.assertIn("no_first_breakout", facts(no_break, decision, params=parameters)["blockers"])
        no_hold = frame.copy()
        no_hold.loc[21, ["low", "close"]] = [100.8, 101.]
        self.assertIn("no_first_retest", facts(no_hold, decision, params=parameters)["blockers"])
        exact_touch = frame.copy()
        exact_touch.loc[21, "low"] = 101.
        self.assertTrue(facts(exact_touch, decision, params=parameters)["closed_trigger"]["confirmed"])

    def test_invalidation_boundary_is_strict_for_both_sides(self):
        frame, decision = fixture()
        frame.loc[21, ["open", "high", "low", "close"]] = [101.,102.,100.8,101.]
        frame = add_bar(frame)
        for side in ("buy", "sell"):
            data = frame if side == "buy" else mirror(frame)
            result = facts(data, decision+timedelta(minutes=5), side, params={"invalidation_atr":0})
            self.assertTrue(result["closed_trigger"]["confirmed"])

    def test_retest_seconds_limit_independent_of_bar_count(self):
        frame, decision = fixture()
        frame = frame.iloc[:-1].copy()
        for _ in range(3):
            frame = add_bar(frame, open=102., high=103., low=102., close=102.5)
        frame = add_bar(frame)
        result = facts(frame, decision+timedelta(minutes=15), params={"max_retest_seconds":900})
        self.assertIn("first_retest_timeout", result["blockers"])

    def test_computation_overflow_never_returns_nonfinite_json(self):
        frame, decision = fixture()
        frame.loc[0, ["high", "low"]] = [1e308, -1e308]
        result = facts(frame, decision)
        self.assertIn("nonfinite_m5_true_range", result["blockers"])
        json.dumps(result, allow_nan=False)

    def test_current_local_window_end_is_excluded(self):
        frame, decision = fixture()
        for minute in (29, 180):
            clock = decision + timedelta(minutes=minute-40)
            self.assertIn("outside_session_window", facts(frame, clock)["blockers"])

    def test_prefix_invariance_first_event_survives_later_closed_bars(self):
        frame, decision = fixture()
        first = facts(frame, decision)
        for i in range(1, 7):
            frame = add_bar(frame, open=100., high=101., low=99., close=100.)
            later = facts(frame, decision+timedelta(minutes=5*i))
            self.assertEqual(later["event_id"], first["event_id"])
            self.assertEqual(later["retest_bar"], first["retest_bar"])
            self.assertEqual(later["reference_close"], first["reference_close"])
            self.assertFalse(later["closed_trigger"]["confirmed"])

    def test_invalid_parameters_fail_closed(self):
        frame, decision = fixture()
        for p in ({"open_minute":479}, {"range_minutes":0}, {"max_retest_bars":0},
                  {"hold_atr":float("nan")}, {"max_retest_seconds":1}, {"min_width_atr":-1}):
            self.assertTrue(facts(frame, decision, params=p)["blockers"][0].startswith("invalid_parameters"))

    def test_oversized_integer_parameters_fail_closed(self):
        frame, decision = fixture()
        for key in ("open_minute", "max_retest_bars", "min_width_atr", "break_atr"):
            for value in (10**1000, -(10**1000)):
                with self.subTest(key=key, negative=value < 0):
                    result = facts(frame, decision, params={key:value})
                    self.assertEqual(result["blockers"], ["invalid_parameters: invalid parameter " + key])
                    self.assertFalse(result["closed_trigger"]["confirmed"])
                    json.dumps(result, allow_nan=False)

    def test_late_window_contains_all_preceding_candidate_evidence(self):
        original, _ = fixture()
        frame = original.iloc[:-2].copy()
        for _ in range(25):
            frame = add_bar(frame, open=100., high=101., low=99., close=100.)
        frame.loc[27, "close"] = 100.25
        frame = add_bar(frame, open=100.5, high=102., low=100.4, close=101.8)
        frame = add_bar(frame)
        decision = datetime.fromtimestamp(int(frame.time.iloc[-1])-3*3600+300, timezone.utc)
        result = facts(frame, decision)
        self.assertTrue(result["closed_trigger"]["confirmed"])
        candidates = result["candidate_bars"]
        self.assertEqual(len(candidates), 27)
        self.assertEqual([bar["time"] for bar in candidates], result["candidate_bar_times"])
        self.assertEqual(result["candidate_bar_times"], frame.time.iloc[20:].tolist())
        self.assertEqual(candidates[7]["close"], 100.25)
        self.assertEqual(candidates[7]["time"], int(frame.time.iloc[27]))
        for bar in candidates:
            source = frame[frame.time == bar["time"]].iloc[0]
            for key in ("open", "high", "low", "close"):
                self.assertEqual(bar[key], float(source[key]))
            self.assertGreater(bar["atr"], 0)
            self.assertLessEqual(bar["close_utc"], decision.timestamp())
        self.assertLess(candidates[0]["time"], result["breakout_bar"]["time"]-8*300)
        json.dumps(result, allow_nan=False)

    def test_candidate_evidence_is_closed_and_future_input_fails_empty(self):
        frame, decision = fixture()
        before = facts(frame.iloc[:-1], decision-timedelta(minutes=5))
        self.assertEqual(len(before["candidate_bars"]), 1)
        self.assertTrue(all(bar["close_utc"] <= (decision-timedelta(minutes=5)).timestamp()
                            for bar in before["candidate_bars"]))
        result = facts(add_bar(frame), decision)
        self.assertIn("unclosed_or_future_m5_evidence", result["blockers"])
        self.assertEqual(result["candidate_bars"], [])
        self.assertEqual(result["candidate_bar_times"], [])
        self.assertFalse(result["closed_trigger"]["confirmed"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
