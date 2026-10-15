"""Explicit, frozen DEMO direction declarations; absent declarations stay legacy."""
from copy import deepcopy
from dataclasses import replace
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from strategies.direction import DEFAULT_POLICY, LEGACY_VERSION, VERSION
from strategies.framework import Registry, StrategyError


PROFILES = ("asia_adaptive", "london_adaptive", "us_adaptive")
WEIGHTS = {
    "asia_adaptive": dict(directional_change=5, same_direction_m5=5, range_edge=10, m5_reclaim=10,
                          small_trend=8, directional_expansion=2, prior_extreme_cross_hold=2,
                          volatility_reaction=0),
    "london_adaptive": dict(directional_change=5, same_direction_m5=5, range_edge=2, m5_reclaim=3,
                            small_trend=2, directional_expansion=12, prior_extreme_cross_hold=8,
                            volatility_reaction=3),
    "us_adaptive": dict(directional_change=10, same_direction_m5=8, range_edge=3, m5_reclaim=5,
                        small_trend=2, directional_expansion=5, prior_extreme_cross_hold=3,
                        volatility_reaction=10),
}


def definition(profile):
    return importlib.import_module("strategies.definitions." + profile)


class BidirectionalStrategyContractTests(unittest.TestCase):
    def test_all_sessions_explicitly_declare_identical_registered_policy_version_two(self):
        policies = []
        expected = {"version": VERSION, "break_lookback_m15": 2, "leg_ttl_m15": 6,
                    "break_margin_atr": .10, "break_margin_ticks": 2, "failure_closes_m5": 2,
                    "anchor_pivot_left_m5": 1, "anchor_pivot_right_m5": 1}
        for profile in PROFILES:
            strategy = definition(profile).STRATEGY
            with self.subTest(profile=profile):
                Registry.validate(strategy)
                self.assertEqual(strategy.version, "3")
                self.assertEqual(strategy.direction_policy, expected)
                self.assertEqual(strategy.manifest()["direction_policy"], expected)
                self.assertTrue(strategy.demo_only)
                self.assertTrue(strategy.analysis_mode)
                policies.append(strategy.direction_policy)
        self.assertIsNot(policies[0], policies[1])
        self.assertIsNot(policies[1], policies[2])
        for policy in policies:
            self.assertIsNot(policy, DEFAULT_POLICY)

    def test_soft_scores_target_and_lifecycle_parameters_remain_registered_baseline(self):
        sessions = {"asia_adaptive": {"asia": 0}, "london_adaptive": {"london": 0},
                    "us_adaptive": {"overlap": 0, "late_us": 0}}
        for profile in PROFILES:
            module = definition(profile)
            strategy = module.STRATEGY
            with self.subTest(profile=profile):
                self.assertEqual(module.P, dict(profile=profile, target_r=3., max_hold_minutes=90,
                    cooldown_seconds=60, soft_scoring=dict(range_edge_fraction=.3,
                        small_trend_max_atr=1., reaction_range_atr=1.25, weights=WEIGHTS[profile])))
                self.assertEqual(strategy.parameters, module.P)
                self.assertEqual(strategy.sessions, sessions[profile])
                self.assertEqual(strategy.target_r, 3.)
                self.assertEqual(strategy.max_hold_minutes, 90)
                self.assertEqual(strategy.cooldown_seconds, 60)
                self.assertEqual(strategy.entry_reference, "closed_m15")
                self.assertEqual(strategy.stop_reference, "m5_extreme")
                self.assertEqual(strategy.regime_filter, "none")
                self.assertFalse(strategy.m5_boost)
                self.assertTrue(strategy.one_entry_per_event)
                self.assertTrue(strategy.enabled)
                self.assertEqual(strategy.manifest()["holding_policy"], "ai_review")

    def test_definition_evaluation_forwards_exact_host_context_and_existing_soft_parameters(self):
        context, result = object(), object()
        for profile in PROFILES:
            module = definition(profile)
            with self.subTest(profile=profile), patch.object(module, "evaluate_profile", return_value=result) as call:
                self.assertIs(module.evaluate(context), result)
                call.assert_called_once_with(context, profile, module.P["soft_scoring"],
                                             direction_policy=module.STRATEGY.direction_policy)

    def test_new_policy_cannot_escape_demo_autonomous_contract(self):
        strategy = definition(PROFILES[0]).STRATEGY
        for flags in [dict(demo_only=False), dict(analysis_mode=False),
                      dict(demo_only=False, analysis_mode=False), dict(one_entry_per_event=False)]:
            with self.subTest(flags=flags), self.assertRaises(StrategyError):
                Registry.validate(replace(strategy, **flags))

    def test_absent_or_explicit_legacy_policy_preserves_exact_h1_only_manifest(self):
        strategy = definition(PROFILES[0]).STRATEGY
        absent = replace(strategy, direction_policy=None, analysis_mode=False, demo_only=False)
        explicit = replace(absent, direction_policy={"version": LEGACY_VERSION})
        Registry.validate(absent)
        Registry.validate(explicit)
        self.assertEqual(absent.manifest()["direction_policy"], {"version": "h1-only-v1"})
        self.assertEqual(absent.manifest(), explicit.manifest())

    def test_malformed_unregistered_and_incomplete_policies_cannot_load(self):
        strategy = definition(PROFILES[0]).STRATEGY
        missing = {key: value for key, value in DEFAULT_POLICY.items() if key != "failure_closes_m5"}
        invalid = ["h1-or-m15-structure-v1", {}, {"version": VERSION}, missing,
                   dict(DEFAULT_POLICY, version="h1-or-m15-structure-v99"),
                   dict(DEFAULT_POLICY, failure_closes_m5=1),
                   dict(DEFAULT_POLICY, break_margin_atr=float("nan")),
                   dict(DEFAULT_POLICY, break_lookback_m15=True),
                   dict(DEFAULT_POLICY, fallback="h1"),
                   {"version": LEGACY_VERSION, "break_lookback_m15": 2}]
        for policy in invalid:
            with self.subTest(policy=policy), self.assertRaises(StrategyError):
                Registry.validate(replace(strategy, direction_policy=policy))

    def test_manifest_snapshot_is_json_finite_and_cannot_mutate_definition(self):
        for profile in PROFILES:
            strategy = definition(profile).STRATEGY
            before = deepcopy(strategy.direction_policy)
            first = strategy.manifest()
            json.dumps(first, allow_nan=False)
            first["direction_policy"]["break_lookback_m15"] = 100
            first["parameters"]["soft_scoring"]["weights"]["directional_change"] = 999
            self.assertEqual(strategy.direction_policy, before)
            self.assertEqual(strategy.manifest()["parameters"]["soft_scoring"]["weights"], WEIGHTS[profile])

    def test_role_rules_have_no_old_h1_only_contradiction_and_keep_public_boundaries(self):
        banned = ("H1同向仍是硬限制", "不能借反转主题逆H1", "反转也不得逆H1",
                  "保留H1同向", "H1同向、SL8")
        for profile in PROFILES:
            rules = definition(profile).STRATEGY.ai_rules
            with self.subTest(profile=profile):
                self.assertEqual(set(rules), {"entry", "review", "guardian"})
                for role, text in rules.items():
                    self.assertLessEqual(len(text), 2000, role)
                    self.assertIn("BUY/SELL", text, role)
                    self.assertIn("direction_permission", text, role)
                    for phrase in banned:
                        self.assertNotIn(phrase, text, role)
                entry, review, guardian = (rules[key] for key in ("entry", "review", "guardian"))
                for text in (entry, review):
                    self.assertIn("H1", text)
                    self.assertIn("背景", text)
                    self.assertIn("连续两根M5收盘失败", text)
                    self.assertIn("SL8", text)
                    self.assertIn("RR1.5", text)
                    self.assertIn("三M5极值", text)
                    self.assertIn("缓冲", text)
                    self.assertIn("规范", text)
                    self.assertIn("锚点", text)
                self.assertIn("H1偏空时有效M15上涨可许可BUY", entry)
                self.assertIn("H1偏多时有效M15下跌可许可SELL", entry)
                self.assertIn("最强反证", entry)
                self.assertIn("不因亏损自动反手", entry)
                self.assertIn("赚亏及双向交易同看", entry)
                self.assertIn("每POST", review)
                self.assertIn("固定SL/TP", review)
                self.assertIn("迁移支点", review)
                self.assertIn("no/REJECT/截断不重审", review)
                self.assertIn("入场保存", guardian)
                self.assertIn("原路线", guardian)
                self.assertIn("不能仅因当前H1逆向自动平仓", guardian)
                self.assertIn("不以当前策略重建旧仓", guardian)
                self.assertIn("入场许可/检查点过期不等于持仓理由失效", guardian)
                self.assertIn("90分钟为持仓复核节点", guardian)
                self.assertIn("profit-floor-v3", guardian)
                self.assertIn("普通跟随保留完整M5空间", guardian)
                self.assertIn("现金阶梯", guardian)
                self.assertIn("tp_review", guardian)
                self.assertIn("已有旧仓沿用原冻结规则", guardian)

    def test_registry_rejects_broken_policy_then_reloads_explicit_valid_source(self):
        template = ("from strategies.framework import Strategy, Assessment\n"
                    "STRATEGY=Strategy(id='offline',name='offline',version='1',sessions={'asia':0},"
                    "evaluate=lambda c:Assessment(0,{}),demo_only=True,analysis_mode=True,"
                    "one_entry_per_event=True,ai_rules={'entry':'e','review':'r','guardian':'g'}__POLICY__)\n")
        with tempfile.TemporaryDirectory(prefix="direction-contract-") as folder:
            path = Path(folder) / "offline.py"
            path.write_text(template.replace("__POLICY__", ""), encoding="utf-8")
            registry = Registry(folder)
            legacy = registry.snapshot()
            self.assertFalse(legacy.error)
            self.assertEqual(legacy.get("offline").manifest()["direction_policy"], {"version": LEGACY_VERSION})
            path.write_text(template.replace("__POLICY__", ",direction_policy={'version':'unknown'}"), encoding="utf-8")
            broken = registry.snapshot()
            self.assertTrue(broken.error)
            self.assertEqual(broken.strategies, ())
            with self.assertRaises(StrategyError):
                registry.require_current(legacy.fingerprint)
            path.write_text(template.replace("__POLICY__", ",direction_policy=" + repr(DEFAULT_POLICY)), encoding="utf-8")
            valid = registry.snapshot()
            self.assertFalse(valid.error)
            self.assertEqual(valid.get("offline").manifest()["direction_policy"], DEFAULT_POLICY)
            self.assertNotEqual(valid.fingerprint, legacy.fingerprint)
            with self.assertRaises(StrategyError):
                registry.require_current(legacy.fingerprint)


if __name__ == "__main__":
    unittest.main()
