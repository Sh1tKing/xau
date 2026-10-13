"""Uniform v4 wire ledger, archived duplicate, and complete offline gold chains."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
import xauusd_fib_volume_pa_ai3 as ai
from strategies import quality
from test_ai_entry_gate_v2 import quality_sample
from test_ai_citations import cite, bindings, facts_pool


def card(side="buy"):
    pack, raw = quality_sample(side)
    raw["opportunity_id"] = ""
    raw["quality_assessment"] = {
        "version": ai.QUALITY_VERSION, "direction": side,
        "dimensions": {name: {"level": "adequate", "reason": "本次实际事实",
                              "evidence_refs": ["/h1_trend"]} for name in ai.QUALITY_DIMENSIONS},
        "issues": []}
    return pack, raw


def issue(dimension="counter_evidence", kind="observation", cause="opposing_structure",
          event=None, refs=None):
    return {"dimension": dimension, "kind": kind, "cause": cause, "event_group": event,
            "reason": "本次实际证据", "evidence_refs": refs or ["/h1_trend"]}


class LedgerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = json.loads((Path(__file__).parent / "fixtures/ai1_quality_20261013_164602.json")
                                 .read_text(encoding="utf-8"))

    def test_archived_v3_duplicate_is_still_invalid_and_is_not_repaired(self):
        raw, pack = self.archive["raw"], self.archive["request_pack"]
        original = copy.deepcopy(raw)
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertIn("/structure/observations/0", error)
        self.assertIn("/counter_evidence/observations/0", error)
        self.assertEqual(raw, original)
        self.assertEqual(result["action"], "no")
        self.assertNotIn("quality_score", result)

    def test_independently_authored_v4_watch_keeps_archived_facts_and_grades(self):
        # New test-authored reply; no such rewriting is done by the production client.
        raw = copy.deepcopy(self.archive["raw"])
        old = raw["quality_assessment"]["dimensions"]
        ledger = []
        for name, kind in (("counter_evidence", "observations"), ("target", "observations"),
                           ("trigger", "reservations")):
            for observation in old[name][kind]:
                ledger.append({"dimension": name, "kind": kind[:-1], **observation})
        raw["quality_assessment"] = {
            "version": ai.QUALITY_VERSION, "direction": "buy", "issues": ledger,
            "dimensions": {name: {k: value[k] for k in ("level", "reason", "evidence_refs")}
                           for name, value in old.items()}}
        schema = ai.response_json_schema("AI-1", self.archive["request_pack"])
        self.assertTrue(Draft202012Validator(schema).is_valid(raw))
        original = copy.deepcopy(raw)
        result, error = ai.normalize_ai1_quality(raw, self.archive["request_pack"])
        self.assertEqual(error, "")
        self.assertEqual(raw, original)
        self.assertEqual(result["quality_score"], 64)
        self.assertEqual(result["action"], "no")
        self.assertEqual(ai.ai1_result_status(result, "", None), "watch")
        self.assertEqual({name: d["level"] for name, d in result["quality_assessment"]["dimensions"].items()},
                         {name: old[name]["level"] for name in ai.QUALITY_DIMENSIONS})
        self.assertEqual(result["quality_version"], ai.QUALITY_VERSION)

    def test_schema_has_one_issue_array_and_uniform_dimensions(self):
        schema = ai.response_json_schema("AI-1")
        Draft202012Validator.check_schema(schema)
        dim = schema["$defs"]["quality_dimension"]
        self.assertEqual(set(dim["properties"]), {"level", "reason", "evidence_refs"})
        fields = schema["properties"]["quality_assessment"]["properties"]
        self.assertEqual(set(fields), {"version", "direction", "dimensions", "issues"})
        self.assertEqual(fields["version"]["enum"], ["entry-quality-v4"])
        self.assertIn(quality.LEDGER_POLICY, ai.AI1_INSTRUCTIONS)
        self.assertEqual(ai.quality_rubric(True)["reservations_policy"], quality.CURRENT_RESERVATIONS_POLICY)
        self.assertLess(len(json.dumps(schema)),
                        len(json.dumps(ai.response_json_schema("AI-1", quality_version=ai.DIMENSIONAL_QUALITY_VERSION))))
        for level in ai.QUALITY_LEVEL_POINTS:
            pack, raw = card()
            raw["quality_assessment"]["dimensions"]["structure"]["level"] = level
            if level in ("weak", "missing"):
                raw["quality_assessment"]["issues"] = [issue("structure", "reservation")]
                raw.update(action="no", stop_loss=None, take_profit=None,
                           wait_basis=[{"code": "missing_structure", "reason": "结构待确认", "evidence_refs": ["/h1_trend"]}])
            self.assertTrue(Draft202012Validator(schema).is_valid(raw))
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertEqual(error, "")
            self.assertEqual(result["quality_score"], 64 + ai.QUALITY_LEVEL_POINTS[level])

    def test_v4_rejects_legacy_arrays_and_unknown_keys_without_conversion(self):
        for field in ("reservations", "observations", "reservation_ids"):
            pack, raw = card()
            raw["quality_assessment"]["dimensions"]["structure"][field] = []
            before = copy.deepcopy(raw)
            self.assertFalse(Draft202012Validator(ai.response_json_schema("AI-1")).is_valid(raw))
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertIn("v4只含", error)
            self.assertEqual(raw, before)
            self.assertEqual(result["quality_assessment"]["version"], ai.QUALITY_VERSION)
            self.assertNotIn("quality_score", result)

    def test_duplicated_issues_still_rejected_across_owners_kinds_and_ref_order(self):
        for kind in ("observation", "reservation"):
            for grouped in (False, True):
                pack, raw = card()
                pack["m15_bars"] = [{"time": 1}]
                group = "m15-1" if grouped else None
                refs = ["/h1_trend", "/m15_bars/0"]
                raw["quality_assessment"]["issues"] = [issue(event=group, refs=refs),
                    issue("structure", kind, event=group, refs=list(reversed(refs)))]
                result, error = ai.normalize_ai1_quality(raw, pack)
                self.assertIn("/issues/0", error)
                self.assertIn("/issues/1", error)
                self.assertNotIn("quality_score", result)
        raw["quality_assessment"]["issues"][1]["evidence_refs"].append(refs[0])
        self.assertIn("重复扣分", ai.normalize_ai1_quality(raw, pack)[1])

    def test_independent_events_with_same_cause_preserve_all_facts(self):
        pack, raw = card()
        raw["quality_assessment"]["issues"] = [issue(event="bar-1"), issue("structure", event="bar-2")]
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertEqual(error, "")
        self.assertEqual(len(result["quality_assessment"]["issues"]), 2)
        self.assertEqual(result["quality_score"], 80)
        self.assertNotIn("reservations", result["quality_assessment"])

    def test_bad_cause_kind_owner_and_empty_required_data_fail_without_mutation(self):
        cases = [("dimension", "other"), ("kind", "other"), ("cause", "invented"),
                 ("event_group", {}), ("evidence_refs", []), ("reason", " ")]
        for key, value in cases:
            pack, raw = card()
            raw["quality_assessment"]["issues"] = [issue()]
            raw["quality_assessment"]["issues"][0][key] = value
            before = copy.deepcopy(raw)
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertTrue(error)
            self.assertEqual(raw, before)
            self.assertNotIn("quality_score", result)
        for field in ("issues", "direction", "dimensions"):
            pack, raw = card()
            del raw["quality_assessment"][field]
            self.assertTrue(ai.normalize_ai1_quality(raw, pack)[1])

    def test_strong_cannot_own_issues_and_weak_missing_need_own_reservation(self):
        for level in ("strong", "weak", "missing"):
            pack, raw = card()
            raw["quality_assessment"]["dimensions"]["structure"]["level"] = level
            raw["quality_assessment"]["issues"] = [issue("structure")]
            self.assertTrue(ai.normalize_ai1_quality(raw, pack)[1])
            if level != "strong":
                raw["quality_assessment"]["issues"] = [issue("target", "reservation")]
                self.assertTrue(ai.normalize_ai1_quality(raw, pack)[1])

    def test_minor_observations_and_material_reservations_keep_separate_severity(self):
        pack, raw = card()
        raw["quality_assessment"]["dimensions"]["counter_evidence"]["level"] = "weak"
        raw["quality_assessment"]["issues"] = [issue(kind="reservation", event="confirmed"),
                                               issue(event="unconfirmed")]
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertEqual(error, "")
        self.assertEqual(result["quality_score"], 72)
        self.assertEqual(result["quality_assessment"]["dimensions"]["counter_evidence"]["level"], "weak")
        self.assertEqual([i["kind"] for i in result["quality_assessment"]["issues"]], ["reservation", "observation"])

    def test_normalized_v4_persists_only_declared_ledger_and_can_be_validated_again(self):
        pack, raw = card()
        raw["quality_assessment"]["issues"] = [issue()]
        original = copy.deepcopy(raw)
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertEqual(error, "")
        self.assertEqual(raw, original)
        self.assertEqual(result["quality_assessment"], raw["quality_assessment"])
        self.assertEqual(set(result["quality_assessment"]), {"version", "direction", "dimensions", "issues"})
        self.assertEqual(ai.normalize_ai1_quality(result, pack)[1], "")

    def test_minor_causes_cannot_be_reserved_and_hard_causes_cannot_be_observations(self):
        for kind, causes in (("reservation", ("volume", "shallow_retrace")),
                             ("observation", ("hard_limit", "missing_data"))):
            for cause in causes:
                pack, raw = card()
                raw["quality_assessment"]["issues"] = [issue(kind=kind, cause=cause)]
                self.assertFalse(Draft202012Validator(ai.response_json_schema("AI-1")).is_valid(raw))
                self.assertIn("程度不符", ai.normalize_ai1_quality(raw, pack)[1])

    def test_blocking_trade_and_minor_material_wait_remain_forbidden(self):
        pack, raw = card()
        raw["quality_assessment"]["dimensions"]["target"]["level"] = "missing"
        raw["quality_assessment"]["issues"] = [issue("target", "reservation", "hard_limit")]
        self.assertIn("阻断条件", ai.normalize_ai1_quality(raw, pack)[1])
        pack, raw = card()
        raw.update(action="no", stop_loss=None, take_profit=None,
                   wait_basis=[{"code": "material_counter_evidence", "reason": "仅轻微反向",
                                "evidence_refs": ["/h1_trend"]}])
        raw["quality_assessment"]["issues"] = [issue()]
        self.assertIn("仅有轻微", ai.normalize_ai1_quality(raw, pack)[1])

    def test_evidence_isolation_and_exact_snapshot_citations_cover_global_issues(self):
        pack = ai.prepare_ai1_request_pack(facts_pool())
        from test_ai_strategy_opportunities import payload
        old = payload()
        raw = {**old, "quality_assessment": {"version": ai.QUALITY_VERSION, "direction": "buy", "issues": [],
            "dimensions": {name: {k: v[k] for k in ("level", "reason", "evidence_refs")}
                           for name, v in old["quality_assessment"]["dimensions"].items()}}}
        raw = cite(pack, raw)
        foreign = next(k for k, v in bindings(pack).items() if v == "/opportunities/0/strategy_evidence/buy/evidence/volume")
        raw["quality_assessment"]["issues"] = [issue(refs=[foreign])]
        self.assertIn("隔离拒绝", ai.normalize_ai1_quality(raw, pack)[1])
        raw["quality_assessment"]["issues"][0]["evidence_refs"] = ["old-snapshot-id"]
        self.assertTrue(ai.normalize_ai1_quality(raw, pack)[1])

    def test_local_schema_diagnostics_detect_missing_ledger_and_wrong_wire_types(self):
        _, raw = card()
        schema = ai.response_json_schema("AI-1")
        samples = [raw]
        for change in ("ledger_missing", "owner_invalid", "kind_invalid", "empty_refs", "wrong_grade", "boolean_price"):
            value = copy.deepcopy(raw)
            if change == "ledger_missing":
                value["quality_assessment"].pop("issues")
            elif change in ("owner_invalid", "kind_invalid"):
                value["quality_assessment"]["issues"] = [issue()]
                value["quality_assessment"]["issues"][0]["dimension" if change == "owner_invalid" else "kind"] = "invalid"
            elif change == "empty_refs":
                value["quality_assessment"]["dimensions"]["structure"]["evidence_refs"] = []
            elif change == "wrong_grade":
                value["quality_assessment"]["dimensions"]["structure"]["level"] = "invalid"
            else:
                value["stop_loss"] = True
            samples.append(value)
        self.assertEqual(ai.response_schema_error(raw, schema), "")
        for value in samples:
            original = copy.deepcopy(value)
            self.assertEqual(not ai.response_schema_error(value, schema), Draft202012Validator(schema).is_valid(value))
            self.assertEqual(value, original)


class LedgerEntryChainTests(unittest.TestCase):
    def run_chain(self, profile="asia_adaptive", side="buy", failover=False, invalid=False, watch=False):
        from test_adaptive_ai_integration import AdaptiveAITests
        fixture = AdaptiveAITests()
        fixture.setUp()
        try:
            fixture.configure(profile, side)
            original = fixture.proposal
            packs = []
            def proposal(request):
                pack = json.loads(request["messages"][0]["content"].split("\n", 1)[1])
                packs.append(pack)
                self.assertEqual(pack["quality_rubric"]["version"], ai.QUALITY_VERSION)
                schema = ai.response_json_schema("AI-1", pack)
                self.assertEqual(request["output_config"]["format"]["schema"], schema)
                if failover and len(packs) == 1:
                    raise ai.requests.ReadTimeout("offline primary timeout")
                raw = original(request)
                raw["quality_assessment"] = {
                    "version": ai.QUALITY_VERSION, "direction": side,
                    "dimensions": {name: {"level": "adequate", "reason": "原始M5事实支持",
                                          "evidence_refs": ["/m5_bars/79"]} for name in ai.QUALITY_DIMENSIONS},
                    "issues": [issue(refs=["/m5_bars/79"])]}
                if invalid:
                    raw["quality_assessment"]["issues"].append(issue("structure", refs=["/m5_bars/79"]))
                if watch:
                    raw.update(action="no", stop_loss=None, take_profit=None,
                               wait_basis=[{"code": "target_space", "reason": "目标空间不足",
                                            "evidence_refs": ["/m5_bars/79"]}])
                raw = cite(pack, raw)
                self.assertTrue(Draft202012Validator(schema).is_valid(raw))
                return raw
            with patch.object(fixture, "proposal", side_effect=proposal), \
                    patch.object(ai, "AI1_URL", "https://primary.test/v1/messages"), \
                    patch.object(ai, "AI2_URL", "https://review.test/v1/messages"), \
                    patch.object(ai, "backup_chat_config", return_value={"model": "backup-unit", "effort": "low",
                        "url": "https://backup.test/v1/messages", "key": "mock"}):
                post = fixture.run_entry(protocol="messages")
            result = next(s[2] for s in fixture.snapshots if s[1] == "ai1_result")
            if invalid or watch:
                self.assertEqual(post.call_count, 1)
                self.assertEqual(fixture.sent, [])
                self.assertEqual(result["status"], "contract_invalid" if invalid else "watch")
                if invalid:
                    self.assertIn("/issues/0", result["quality_error"])
                    self.assertNotIn("quality_score", result["parsed"])
                else:
                    self.assertEqual(result["parsed"]["quality_score"], 80)
            else:
                self.assertEqual(post.call_count, 3 if failover else 2)
                self.assertEqual(len(fixture.sent), 1)
                self.assertEqual(result["quality_version"], ai.QUALITY_VERSION)
                self.assertEqual(result["parsed"]["quality_score"], 80)
                self.assertEqual(fixture.sent[0]["volume"], .01)
                saved = copy.deepcopy(fixture.trader.entry_contexts["123"])
                self.assertEqual(saved["quality_version"], ai.QUALITY_VERSION)
                self.assertEqual(saved["quality_assessment"]["issues"][0]["dimension"], "counter_evidence")
                self.assertEqual(saved["quality_assessment"]["issues"][0]["evidence_refs"], ["/m5_bars/79"])
                self.assertNotIn("reservations", saved["quality_assessment"])
                self.assertEqual(set(saved["quality_assessment"]["dimensions"]["counter_evidence"]),
                                 {"level", "reason", "evidence_refs"})
                fixture.trader.load_entry_contexts()
                self.assertEqual(fixture.trader.entry_contexts["123"], saved)
                review = next(s[2]["review"] for s in fixture.snapshots if s[1] == "ai2_input")
                self.assertNotIn("quality_assessment", review["ai1_raw_proposal"])
                self.assertNotIn("details", review["proposal"])
                guardian = fixture.trader.collect_ai3_snapshot()
                guard_pack = json.loads(guardian["prompt"].split("\n", 1)[1])
                self.assertNotIn("quality_assessment", guard_pack["positions"][0]["entry_context"])
                self.assertEqual(fixture.trader.entry_contexts["123"]["quality_assessment"], saved["quality_assessment"])
        finally:
            fixture.doCleanups()

    def test_three_sessions_both_directions_v4_through_ai2_and_saved_guardian_context(self):
        for profile in ("asia_adaptive", "london_adaptive", "us_adaptive"):
            for side in ("buy", "sell"):
                with self.subTest(profile=profile, side=side):
                    self.run_chain(profile, side)

    def test_primary_timeout_refreshes_backup_ledger_schema_and_citations(self):
        self.run_chain(failover=True)

    def test_invalid_ledger_stops_before_ai2_without_paid_regeneration(self):
        self.run_chain(invalid=True)

    def test_valid_watch_preserves_action_and_never_calls_ai2(self):
        self.run_chain(watch=True)


if __name__ == "__main__":
    unittest.main()
