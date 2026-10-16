"""Bound severity grading, strict old-card compatibility and stable profit policy."""
import copy
import itertools
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
import xauusd_fib_volume_pa_ai3 as ai
from strategies import quality, profit_guard
from test_ai_entry_gate_v2 import quality_sample
from test_ai_citations import cite, bindings, facts_pool


def sample(side="buy", support="adequate"):
    pack, raw = quality_sample(side)
    raw.update(opportunity_id="", wait_basis=[])
    raw["quality_assessment"] = {
        "version": "entry-quality-v5", "direction": side, "issues": [],
        "dimensions": {name: {"support": support, "reason": "本项原始闭合证据支持",
                               "evidence_refs": ["/h1_trend"]} for name in quality.DIMENSIONS}}
    return pack, raw


def opinion(dimension="target", severity="material", kind="reservation",
            cause="target_uncertainty", event="independent-target"):
    return dict(dimension=dimension, severity=severity, kind=kind, cause=cause,
                event_group=event, reason="本项独立证据的不利程度", evidence_refs=["/h1_trend"])


class BoundQualityTests(unittest.TestCase):
    def test_current_schema_binds_severity_once_and_removes_independent_final_levels(self):
        schema = ai.response_json_schema("AI-1")
        Draft202012Validator.check_schema(schema)
        card = schema["properties"]["quality_assessment"]
        self.assertEqual(card["properties"]["version"]["enum"], ["entry-quality-v5"])
        self.assertEqual(set(schema["$defs"]["quality_dimension"]["properties"]),
                         {"support", "reason", "evidence_refs"})
        for side in ("buy", "sell"):
            pack, raw = sample(side)
            self.assertTrue(Draft202012Validator(schema).is_valid(raw))
            normalized, error = ai.normalize_ai1_quality(raw, pack)
            self.assertEqual(error, "")
            self.assertEqual(normalized["quality_score"], 80)
            self.assertEqual(normalized["quality_version"], "entry-quality-v5")

    def test_each_grade_is_derived_from_only_its_owned_issues_without_mutating_wire(self):
        for severity, expected_level, expected_score in (("minor", "adequate", 80),
                                                        ("material", "weak", 72),
                                                        ("blocking", "missing", 64)):
            for side in ("buy", "sell"):
                pack, raw = sample(side)
                raw["quality_assessment"]["issues"] = [opinion(severity=severity)]
                if severity == "blocking":
                    raw.update(action="no", stop_loss=None, take_profit=None,
                               wait_basis=[dict(code="target_space", reason="独立目标空间不足",
                                                evidence_refs=["/h1_trend"])])
                before = copy.deepcopy(raw)
                normalized, error = ai.normalize_ai1_quality(raw, pack)
                self.assertEqual(error, "")
                self.assertEqual(raw, before)
                self.assertEqual(normalized["quality_assessment"], before["quality_assessment"])
                self.assertEqual(normalized["quality_grades"]["target"], expected_level)
                self.assertEqual(normalized["quality_score"], expected_score)
                self.assertEqual(ai.normalize_ai1_quality(normalized, pack)[1], "")

    def test_strong_support_still_obeys_minor_and_material_issues(self):
        for severity, expected in (("minor", 96), ("material", 88)):
            pack, raw = sample(support="strong")
            raw["quality_assessment"]["issues"] = [opinion(severity=severity)]
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertEqual(error, "")
            self.assertEqual(result["quality_score"], expected)

    def test_mixed_severity_preserves_each_issue_and_deducts_once(self):
        pack, raw = sample()
        raw["quality_assessment"]["issues"] = [opinion(severity="material"),
            opinion(severity="minor", kind="observation", event="independent-obstacle")]
        before = copy.deepcopy(raw)
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertEqual(error, "")
        self.assertEqual(result["quality_score"], 72)
        self.assertEqual(result["quality_assessment"]["issues"], before["quality_assessment"]["issues"])
        self.assertEqual(raw, before)

    def test_mixed_reservations_keep_their_own_declared_severity(self):
        pack, raw = sample()
        raw.update(action="no", stop_loss=None, take_profit=None,
                   wait_basis=[dict(code="target_space", reason="目标必要空间缺失", evidence_refs=["/h1_trend"])])
        entries = [opinion(severity=s, event="target-"+s) for s in ("minor", "material", "blocking")]
        raw["quality_assessment"]["issues"] = entries
        projection, error = quality.decode_bound(raw["quality_assessment"])
        self.assertEqual(error, "")
        self.assertEqual([x["severity"] for x in projection["reservations"]], ["minor", "material", "blocking"])
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertEqual(error, "")
        self.assertEqual(result["quality_score"], 64)
        self.assertEqual(result["quality_assessment"]["issues"], entries)

    def test_blocking_cannot_become_a_trade_even_with_strong_support(self):
        for side in ("buy", "sell"):
            pack, raw = sample(side, support="strong")
            raw["quality_assessment"]["issues"] = [opinion("protection", "blocking", cause="hard_limit")]
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertIn("阻断条件", error)
            self.assertNotIn("quality_score", result)

    def test_minor_only_causes_cannot_become_material_or_reserved(self):
        for cause in ("volume", "shallow_retrace"):
            for severity, kind in (("material", "observation"), ("minor", "reservation")):
                pack, raw = sample()
                raw["quality_assessment"]["issues"] = [opinion(cause=cause,severity=severity,kind=kind)]
                self.assertTrue(ai.normalize_ai1_quality(raw, pack)[1])
                self.assertFalse(Draft202012Validator(ai.response_json_schema("AI-1")).is_valid(raw))

    def test_snapshot_and_direction_isolation_apply_to_every_v5_issue(self):
        pack = ai.prepare_ai1_request_pack(facts_pool())
        _, raw = sample()
        raw["opportunity_id"] = pack["opportunities"][1]["opportunity_id"]
        raw["quality_assessment"]["issues"] = [opinion("counter_evidence", "minor", "observation", "opposing_structure")]
        raw = cite(pack, raw)
        good, error = ai.normalize_ai1_quality(raw, pack)
        self.assertEqual(error, "")
        schema = ai.response_json_schema("AI-1", pack)
        self.assertTrue(Draft202012Validator(schema).is_valid(raw))
        foreign = next(k for k,v in bindings(pack).items() if v == "/opportunities/0/strategy_evidence/buy/evidence/volume")
        raw["quality_assessment"]["issues"][0]["evidence_refs"] = [foreign]
        self.assertIn("隔离拒绝", ai.normalize_ai1_quality(raw, pack)[1])
        raw["quality_assessment"]["issues"][0]["evidence_refs"] = ["previous-snapshot-id"]
        self.assertTrue(ai.normalize_ai1_quality(raw, pack)[1])

    def test_valid_v4_grade_combinations_retain_exact_points_in_new_declarations(self):
        # Complete, independent cards only; no archived bad reply is transformed.
        for levels in itertools.product(quality.LEVEL_RULES, repeat=5):
            legacy = {"version": quality.LEDGER_VERSION, "direction": "buy", "issues": [], "dimensions": {}}
            current = {"version": quality.CURRENT_VERSION, "direction": "buy", "issues": [], "dimensions": {}}
            for name, level in zip(quality.DIMENSIONS, levels):
                evidence = dict(reason="独立证据结论", evidence_refs=["/h1_trend"])
                legacy["dimensions"][name] = dict(level=level, **evidence)
                current["dimensions"][name] = dict(support="strong" if level=="strong" else "adequate", **evidence)
                if level in ("weak", "missing"):
                    item = opinion(name, "material" if level=="weak" else "blocking", cause="stop_vulnerability", event=name)
                    current["issues"].append(item)
                    legacy["issues"].append({k:v for k,v in item.items() if k!="severity"})
            old, error = quality.decode_ledger(legacy)
            self.assertEqual(error, "")
            new, error = quality.decode_bound(current)
            self.assertEqual(error, "")
            self.assertEqual({k:v["level"] for k,v in old["dimensions"].items()},
                             {k:v["level"] for k,v in new["dimensions"].items()})

    def test_material_counter_wait_never_uses_minor_only_evidence(self):
        for severity, valid in (("minor", False), ("material", True)):
            pack, raw = sample()
            raw.update(action="no", stop_loss=None, take_profit=None,
                       wait_basis=[dict(code="material_counter_evidence", reason="独立反向证据",
                                        evidence_refs=["/h1_trend"])])
            raw["quality_assessment"]["issues"] = [opinion("counter_evidence",severity,
                                                        cause="opposing_structure")]
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertEqual(not error, valid)
            if not valid:
                self.assertNotIn("quality_score", result)

    def test_invalid_missing_or_duplicate_declarations_never_get_a_score(self):
        cases = []
        for key, value in (("severity", "unknown"), ("severity", None), ("dimension", "unknown"),
                           ("kind", "unknown"), ("reason", " "), ("evidence_refs", [])):
            _, raw = sample(); raw["quality_assessment"]["issues"] = [opinion()]
            raw["quality_assessment"]["issues"][0][key] = value; cases.append(raw)
        _, raw = sample(); raw["quality_assessment"]["issues"] = [opinion(), opinion("structure")]; cases.append(raw)
        _, raw = sample(); del raw["quality_assessment"]["dimensions"]["structure"]["support"]; cases.append(raw)
        _, raw = sample(); raw["quality_assessment"]["dimensions"]["structure"]["level"] = "weak"; cases.append(raw)
        for raw in cases:
            pack, _ = sample(); before = copy.deepcopy(raw)
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertTrue(error)
            self.assertEqual(raw, before)
            self.assertNotIn("quality_score", result)

    def test_hard_causes_are_blocking_and_cannot_hide_in_minor_observations(self):
        for cause in ("hard_limit", "missing_data"):
            for severity, kind in (("minor", "reservation"), ("material", "reservation"),
                                   ("minor", "observation")):
                pack, raw = sample()
                raw["quality_assessment"]["issues"] = [opinion(cause=cause, severity=severity,kind=kind)]
                result, error = ai.normalize_ai1_quality(raw, pack)
                self.assertTrue(error)
                self.assertNotIn("quality_score", result)
                self.assertFalse(Draft202012Validator(ai.response_json_schema("AI-1")).is_valid(raw))

    def test_guardian_remains_unsmoothed_v3_with_original_cash_steps(self):
        self.assertEqual(profit_guard.VERSION, "profit-floor-v3")
        self.assertEqual(profit_guard.policy()["cash_floors"], [[2., .2], [4., 1.], [6., 2.], [10., 4.], [15., 6.]])
        self.assertNotIn("profit-floor-v4", ai.AI3_INSTRUCTIONS)


