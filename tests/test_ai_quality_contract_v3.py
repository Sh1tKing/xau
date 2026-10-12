"""Real archived failure, scorecard/schema parity, and mocked primary/backup requests."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jsonschema import Draft202012Validator
import xauusd_fib_volume_pa_ai3 as ai
from strategies import quality
from test_ai_entry_gate_v2 import quality_sample
from test_ai_citations import facts_pool, cite, bindings
from test_ai_strategy_opportunities import payload as legacy_pool_payload
from test_ai_response_contract import Response


def current_card(side="buy", action=None):
    pack, raw = quality_sample(side)
    raw["opportunity_id"] = ""
    raw["quality_assessment"]["version"] = ai.QUALITY_VERSION
    for item in raw["quality_assessment"]["dimensions"].values():
        item["observations"] = []
    if action == "no":
        raw.update(action="no", stop_loss=None, take_profit=None)
        raw["wait_basis"] = [{"code": "target_space", "reason": "等待目标空间", "evidence_refs": ["/h1_trend"]}]
    return pack, raw


def issue(cause, event="bar-1", refs=None):
    return {"cause": cause, "event_group": event, "reason": "本次真实证据", "evidence_refs": refs or ["/h1_trend"]}


def v3_copy(raw):
    """Explicit test fixture authoring, never called by production code."""
    result = copy.deepcopy(raw)
    result["quality_assessment"]["version"] = ai.QUALITY_VERSION
    for dimension in result["quality_assessment"]["dimensions"].values():
        dimension["observations"] = []
    return result


class QualityContractV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = json.loads((Path(__file__).parent / "fixtures/ai1_quality_20261012_162312.json").read_text(encoding="utf-8"))
        cls.latest = json.loads((Path(__file__).parent / "fixtures/ai1_quality_20261012_173643.json").read_text(encoding="utf-8"))

    def assert_contract(self, pack, raw, valid, score=None):
        before = copy.deepcopy(raw)
        schema = ai.response_json_schema("AI-1", pack)
        Draft202012Validator.check_schema(schema)
        errors = list(Draft202012Validator(schema).iter_errors(raw))
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertEqual(not errors, valid, errors)
        self.assertEqual(not error, valid, error)
        self.assertEqual(raw, before)
        if valid:
            self.assertEqual(result["action"], raw["action"])
            self.assertEqual(result["quality_version"], ai.QUALITY_VERSION)
            if score is not None:
                self.assertEqual(result["confidence"], score)
        return result, error

    def test_archived_v2_misclassified_shallow_card_is_precise_and_not_repaired(self):
        raw, pack = self.archive["raw"], self.archive["request_pack"]
        before = copy.deepcopy(raw)
        result, error = ai.normalize_ai1_quality(raw, pack)
        self.assertIn("/structure/reservations/0", error)
        self.assertIn("cause=shallow_retrace level=weak", error)
        self.assertEqual(result["action"], "no")
        self.assertNotIn("quality_score", result)
        self.assertEqual(raw, before)
        self.assertEqual(ai.ai1_result_status(result, error, None), "contract_invalid")
        self.assert_contract(pack, v3_copy(raw), False)

    def test_archived_independent_live_chase_diagnostic_does_not_score_or_open(self):
        raw, pack = self.archive["raw"], self.archive["request_pack"]
        notes = ai.ai1_verified_risk_notes(raw, pack)
        self.assertEqual(len(notes), 1)
        self.assertIn("2.35ATR > 1.50ATR", notes[0])
        self.assertEqual(raw["action"], "no")
        facts = ai.plan_feasibility("buy", pack["live_quote"]["ask"], 4173.95, 4187.15,
                                    pack["live_quote"], pack["atr"], pack["m15_ema21"])
        self.assertFalse(facts["chase_passes"])
        self.assertFalse(facts["executable"])

    def test_valid_no_card_retains_declared_grades_and_real_archived_citations(self):
        pack = self.archive["request_pack"]
        raw = v3_copy(self.archive["raw"])
        # A new, explicitly authored fixture categorizes the actual EMA hard limit correctly.
        # The old response remains invalid; production never substitutes a cause.
        raw["quality_assessment"]["dimensions"]["structure"]["reservations"][0]["cause"] = "hard_limit"
        self.assert_contract(pack, raw, True, 48)

    def test_archived_v3_unconfirmed_opposition_retains_watch_grades_and_hard_limit(self):
        pack, raw = self.latest["request_pack"], self.latest["raw"]
        result, _ = self.assert_contract(pack, raw, True, 72)
        self.assertEqual(ai.ai1_result_status(result, "", None), "watch")
        self.assertEqual(result["quality_assessment"]["dimensions"]["structure"]["level"], "weak")
        self.assertEqual(result["quality_assessment"]["reservations"][0]["severity"], "material")
        observations = result["quality_assessment"]["dimensions"]["counter_evidence"]["observations"]
        self.assertEqual([item["cause"] for item in observations], ["opposing_structure", "volume"])
        self.assertIn("2.06ATR > 1.50ATR", ai.ai1_verified_risk_notes(raw, pack)[0])
        facts = ai.plan_feasibility("buy", pack["live_quote"]["ask"], 4171, 4184,
                                    pack["live_quote"], pack["atr"], pack["m15_ema21"])
        self.assertFalse(facts["chase_passes"])
        self.assertFalse(facts["executable"])

    def test_minor_observation_topics_do_not_imply_material_severity_buy_sell(self):
        # Causes describe topics; both mild and major judgments can concern these topics.
        for side in ("buy", "sell"):
            for cause in ("trigger_weakness", "opposing_structure", "stop_vulnerability",
                          "target_uncertainty", "execution_drift"):
                pack, raw = current_card(side)
                raw["quality_assessment"]["dimensions"]["counter_evidence"]["observations"] = [issue(cause)]
                self.assert_contract(pack, raw, True, 80)

    def test_minor_opposition_coexists_with_independent_material_reservation(self):
        for side in ("buy", "sell"):
            pack, raw = current_card(side)
            raw["quality_assessment"]["dimensions"]["counter_evidence"].update(
                level="weak", reservations=[issue("opposing_structure", "confirmed-structure")],
                observations=[issue("opposing_structure", "single-opposing-m5")])
            result, _ = self.assert_contract(pack, raw, True, 72)
            self.assertEqual(result["quality_assessment"]["reservations"][0]["severity"], "material")

    def test_minor_opposition_alone_cannot_support_material_wait(self):
        pack, raw = current_card(action="no")
        raw["quality_assessment"]["dimensions"]["counter_evidence"]["observations"] = [issue("opposing_structure")]
        raw["wait_basis"][0]["code"] = "material_counter_evidence"
        self.assertIn("仅有轻微", ai.normalize_ai1_quality(raw, pack)[1])

    def test_minor_opposition_cannot_replace_required_material_reason_or_repeat_same_event(self):
        for level in ("weak", "missing"):
            pack, raw = current_card(action="no")
            raw["quality_assessment"]["dimensions"]["counter_evidence"].update(
                level=level, observations=[issue("opposing_structure")])
            self.assert_contract(pack, raw, False)
        pack, raw = current_card()
        raw["quality_assessment"]["dimensions"]["counter_evidence"].update(
            reservations=[issue("opposing_structure")], observations=[issue("opposing_structure")])
        self.assertIn("重复扣分", ai.normalize_ai1_quality(raw, pack)[1])

    def test_mixed_material_reason_and_minor_observation_keep_weak_and_score_72(self):
        for side in ("buy", "sell"):
            pack, raw = current_card(side)
            dimension = raw["quality_assessment"]["dimensions"]["structure"]
            dimension.update(level="weak", reservations=[issue("opposing_structure")],
                             observations=[issue("volume", "bar-2")])
            result, _ = self.assert_contract(pack, raw, True, 72)
            self.assertEqual(result["quality_assessment"]["reservations"][0]["severity"], "material")
            self.assertEqual(result["quality_assessment"]["dimensions"]["structure"]["observations"], dimension["observations"])

    def test_adequate_minor_observations_have_no_extra_penalty_buy_sell(self):
        for side in ("buy", "sell"):
            for cause in ("volume", "shallow_retrace"):
                pack, raw = current_card(side)
                raw["quality_assessment"]["dimensions"]["counter_evidence"]["observations"] = [issue(cause)]
                self.assert_contract(pack, raw, True, 80)

    def test_strong_shape_has_no_reservation_or_observation_arrays(self):
        pack, raw = current_card()
        dimension = raw["quality_assessment"]["dimensions"]["structure"]
        dimension.update(level="strong")
        dimension.pop("reservations")
        dimension.pop("observations")
        self.assert_contract(pack, raw, True, 84)
        for key, value in (("reservations", []), ("observations", [issue("volume")])):
            bad = copy.deepcopy(raw)
            bad["quality_assessment"]["dimensions"]["structure"][key] = value
            self.assert_contract(pack, bad, False)

    def test_empty_weak_missing_and_minor_only_bad_grades_fail_in_both_layers(self):
        for level in ("weak", "missing"):
            pack, raw = current_card(action="no")
            dimension = raw["quality_assessment"]["dimensions"]["structure"]
            dimension.update(level=level, observations=[issue("volume")])
            self.assert_contract(pack, raw, False)
            for cause in ("volume", "shallow_retrace"):
                dimension.update(reservations=[issue(cause, "bar-2")])
                self.assert_contract(pack, raw, False)

    def test_hard_limits_and_missing_data_cannot_hide_in_observations(self):
        for cause in ("hard_limit", "missing_data"):
            pack, raw = current_card()
            raw["quality_assessment"]["dimensions"]["structure"]["observations"] = [issue(cause)]
            self.assert_contract(pack, raw, False)

    def test_all_grade_cause_combinations_match_schema_and_python(self):
        for level in ai.QUALITY_LEVEL_POINTS:
            for cause in ai.QUALITY_CAUSES:
                for container in ("reservations", "observations"):
                    pack, raw = current_card(action="no")
                    dimension = raw["quality_assessment"]["dimensions"]["structure"]
                    dimension["level"] = level
                    if level == "strong":
                        dimension.pop("reservations")
                        dimension.pop("observations")
                    elif level in ("weak", "missing"):
                        dimension["reservations"] = [issue("opposing_structure", "base-event")]
                    dimension[container] = [issue(cause)]
                    with self.subTest(level=level, cause=cause, container=container):
                        schema_valid = Draft202012Validator(ai.response_json_schema("AI-1", pack)).is_valid(raw)
                        self.assertEqual(schema_valid, not ai.normalize_ai1_quality(raw, pack)[1])

    def test_observations_require_nonempty_real_and_same_candidate_evidence(self):
        for cause in ("volume", "opposing_structure"):
            for ref in ("invented", "/unknown", []):
                pack, raw = current_card()
                value = issue(cause)
                value["evidence_refs"] = [] if ref == [] else [ref]
                raw["quality_assessment"]["dimensions"]["structure"]["observations"] = [value]
                self.assertTrue(ai.normalize_ai1_quality(raw, pack)[1])
            pack = ai.prepare_ai1_request_pack(facts_pool())
            raw = cite(pack, v3_copy(legacy_pool_payload()))
            foreign = next(k for k, v in bindings(pack).items() if v == "/opportunities/0/strategy_evidence/buy/evidence/volume")
            raw["quality_assessment"]["dimensions"]["structure"]["observations"] = [issue(cause, refs=[foreign])]
            self.assertIn("隔离拒绝", ai.normalize_ai1_quality(raw, pack)[1])

    def test_duplicates_in_observations_and_across_dimensions_remain_invalid(self):
        pack, raw = current_card()
        for name in ("structure", "counter_evidence"):
            raw["quality_assessment"]["dimensions"][name]["observations"] = [issue("volume")]
        self.assertIn("重复扣分", ai.normalize_ai1_quality(raw, pack)[1])

    def test_minor_observation_cannot_support_material_wait_or_blocking_trade(self):
        pack, raw = current_card(action="no")
        raw["quality_assessment"]["dimensions"]["structure"]["observations"] = [issue("volume")]
        raw["wait_basis"][0]["code"] = "material_counter_evidence"
        self.assertIn("仅有轻微", ai.normalize_ai1_quality(raw, pack)[1])
        pack, raw = current_card()
        raw["quality_assessment"]["dimensions"]["structure"].update(level="missing", reservations=[issue("hard_limit")])
        self.assertIn("阻断条件", ai.normalize_ai1_quality(raw, pack)[1])

    def test_previous_valid_v1_v2_cards_are_not_upgraded_or_changed(self):
        from test_ai_quality_scorecard import sample
        for pack, raw, version in ((*quality_sample(), ai.PREVIOUS_QUALITY_VERSION), (*sample(), ai.LEGACY_QUALITY_VERSION)):
            original = copy.deepcopy(raw)
            result, error = ai.normalize_ai1_quality(raw, pack)
            self.assertEqual(error, "")
            self.assertEqual(result["quality_version"], version)
            self.assertEqual(raw, original)

    def test_legacy_v1_cannot_bypass_minor_cause_rule_with_material_severity(self):
        from test_ai_quality_scorecard import sample, reserve
        pack, raw = sample()
        reserve(raw, "minor-mislabeled", "structure", severity="material", cause="shallow_retrace")
        raw["quality_assessment"]["dimensions"]["structure"]["level"] = "weak"
        self.assertIn("cause=shallow_retrace severity=material", ai.normalize_ai1_quality(raw, pack)[1])

    def test_schema_prompt_rubric_share_contract_and_fit_native_subset(self):
        rubric = ai.quality_rubric(True)
        self.assertEqual(rubric["version"], quality.VERSION)
        self.assertIn(quality.RESERVATIONS_POLICY, ai.AI1_INSTRUCTIONS)
        self.assertEqual(rubric["reservations_policy"], quality.RESERVATIONS_POLICY)
        self.assertEqual(rubric["cause_rules"], quality.CAUSE_RULES)
        rubric["cause_rules"]["volume"]["meaning"] = "mutated caller copy"
        self.assertNotEqual(rubric["cause_rules"], quality.CAUSE_RULES)
        self.assertEqual(ai.ai1_json_template()["quality_assessment"]["version"], quality.VERSION)
        schema = ai.response_json_schema("AI-1")
        encoded = json.dumps(schema)
        for unsupported in ('"if"', '"then"', '"maxItems"', '"minLength"'):
            self.assertNotIn(unsupported, encoded)
        self.assertLessEqual(encoded.count('"anyOf"'), 16)

    def test_real_request_schema_and_snapshot_refresh_on_primary_backup_without_paid_repair(self):
        packs = [ai.prepare_ai1_request_pack(facts_pool()), None]
        updated = facts_pool()
        updated["live_quote"]["ask"] += .01
        packs[1] = ai.prepare_ai1_request_pack(updated)
        raw = cite(packs[1], v3_copy(legacy_pool_payload()))
        snapshots = iter([{"prompt": json.dumps(pack), "response_schema": ai.response_json_schema("AI-1", pack)} for pack in packs])
        with tempfile.TemporaryDirectory() as folder, patch.object(ai, "AI_USAGE_DIR", folder), \
                patch.dict(ai._CHAT_OPTIONS, {}, clear=True), patch.dict(ai._CAP_BLOCKED_ROUTES, {}, clear=True), \
                patch.dict(ai.os.environ, {"AI1_BACKUP_URL": "https://backup.test/v1/messages", "AI1_BACKUP_KEY": "mock"}, clear=True), \
                patch.object(ai.time, "sleep"), patch.object(ai.requests, "post", side_effect=[ai.requests.ReadTimeout("slow"), Response(text=json.dumps(raw))]) as post:
            reply = ai.call_chat("AI-1", "claude-unit", "medium", ai.AI1_INSTRUCTIONS, "old prompt", 120,
                                 "https://primary.test/v1/messages", "mock", refresh_request=lambda: next(snapshots))
        self.assertEqual(post.call_count, 2)
        for call, pack in zip(post.call_args_list, packs):
            body = call.kwargs["json"]
            self.assertEqual(body["output_config"]["format"]["schema"], ai.response_json_schema("AI-1", pack))
            self.assertEqual(json.loads(body["messages"][0]["content"]), pack)
            self.assertEqual(body["max_tokens"], ai.AI_MAX_TOKENS["AI-1"])
        self.assert_contract(packs[1], json.loads(reply), True, 80)

    def run_adaptive_chain(self, profile="asia_adaptive", side="buy", failover=False, invalid=False, observation_cause="volume", watch=False):
        from test_adaptive_ai_integration import AdaptiveAITests
        fixture = AdaptiveAITests()
        fixture.setUp()
        try:
            fixture.configure(profile, side)
            original = fixture.proposal
            requests = []
            def proposal(request):
                pack = json.loads(request["messages"][0]["content"].split("\n", 1)[1])
                requests.append(pack)
                if failover and len(requests) == 1:
                    raise ai.requests.ReadTimeout("mocked primary timeout")
                raw = v3_copy(original(request))
                for dimension in raw["quality_assessment"]["dimensions"].values():
                    dimension["evidence_refs"] = ["/m5_bars/79"]
                raw["quality_assessment"]["dimensions"]["counter_evidence"]["observations"] = [
                    issue(observation_cause, "last-m5-observation", ["/m5_bars/79"])]
                if invalid:
                    raw["quality_assessment"]["dimensions"]["structure"].update(
                        level="weak", reservations=[issue("shallow_retrace", refs=["/m5_bars/79"])])
                if watch:
                    raw.update(action="no", stop_loss=None, take_profit=None,
                               wait_basis=[{"code": "target_space", "reason": "重要障碍前空间不足",
                                            "evidence_refs": ["/m5_bars/79"]}])
                raw = cite(pack, raw)
                self.assertEqual(Draft202012Validator(ai.response_json_schema("AI-1", pack)).is_valid(raw), not invalid)
                return raw
            with patch.object(fixture, "proposal", side_effect=proposal), \
                    patch.object(ai, "AI1_URL", "https://primary.test/v1/messages"), \
                    patch.object(ai, "AI2_URL", "https://review.test/v1/messages"), \
                    patch.object(ai, "backup_chat_config", return_value={"model": "backup-unit", "effort": "low",
                        "url": "https://backup.test/v1/messages", "key": "mock"}):
                post = fixture.run_entry(protocol="messages")
            result = next(s[2] for s in fixture.snapshots if s[1] == "ai1_result")
            if invalid:
                self.assertEqual(post.call_count, 1)
                self.assertEqual(fixture.sent, [])
                self.assertEqual(result["status"], "contract_invalid")
                self.assertIn("cause=shallow_retrace level=weak", result["quality_error"])
            elif watch:
                self.assertEqual(post.call_count, 1)
                self.assertEqual(fixture.sent, [])
                self.assertEqual(result["status"], "watch")
                self.assertEqual(result["parsed"]["action"], "no")
                self.assertEqual(result["parsed"]["quality_score"], 80)
            else:
                self.assertEqual(post.call_count, 3 if failover else 2)
                self.assertEqual(result["status"], "review_pending")
                self.assertEqual(result["quality_version"], ai.QUALITY_VERSION)
                self.assertEqual(len(fixture.sent), 1, fixture.snapshots)
                self.assertEqual(fixture.sent[0]["volume"], .01)
                saved = fixture.trader.entry_contexts["123"]
                self.assertEqual(saved["quality_version"], ai.QUALITY_VERSION)
                self.assertEqual(saved["quality_assessment"]["dimensions"]["counter_evidence"]["observations"][0]["cause"], observation_cause)
                fixture.trader.load_entry_contexts()
                self.assertEqual(fixture.trader.entry_contexts["123"], saved)
        finally:
            fixture.doCleanups()

    def test_v3_six_profile_direction_full_chains_save_original_scorecard(self):
        for profile in ("asia_adaptive", "london_adaptive", "us_adaptive"):
            for side in ("buy", "sell"):
                with self.subTest(profile=profile, side=side):
                    self.run_adaptive_chain(profile, side)

    def test_v3_backup_full_chain_uses_successful_request_ids(self):
        self.run_adaptive_chain(failover=True)

    def test_v3_minor_opposition_six_profile_direction_chains_save_and_restore(self):
        for profile in ("asia_adaptive", "london_adaptive", "us_adaptive"):
            for side in ("buy", "sell"):
                with self.subTest(profile=profile, side=side):
                    self.run_adaptive_chain(profile, side, observation_cause="opposing_structure")

    def test_v3_watch_with_minor_opposition_never_calls_ai2_or_opens(self):
        self.run_adaptive_chain(observation_cause="opposing_structure", watch=True)

    def test_v3_invalid_card_stops_actual_entry_chain_without_ai2_or_paid_retry(self):
        self.run_adaptive_chain(invalid=True)


if __name__ == "__main__":
    unittest.main()
