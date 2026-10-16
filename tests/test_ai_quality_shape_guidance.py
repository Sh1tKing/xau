"""Actual malformed replies stay rejected; fresh requests teach exact v5 shape."""
import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
import xauusd_fib_volume_pa_ai3 as ai
from strategies import quality
from test_ai_citations import bindings, cite, facts_pool
from test_ai_quality_bound_v5 import sample, opinion


def request_examples(pack):
    return pack["quality_reference_examples"]


class ArchivedShapeFailures(unittest.TestCase):
    def test_actual_primary_and_backup_replies_are_schema_violations_and_stay_rejected(self):
        for suffix in ("020230", "034026"):
            archive = json.loads((Path(__file__).parent / "fixtures" /
                                  ("ai1_quality_20261017_" + suffix + ".json")).read_text(encoding="utf-8"))
            raw, pack = archive["raw"], archive["request_pack"]
            parsed, error = ai.parse_json_object(archive["raw_text"])
            self.assertEqual(error, "")
            self.assertEqual(parsed, raw)
            self.assertTrue(ai.catalog_bindings(pack))
            schema = ai.response_json_schema("AI-1", pack)
            self.assertFalse(Draft202012Validator(schema).is_valid(raw))
            self.assertTrue(ai.response_schema_error(raw, schema))
            before = copy.deepcopy(raw)
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertTrue(error.startswith(archive["expected_error"]))
            self.assertEqual(raw, before)
            self.assertNotIn("quality_score", result)
            self.assertEqual(ai.ai1_result_status(result, error, None), "contract_invalid")

    def test_wrong_issue_owner_diagnostic_identifies_cause_dimension_confusion(self):
        pack, raw = sample()
        raw["quality_assessment"]["issues"] = [opinion(dimension="volume", kind="observation",
                                                      cause="volume", severity="minor")]
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertIn("dimension='volume'", error)
        self.assertIn("counter_evidence", error)
        self.assertNotIn("quality_score", result)