class ArchivedOrphanGradeTests(unittest.TestCase):
    def test_both_real_v4_failures_stay_invalid_with_exact_post_catalogs(self):
        for suffix in ("121144", "161058"):
            path = Path(__file__).parent/"fixtures"/("ai1_quality_20261016_"+suffix+".json")
            archive = json.loads(path.read_text(encoding="utf-8"))
            raw, pack = archive["raw"], archive["request_pack"]
            self.assertEqual(json.loads(archive["raw_text"]), raw)
            self.assertTrue(ai.catalog_bindings(pack))
            schema = ai.response_json_schema("AI-1", pack, quality_version=ai.LEDGER_QUALITY_VERSION)
            self.assertTrue(Draft202012Validator(schema).is_valid(raw))
            before = copy.deepcopy(raw)
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertEqual(error, archive["expected_error"])
            self.assertEqual(raw, before)
            self.assertEqual(result["action"], "no")
            self.assertNotIn("quality_score", result)
            self.assertEqual(ai.ai1_result_status(result,error,None), "contract_invalid")

    def test_new_version_cannot_be_used_as_a_label_to_pass_an_old_broken_card(self):
        path = Path(__file__).parent/"fixtures/ai1_quality_20261016_161058.json"
        archive = json.loads(path.read_text(encoding="utf-8"))
        raw = copy.deepcopy(archive["raw"])
        raw["quality_assessment"]["version"] = quality.CURRENT_VERSION
        result, error = ai.normalize_ai1_quality(raw,archive["request_pack"])
        self.assertTrue(error)
        self.assertNotIn("quality_score",result)


if __name__ == "__main__":
    unittest.main()
