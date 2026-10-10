"""Adaptive profile/checkpoint contract tests; no production or broker imports."""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from strategies.adaptive import checkpoint_id, validate_analysis_checkpoint
from strategies.framework import Context, DEFAULT_REGISTRY, Registry, StrategyError


def context(profile="asia_adaptive", action="buy", offset=3, clock=None):
    clock = clock or datetime(2026, 10, 10, 11, 35, tzinfo=timezone.utc)
    frames = {}
    for tf, step, count in (("M5", 300, 18), ("M15", 900, 30), ("H1", 3600, 30)):
        end = int((clock.timestamp()+offset*3600)//step)*step
        frames[tf] = pd.DataFrame([dict(time=end-(count-i)*step, open=100., high=101.,
                                      low=99., close=100., tick_volume=100) for i in range(count)])
    session = {"asia_adaptive": "asia", "london_adaptive": "london", "us_adaptive": "overlap"}[profile]
    return Context(action, frames["M15"], frames["H1"], frames["M5"], None, 2.,
                   "bull" if action == "buy" else "bear", "neutral", 10., session,
                   None, clock, offset)


class AdaptiveProfileTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = DEFAULT_REGISTRY.snapshot()
        self.assertFalse(self.snapshot.error)

    def assess(self, c, profile="asia_adaptive"):
        return self.snapshot.get(profile).assess(c)

    def validate(self, evidence, c, **kwargs):
        return validate_analysis_checkpoint(evidence, c.now, c.server_utc_offset,
                                            c.df_m5, c.df_m15, **kwargs)

    def test_each_host_session_has_only_its_adaptive_profile(self):
        for session, expected in (("asia", "asia_adaptive"), ("london", "london_adaptive"),
                                  ("overlap", "us_adaptive"), ("late_us", "us_adaptive")):
            self.assertEqual([s.id for s in self.snapshot.active(session)], [expected])
        for strategy in self.snapshot.strategies:
            self.assertEqual(strategy.enabled, strategy.id.endswith("_adaptive"))

    def test_exact_declarations_and_three_distinct_short_role_rules(self):
        rules = []
        for profile in ("asia_adaptive", "london_adaptive", "us_adaptive"):
            strategy = self.snapshot.get(profile)
            manifest = strategy.manifest()
            self.assertEqual((strategy.entry_reference, strategy.stop_reference, strategy.target_r,
                              strategy.max_hold_minutes, strategy.cooldown_seconds,
                              strategy.one_entry_per_event, strategy.m5_boost, strategy.regime_filter,
                              strategy.demo_only, strategy.analysis_mode),
                             ("closed_m15", "m5_extreme", 3., 90, 60, True, False, "none", True, True))
            self.assertIs(manifest["analysis_mode"], True)
            self.assertEqual(set(strategy.ai_rules), {"entry", "review", "guardian"})
            self.assertTrue(all(0 < len(rule) <= 2000 for rule in strategy.ai_rules.values()))
            self.assertEqual(len(set(strategy.ai_rules.values())), 3)
            rules.extend(strategy.ai_rules.values())
        self.assertEqual(len(set(rules)), 9)
        self.assertIs(self.snapshot.get("session_retest").manifest()["analysis_mode"], False)

    def test_analysis_mode_is_explicit_boolean_and_demo_single_checkpoint_only(self):
        strategy = self.snapshot.get("asia_adaptive")
        for changes in ({"analysis_mode": "true"}, {"analysis_mode": 1},
                        {"demo_only": False}, {"one_entry_per_event": False}):
            with self.subTest(changes=changes), self.assertRaises(StrategyError):
                Registry.validate(replace(strategy, **changes))
        Registry.validate(replace(strategy, analysis_mode=False, demo_only=False))

    def test_definition_parameter_edits_keep_contract_role_rules_and_soft_math_coherent(self):
        with tempfile.TemporaryDirectory() as directory:
            for profile in ("asia_adaptive", "london_adaptive", "us_adaptive"):
                origin = Path(__file__).resolve().parents[1] / "strategies/definitions" / (profile+".py")
                edited = origin.read_text(encoding="utf-8").replace("target_r=3.", "target_r=4.").replace(
                    "max_hold_minutes=90", "max_hold_minutes=120").replace("cooldown_seconds=60", "cooldown_seconds=75")
                path = Path(directory) / (profile+".py")
                path.write_text(edited, encoding="utf-8")
                snapshot = Registry(directory).snapshot()
                self.assertFalse(snapshot.error, snapshot.error)
                strategy = snapshot.get(profile)
                self.assertEqual((strategy.target_r, strategy.max_hold_minutes, strategy.cooldown_seconds), (4., 120, 75))
                self.assertIn("4R", strategy.ai_rules["entry"])
                self.assertIn("75秒", strategy.ai_rules["review"])
                self.assertIn("120分钟", strategy.ai_rules["guardian"])
                self.assertIn("4R", strategy.ai_rules["guardian"])
                result = strategy.assess(context(profile))
                self.assertFalse(result.blockers)
                self.assertEqual({name: item["weight"] for name, item in result.evidence["soft_scoring"]["components"].items()},
                                 strategy.parameters["soft_scoring"]["weights"])

    def test_all_profiles_allow_weak_score_without_fixed_pattern_both_sides(self):
        for profile in ("asia_adaptive", "london_adaptive", "us_adaptive"):
            for action in ("buy", "sell"):
                with self.subTest(profile=profile, action=action):
                    c = context(profile, action)
                    result = self.assess(c, profile)
                    self.assertEqual(result.blockers, ())
                    self.assertLess(result.score, 55)
                    self.assertNotIn("entry_event", result.evidence)
                    self.assertNotIn("closed_trigger", result.evidence)
                    self.assertIs(result.evidence["trade_trigger_confirmed"], False)
                    self.assertTrue(result.evidence["bar_behavior"]["m5_patterns"]["current_doji"])
                    self.assertFalse(result.evidence["bar_behavior"]["m5_patterns"]["body_engulf_previous"])
                    self.assertEqual(self.validate(result.evidence, c, profile=profile, action=action), "")
                    json.dumps(result.evidence, allow_nan=False)

    def test_h1_opposing_or_unknown_is_hard_blocker(self):
        for profile in ("asia_adaptive", "london_adaptive", "us_adaptive"):
            for action in ("buy", "sell"):
                for trend in ("bear" if action == "buy" else "bull", "neutral"):
                    result = self.assess(replace(context(profile, action), h1_trend=trend), profile)
                    self.assertTrue(any("H1" in blocker for blocker in result.blockers))

    def test_opposing_m5_and_extreme_location_are_soft_context(self):
        c = context()
        altered = c.df_m5.copy()
        altered.loc[altered.index[-1], ["open", "high", "low", "close"]] = [100.8, 101., 99., 99.2]
        result = self.assess(replace(c, df_m5=altered))
        self.assertFalse(result.blockers)
        self.assertIs(result.evidence["bar_behavior"]["m5_opposes_action"], True)
        self.assertLess(result.score, 55)

    def test_same_actual_context_has_distinct_profile_soft_contributions(self):
        c = context()
        m15, m5 = c.df_m15.copy(), c.df_m5.copy()
        # Actual directional expansion over the previous M15 extreme, and M5
        # crossing/holding it. These facts are context, not a mandated pattern.
        m15.loc[m15.index[-1], ["open", "high", "low", "close"]] = [100., 102., 99., 101.5]
        m5.loc[m5.index[-1], ["open", "high", "low", "close"]] = [100., 102., 99., 101.5]
        scores, facts_by_profile = [], []
        for profile in ("asia_adaptive", "london_adaptive", "us_adaptive"):
            result = self.assess(replace(c, df_m15=m15, df_m5=m5), profile)
            self.assertFalse(result.blockers)
            soft = result.evidence["soft_scoring"]
            self.assertEqual(result.score, 40+sum(item["contribution"] for item in soft["components"].values()))
            self.assertEqual(soft["thresholds"], {"range_edge_fraction": .3,
                                                  "small_trend_max_atr": 1., "reaction_range_atr": 1.25})
            expected_weights = self.snapshot.get(profile).parameters["soft_scoring"]["weights"]
            for name, item in soft["components"].items():
                self.assertEqual(item["weight"], expected_weights[name])
                self.assertEqual(item["contribution"], item["weight"] if item["fact"] else 0)
            self.assertIs(soft["components"]["directional_expansion"]["fact"], True)
            self.assertIs(soft["components"]["prior_extreme_cross_hold"]["fact"], True)
            scores.append(result.score)
            facts_by_profile.append({name: item["fact"] for name, item in soft["components"].items()})
        self.assertEqual(scores, [62, 75, 78])
        self.assertEqual(facts_by_profile[0], facts_by_profile[1])
        self.assertEqual(facts_by_profile[1], facts_by_profile[2])

    def test_prices_and_extremes_are_exact_source_facts(self):
        c = context()
        altered = c.df_m5.copy()
        altered.loc[altered.index[-2], "low"] = 98.12345
        result = self.assess(replace(c, df_m5=altered))
        self.assertEqual(result.evidence["recent_extremes"]["m5_protection_low"], 98.12345)
        self.assertEqual(result.evidence["recent_extremes"]["m5_protection_bars"][1]["low"], 98.12345)
        self.assertEqual(result.evidence["position"]["reference_close"], c.df_m15.close.iloc[-1])
        self.assertTrue(result.evidence["style_menu"])
        self.assertTrue(result.evidence["uncertainty"])

    def test_checkpoint_identity_is_directional_profile_and_source_specific(self):
        c = context()
        evidence = self.assess(c).evidence
        buy_id = evidence["event_id"]
        self.assertEqual(buy_id, self.assess(replace(c, now=c.now+timedelta(seconds=10))).evidence["event_id"])
        self.assertNotEqual(buy_id, self.assess(context(action="sell")).evidence["event_id"])
        self.assertNotEqual(buy_id, self.assess(context("london_adaptive"), "london_adaptive").evidence["event_id"])
        next_c = context(clock=c.now+timedelta(minutes=5))
        self.assertNotEqual(buy_id, self.assess(next_c).evidence["event_id"])
        cp = evidence["analysis_checkpoint"]
        self.assertEqual(checkpoint_id(cp["profile"], cp["action"], cp["m5_bar_time"], cp["m15_bar_time"], 3),
                         checkpoint_id(cp["profile"], cp["action"], cp["m5_bar_time"], cp["m15_bar_time"], 3.0))
        self.assertNotEqual(buy_id, checkpoint_id(cp["profile"], cp["action"], cp["m5_bar_time"], cp["m15_bar_time"], -3))

    def test_checkpoint_close_inclusive_next_close_exclusive_signed_offsets(self):
        for offset in (0, 3, -4, 5.5, -3.5):
            with self.subTest(offset=offset):
                c = context(offset=offset)
                e = self.assess(c).evidence
                cp = e["analysis_checkpoint"]
                self.assertEqual(cp["valid_until_utc"], cp["m5_bar_time"]+600-offset*3600)
                self.assertEqual(self.validate(e, c), "")
                self.assertEqual(self.validate(e, replace(c, now=c.now+timedelta(seconds=299))), "")
                self.assertTrue(self.validate(e, replace(c, now=c.now+timedelta(seconds=300))))
                self.assertTrue(self.validate(e, replace(c, now=c.now-timedelta(seconds=1))))

    def test_no_first_open_or_fixed_window_requirement(self):
        for hour in (0, 3, 7, 14, 19, 23):
            c = context(clock=datetime(2026, 10, 10, hour, 35, tzinfo=timezone.utc))
            result = self.assess(c)
            self.assertFalse(result.blockers, result.blockers)
            self.assertEqual(self.validate(result.evidence, c), "")
            self.assertNotIn("windows", result.evidence)

    def test_checkpoint_without_frames_still_rejects_shape_identity_clock_and_expiry(self):
        c = context()
        original = self.assess(c).evidence
        self.assertEqual(validate_analysis_checkpoint(original, c.now, c.server_utc_offset), "")
        alterations = []
        for key, value in (("kind", "entry-event-v1"), ("profile", "fake"), ("action", "no"),
                           ("m5_bar_time", 0), ("m15_bar_time", 0), ("server_offset_hours", -3),
                           ("observed_utc", c.now.timestamp()+1), ("valid_until_utc", c.now.timestamp()+301),
                           ("source_refs", []), ("checkpoint_id", "fake")):
            e = copy.deepcopy(original)
            e["analysis_checkpoint"][key] = value
            alterations.append(e)
        for key, value in (("event_id", "fake"), ("profile", "us_adaptive"), ("action", "sell"),
                           ("server_offset_hours", -3), ("trade_trigger_confirmed", True),
                           ("entry_event", {}), ("closed_trigger", {"confirmed": True})):
            e = copy.deepcopy(original)
            e[key] = value
            alterations.append(e)
        reordered = copy.deepcopy(original)
        reordered["analysis_checkpoint"]["source_refs"].reverse()
        alterations.append(reordered)
        for evidence in alterations:
            self.assertTrue(validate_analysis_checkpoint(evidence, c.now, c.server_utc_offset))
        for clock, offset in ((c.now.replace(tzinfo=None), 3), (c.now, None), (c.now, True),
                              (c.now, float("nan")), (c.now, 15), (c.now, "3")):
            self.assertTrue(validate_analysis_checkpoint(original, clock, offset))
        self.assertTrue(self.validate(original, c, profile="us_adaptive"))
        self.assertTrue(self.validate(original, c, action="sell"))

    def test_supplied_sources_reject_missing_reordered_gapped_stale_and_unclosed(self):
        c = context()
        e = self.assess(c).evidence
        for attr in ("df_m5", "df_m15"):
            frame = getattr(c, attr)
            altered_frames = [frame.iloc[:-1], frame.iloc[::-1], frame.iloc[:0],
                              pd.concat([frame, frame.iloc[-1:]], ignore_index=True)]
            future = frame.copy()
            future.loc[future.index[-1], "time"] += 300 if attr == "df_m5" else 900
            altered_frames.append(future)
            missing_ohlc = frame.drop(columns="open")
            altered_frames.append(missing_ohlc)
            invalid = frame.copy()
            invalid.loc[invalid.index[-1], "low"] = 102.
            altered_frames.append(invalid)
            if attr == "df_m5":
                altered_frames.append(frame.drop(frame.index[-2]))
                altered_frames.append(frame.iloc[-2:])
            for altered in altered_frames:
                with self.subTest(attr=attr, shape=altered.shape):
                    changed = replace(c, **{attr: altered})
                    self.assertTrue(self.validate(e, changed))
                    self.assertTrue(self.assess(changed).blockers)

    def test_invalid_h1_atr_volume_or_price_never_publish_checkpoint(self):
        c = context()
        changes = [{"df_h1": None}, {"df_h1": c.df_h1.iloc[:-1]},
                   {"atr": 0.}, {"atr": float("nan")}, {"now": c.now.replace(tzinfo=None)}]
        for key in ("close", "tick_volume"):
            for value in (float("nan"), -1.):
                altered = c.df_m15.astype(float).copy()
                altered.loc[altered.index[-1], key] = value
                changes.append({"df_m15": altered})
        for change in changes:
            result = self.assess(replace(c, **change))
            self.assertTrue(result.blockers)
            self.assertNotIn("analysis_checkpoint", result.evidence)
            json.dumps(result.evidence, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