class FreshShapeGuidance(unittest.TestCase):
    def test_dynamic_example_ids_stay_after_catalog_and_outside_static_rubric_and_fact_hash(self):
        pack = ai.prepare_ai1_request_pack(facts_pool())
        self.assertNotIn("reference_examples", pack["quality_rubric"])
        self.assertGreater(list(pack).index("quality_reference_examples"), list(pack).index("evidence_catalog"))
        before = ai.catalog_bindings(pack)
        without_guidance = copy.deepcopy(pack)
        without_guidance.pop("quality_reference_examples")
        self.assertEqual(ai.catalog_bindings(without_guidance), before)
        self.assertFalse(ai.scope_allowed("/quality_reference_examples", 0, "buy"))
        tampered = copy.deepcopy(pack)
        tampered["live_quote"]["ask"] += .01
        with self.assertRaisesRegex(ValueError, "证据目录"):
            ai.catalog_bindings(tampered)

    def test_rubric_and_schema_distinguish_fixed_dimensions_from_issue_causes(self):
        rubric = ai.quality_rubric(True)
        contract = rubric["output_contract"]
        self.assertEqual(contract["version"], "ai1-quality-shape-v1")
        self.assertEqual(contract["dimension_values"], list(quality.DIMENSIONS))
        self.assertEqual(contract["minimum_evidence_refs"], 1)
        self.assertIn("volume", contract["issue_dimension_policy"])
        self.assertIn("cause", contract["issue_dimension_policy"])
        self.assertIn("target", contract["blocked_target_policy"])
        self.assertIn("no", contract["reference_policy"])
        self.assertIn("非空", ai.AI1_INSTRUCTIONS)
        schema = ai.response_json_schema("AI-1")
        Draft202012Validator.check_schema(schema)
        for branch in schema["properties"]["quality_assessment"]["properties"]["issues"]["items"]["anyOf"]:
            owner = branch["properties"]["dimension"]
            self.assertEqual(owner["enum"], list(quality.DIMENSIONS))
            self.assertIn("cause", owner["description"])

    def test_current_rubric_does_not_instruct_model_to_emit_legacy_issue_arrays_or_final_weak(self):
        text = ai.quality_rubric(True)["dimensions"]["counter_evidence"]
        self.assertNotIn("放observations", text)
        self.assertNotIn("放reservations", text)
        self.assertNotIn("评weak", text)
        self.assertIn("severity", text)
        self.assertIn("issues", text)
        self.assertIn("依赖", ai.ai1_json_template()["quality_assessment"]["dimensions"]["target"]["reason"])

    def test_reference_examples_use_exact_post_ids_and_candidate_ownership_without_mutating_facts(self):
        source = facts_pool()
        before = copy.deepcopy(source)
        pack = ai.prepare_ai1_request_pack(source)
        self.assertEqual(source, before)
        self.assertTrue(ai.catalog_bindings(pack))
        for index, example in enumerate(request_examples(pack)):
            candidate = pack["opportunities"][index]
            self.assertEqual(example["opportunity_id"], candidate["opportunity_id"])
            self.assertEqual(example["direction"], candidate["action"])
            volume = example["volume_observation"]
            self.assertEqual((volume["dimension"], volume["cause"], volume["kind"], volume["severity"]),
                             ("counter_evidence", "volume", "observation", "minor"))
            for snippet in (volume, example["unassessed_target"]):
                self.assertTrue(snippet["evidence_refs"])
                for identifier in snippet["evidence_refs"]:
                    path = bindings(pack)[identifier]
                    self.assertTrue(ai.scope_allowed(path, index, candidate["action"]), path)
                    self.assertTrue(ai._evidence_pointer_exists(pack, path), path)

    def test_refreshed_post_examples_never_reuse_prior_quote_ids(self):
        source = facts_pool()
        first = ai.prepare_ai1_request_pack(source)
        source["live_quote"]["ask"] += .03
        source["snapshot_id"] = "refreshed"
        second = ai.prepare_ai1_request_pack(source)
        self.assertNotEqual(first["evidence_catalog"]["scope"], second["evidence_catalog"]["scope"])
        old = set(bindings(first))
        new = set(bindings(second))
        for example in request_examples(second):
            for key in ("volume_observation", "unassessed_target"):
                refs = set(example[key]["evidence_refs"])
                self.assertTrue(refs <= new)
                self.assertFalse(refs & old)

    def test_absent_volume_is_not_invented_or_replaced_by_another_candidate(self):
        source = facts_pool()
        del source["opportunities"][0]["strategy_evidence"]["buy"]["evidence"]["volume"]
        pack = ai.prepare_ai1_request_pack(source)
        self.assertNotIn("volume_observation", request_examples(pack)[0])
        self.assertIn("volume_observation", request_examples(pack)[1])
        no_candidates = ai.prepare_ai1_request_pack({"opportunities": [], "quality_rubric": ai.quality_rubric(True)})
        self.assertEqual(request_examples(no_candidates), [])

    def test_bid_and_ask_reference_examples_are_mirrored_for_buy_and_sell(self):
        source = facts_pool()
        candidate = source["opportunities"][1]
        candidate["action"] = "sell"
        candidate["strategy_evidence"] = {"sell": candidate["strategy_evidence"]["buy"]}
        pack = ai.prepare_ai1_request_pack(source)
        for index, side in enumerate(("buy", "sell")):
            example = request_examples(pack)[index]
            paths = [bindings(pack)[ref] for ref in example["unassessed_target"]["evidence_refs"]]
            self.assertIn("/live_quote/" + ("ask" if side == "buy" else "bid"), paths)
            self.assertIn("/rules/max_stop_abs", paths)
            self.assertTrue(all(ai.scope_allowed(path, index, side) for path in paths))

    def test_independent_valid_volume_issue_has_no_sixth_dimension_or_score_change(self):
        for side in ("buy", "sell"):
            source = facts_pool()
            if side == "sell":
                for candidate in source["opportunities"]:
                    candidate["action"] = "sell"
                    candidate["strategy_evidence"] = {"sell": candidate["strategy_evidence"]["buy"]}
            pack = ai.prepare_ai1_request_pack(source)
            _, raw = sample(side)
            raw["opportunity_id"] = pack["opportunities"][1]["opportunity_id"]
            raw = cite(pack, raw)
            item = copy.deepcopy(request_examples(pack)[1]["volume_observation"])
            item.update(reason="本次量能仅构成轻微观察", event_group="independent-current-volume")
            raw["quality_assessment"]["issues"] = [item]
            self.assertTrue(Draft202012Validator(ai.response_json_schema("AI-1", pack)).is_valid(raw))
            before = copy.deepcopy(raw)
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertEqual(error, "")
            self.assertEqual(result["quality_score"], 80)
            self.assertEqual(result["action"], side)
            self.assertEqual(raw, before)
            # Exact same issue carrying another candidate's ID must still fail.
            raw["quality_assessment"]["issues"][0]["evidence_refs"] = \
                request_examples(pack)[0]["volume_observation"]["evidence_refs"]
            self.assertIn("隔离拒绝", ai.normalize_ai1_quality(raw, pack)[1])

    def test_independent_blocked_watch_cites_dependency_without_inventing_target_or_allowing_trade(self):
        for side in ("buy", "sell"):
            source = facts_pool()
            if side == "sell":
                for candidate in source["opportunities"]:
                    candidate["action"] = "sell"
                    candidate["strategy_evidence"] = {"sell": candidate["strategy_evidence"]["buy"]}
            pack = ai.prepare_ai1_request_pack(source)
            _, raw = sample(side)
            raw.update(opportunity_id=pack["opportunities"][1]["opportunity_id"], action="no",
                       stop_loss=None, take_profit=None)
            raw = cite(pack, raw)
            refs = request_examples(pack)[1]["unassessed_target"]["evidence_refs"]
            raw["wait_basis"] = [dict(code="hard_limit", reason="保护条件阻断，待新结构", evidence_refs=refs)]
            raw["quality_assessment"]["issues"] = [dict(opinion("protection", "blocking", cause="hard_limit"),
                                                        evidence_refs=refs)]
            raw["quality_assessment"]["dimensions"]["target"].update(
                reason="保护先阻断，目标未评估；引用依赖，不宣称RR达标", evidence_refs=refs)
            before = copy.deepcopy(raw)
            self.assertTrue(Draft202012Validator(ai.response_json_schema("AI-1", pack)).is_valid(raw))
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertEqual(error, "")
            self.assertEqual(raw, before)
            self.assertEqual((result["action"], result["quality_score"]), ("no", 64))
            self.assertEqual(len(result["quality_assessment"]["issues"]), 1)
            raw["action"] = side
            self.assertIn("阻断条件", ai.normalize_ai1_quality(raw, pack)[1])
            raw["action"] = "no"
            raw["quality_assessment"]["dimensions"]["target"]["evidence_refs"] = []
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertIn("/target/evidence_refs", error)
            self.assertNotIn("quality_score", result)


if __name__ == "__main__":
    unittest.main()
