"""Regression tests for auditable AI quality, separate from market profitability."""
import copy
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)
from crypto_demo.tests import bootstrap
from crypto_demo import crypto_ai3 as ai


def sample(direction="sell"):
    pack = {
        "h1_trend": "bear" if direction == "sell" else "bull",
        "m15_ema21_distance_atr": -.66 if direction == "sell" else .66,
        "local_plan_feasibility": {"available": True, "live_rr": 1.74},
        "computed_evidence": {"fib": {"reference_in_core": True}, "price_action": {
            "M15": {direction: {"score": 20, "checks": {"false_break": True}}},
            "M5": {direction: {"score": 0}}}},
    }
    payload = {
        "action": direction, "confidence": 60,
        "stop_loss": 101.5 if direction == "sell" else 98.5,
        "take_profit": 97.3 if direction == "sell" else 102.7,
        "trigger_evidence": "M15 closed false-break confirmed",
        "invalidation": "Named trigger extreme lost on closed bar",
        "target_basis": "Existing structural target before resistance",
        "fact_conflicts": [], "unknowns": [], "wait_basis": [],
        "quality_assessment": {"direction": direction, "dimensions": {
            name: {"level": "adequate", "reason": "Supported by supplied facts",
                   "evidence_refs": ["/h1_trend"], "reservation_ids": []}
            for name in ai.QUALITY_DIMENSIONS}, "reservations": []},
    }
    payload["quality_assessment"]["dimensions"]["trigger"]["evidence_refs"] = [
        "/computed_evidence/price_action/M15/%s/checks/false_break" % direction]
    return pack, payload


def reserve(payload, identifier, dimension, severity="minor", cause="volume", event="event1"):
    card = payload["quality_assessment"]
    card["reservations"].append({"id": identifier, "cause": cause, "event_group": event,
                                "dimension": dimension, "severity": severity,
                                "reason": "Evidence-backed reservation", "evidence_refs": ["/h1_trend"]})
    card["dimensions"][dimension]["reservation_ids"].append(identifier)


def pooled_sample(direction="sell"):
    pack, payload = sample(direction)
    pack["opportunities"] = [
        {"opportunity_id": "selected", "action": direction,
         "strategy_evidence": {direction: {"evidence": {"closed_trigger": {"confirmed": True}}}}},
        {"opportunity_id": "other", "action": direction},
    ]
    pack["rules"] = {"min_rr": 1.5, "price_finalization": {
        "generation_min_risk": .6, "execution_min_stop_clearance": .3, "target_max_reference_r": 2.5}}
    payload["opportunity_id"] = "selected"
    payload["quality_assessment"]["version"] = ai.QUALITY_VERSION
    payload["quality_assessment"].pop("reservations")
    for item in payload["quality_assessment"]["dimensions"].values():
        item.pop("reservation_ids")
        item["reservations"] = []
    return pack, payload


class QualityScorecardTests(unittest.TestCase):
    def setUp(self):
        from crypto_demo.tests.test_crypto_risk import specification
        scope = patch.object(ai.base.mt5, "symbol_info", return_value=specification(trade_tick_size=.01))
        scope.start()
        self.addCleanup(scope.stop)

    def test_quality_is_sum_and_not_free_confidence_buy_sell(self):
        for direction in ("buy", "sell"):
            pack, payload = sample(direction)
            reserve(payload, "volume", "counter_evidence")
            normalized, error = ai.normalize_ai1_quality(payload, pack)
            self.assertFalse(error)
            self.assertEqual(normalized["confidence"], 80)
            self.assertEqual(normalized["model_reported_confidence"], 60)
            self.assertEqual(payload["confidence"], 60)  # keep raw model evidence intact
            proposal, reason = ai.finalize_ai_proposal(normalized, 100, 1, pack["h1_trend"], pack["h1_trend"], None, min_confidence=75)
            self.assertIsNotNone(proposal, reason)
            self.assertEqual(proposal.details["quality_version"], ai.LEGACY_QUALITY_VERSION)

    def test_minor_reservations_cannot_be_scored_weak(self):
        pack, payload = sample()
        reserve(payload, "volume", "trigger")
        payload["quality_assessment"]["dimensions"]["trigger"]["level"] = "weak"
        _, error = ai.normalize_ai1_quality(payload, pack)
        self.assertIn("程度不符", error)

    def test_material_counter_evidence_can_still_fail_75(self):
        pack, payload = sample()
        reserve(payload, "opposition", "counter_evidence", "material", "opposing_structure")
        payload["quality_assessment"]["dimensions"]["counter_evidence"]["level"] = "weak"
        normalized, error = ai.normalize_ai1_quality(payload, pack)
        self.assertFalse(error)
        self.assertEqual(normalized["confidence"], 72)
        proposal, reason = ai.finalize_ai_proposal(normalized, 100, 1, "bear", "bear", None, min_confidence=75)
        self.assertIsNone(proposal)
        self.assertIn("72 < 75", reason)

    def test_duplicate_event_cause_cannot_charge_two_dimensions(self):
        pack, payload = sample()
        reserve(payload, "volume1", "trigger")
        reserve(payload, "volume2", "counter_evidence")
        _, error = ai.normalize_ai1_quality(payload, pack)
        self.assertIn("重复扣分", error)

    def test_false_missing_trigger_is_rejected(self):
        pack, payload = sample()
        payload["action"] = "no"
        payload["wait_basis"] = [{"code": "missing_trigger", "reason": "M5 zero so no trigger",
                                   "evidence_refs": ["/computed_evidence/price_action/M5/sell/score"]}]
        _, error = ai.normalize_ai1_quality(payload, pack)
        self.assertIn("已命中规则", error)

    def test_legitimate_hard_limit_wait_remains_no(self):
        pack, payload = sample()
        pack["m15_ema21_distance_atr"] = -2.33
        payload["action"] = "no"
        reserve(payload, "chase", "structure", "blocking", "hard_limit")
        payload["quality_assessment"]["dimensions"]["structure"]["level"] = "missing"
        payload["wait_basis"] = [{"code": "hard_limit", "reason": "Chase exceeds1.5ATR",
                                   "evidence_refs": ["/m15_ema21_distance_atr"]}]
        normalized, error = ai.normalize_ai1_quality(payload, pack)
        self.assertFalse(error)
        self.assertEqual(normalized["action"], "no")
        self.assertIsNone(ai.finalize_ai_proposal(normalized, 100, 1, "bear", "bear", None)[0])

    def test_missing_card_or_invented_reference_has_no_fallback(self):
        pack, payload = sample()
        bad = copy.deepcopy(payload)
        bad.pop("quality_assessment")
        self.assertTrue(ai.normalize_ai1_quality(bad, pack)[1])
        payload["quality_assessment"]["dimensions"]["target"]["evidence_refs"] = ["/nonexistent"]
        self.assertTrue(ai.normalize_ai1_quality(payload, pack)[1])

    def test_quality_cannot_bypass_stop_cap(self):
        for direction in ("buy", "sell"):
            pack, payload = sample(direction)
            payload["stop_loss"] = 109 if direction == "sell" else 91
            normalized, error = ai.normalize_ai1_quality(payload, pack)
            self.assertFalse(error)
            proposal, reason = ai.finalize_ai_proposal(normalized, 100, 1, pack["h1_trend"], pack["h1_trend"], None)
            self.assertIsNone(proposal)
            self.assertIn("上限", reason)

    def test_quote_rr_drift_is_not_a_reference_fact_conflict(self):
        sell = ai.plan_feasibility("sell", 4119.74, 4125.19, 4109.93, {"bid": 4119.62, "ask": 4119.95, "time": 1})
        later = ai.plan_feasibility("sell", 4119.74, 4125.07, 4110.15, {"bid": 4118.34, "ask": 4118.67, "time": 2})
        self.assertAlmostEqual(sell["live_rr"], 1.74, places=2)
        self.assertTrue(sell["rr_passes"])
        self.assertFalse(later["rr_passes"])
        buy = ai.plan_feasibility("buy", 100, 98, 103, {"bid": 100, "ask": 100.2, "time": 3})
        self.assertEqual(buy["live_entry"], 100.2)



    def test_malformed_levels_are_reported_without_raising(self):
        pack, payload = sample()
        payload["quality_assessment"]["dimensions"]["trigger"]["level"] = []
        self.assertTrue(ai.normalize_ai1_quality(payload, pack)[1])

    def test_pool_accepts_shared_ema_and_execution_clearance_without_changing_action(self):
        for direction in ("buy", "sell"):
            for action in (direction, "no"):
                with self.subTest(direction=direction, action=action):
                    pack, payload = pooled_sample(direction)
                    payload["action"] = action
                    payload["quality_assessment"]["dimensions"]["structure"]["evidence_refs"] = [
                        "/m15_ema21_distance_atr"]
                    payload["quality_assessment"]["dimensions"]["protection"]["evidence_refs"] = [
                        "/rules/price_finalization/execution_min_stop_clearance"]
                    if action == "no":
                        payload["wait_basis"] = [{"code": "hard_limit", "reason": "Current protective clearance is insufficient",
                            "evidence_refs": ["/rules/price_finalization/execution_min_stop_clearance"]}]
                    original = copy.deepcopy(payload)
                    normalized, error = ai.normalize_ai1_quality(payload, pack)
                    self.assertFalse(error, error)
                    self.assertEqual(normalized["action"], action)
                    self.assertEqual(normalized["quality_score"], 80)
                    self.assertEqual(payload, original)

    def test_pool_still_rejects_missing_shared_fact_and_unsafe_aliases(self):
        pointers = (
            "/rules", "/rules/price_finalization", "/rules/price_finalization/target_max_reference_r",
            "/local_plan_feasibility", "/computed_evidence/fib",
            "/opportunities/1/action", "/opportunities/0/strategy_evidence/buy",
            "/opportunities/0/host_verified_analysis_checkpoint",
            "/opportunities/0/local_plan/details/candidates/0/price",
        )
        for pointer in pointers:
            with self.subTest(pointer=pointer):
                pack, payload = pooled_sample()
                pack["local_plan_feasibility"] = {"available": True}
                candidate = pack["opportunities"][0]
                candidate["strategy_evidence"]["buy"] = {"score": 80}
                candidate["host_verified_analysis_checkpoint"] = {"verified": True}
                candidate["local_plan"] = {"details": {"candidates": [{"price": 100}]}}
                self.assertTrue(ai._evidence_pointer_exists(pack, pointer))
                payload["quality_assessment"]["dimensions"]["structure"]["evidence_refs"] = [pointer]
                self.assertIn("证据隔离拒绝", ai.normalize_ai1_quality(payload, pack)[1])
        for pointer in ("/m15_ema21_distance_atr", "/rules/price_finalization/execution_min_stop_clearance"):
            pack, payload = pooled_sample()
            if pointer.startswith("/rules/"):
                pack["rules"]["price_finalization"].pop("execution_min_stop_clearance")
            else:
                pack.pop("m15_ema21_distance_atr")
            payload["quality_assessment"]["dimensions"]["structure"]["evidence_refs"] = [pointer]
            self.assertIn("证据路径无效", ai.normalize_ai1_quality(payload, pack)[1])

    def test_pool_accepts_common_generation_floor_for_buy_sell_and_no(self):
        pointer = "/rules/price_finalization/generation_min_risk"
        for direction in ("buy", "sell"):
            for action in (direction, "no"):
                with self.subTest(direction=direction, action=action):
                    pack, payload = pooled_sample(direction)
                    payload["action"] = action
                    payload["quality_assessment"]["dimensions"]["protection"]["evidence_refs"] = [pointer]
                    if action == "no":
                        payload["wait_basis"] = [{"code": "hard_limit", "reason": "Protection cannot cover the required floor",
                                                 "evidence_refs": [pointer]}]
                    original = copy.deepcopy(payload)
                    normalized, error = ai.normalize_ai1_quality(payload, ai.compact_market_pack(pack))
                    self.assertFalse(error, error)
                    self.assertEqual(normalized["action"], action)
                    self.assertEqual(normalized["quality_score"], 80)
                    self.assertEqual(payload, original)

    def test_generation_floor_ref_requires_present_non_null_sent_fact(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                pack, payload = pooled_sample()
                finalization = pack["rules"]["price_finalization"]
                if missing:
                    finalization.pop("generation_min_risk")
                else:
                    finalization["generation_min_risk"] = None
                payload["quality_assessment"]["dimensions"]["protection"]["evidence_refs"] = [
                    "/rules/price_finalization/generation_min_risk"]
                self.assertIn("证据路径无效", ai.normalize_ai1_quality(payload, pack)[1])

    def test_pool_rubric_examples_match_sent_evidence_scope(self):
        pack, payload = pooled_sample()
        pack["quality_rubric"] = ai.quality_rubric(analysis_mode=True)
        sent = ai.compact_market_pack(pack)
        paths = sent["quality_rubric"]["reference_paths"]
        self.assertIn("/m15_ema21_distance_atr", paths)
        self.assertIn("/rules/price_finalization/execution_min_stop_clearance", paths)
        self.assertIn("/rules/price_finalization/generation_min_risk", paths)
        self.assertNotIn("/local_plan_feasibility", paths)
        self.assertNotIn("/computed_evidence/fib", paths)
        for pointer in paths:
            with self.subTest(pointer=pointer):
                payload["quality_assessment"]["dimensions"]["structure"]["evidence_refs"] = [pointer]
                self.assertFalse(ai.normalize_ai1_quality(payload, sent)[1])

    def test_pool_hides_winner_reference_and_target_but_keeps_selected_review_aliases(self):
        pack, payload = pooled_sample()
        finalization = pack["rules"]["price_finalization"]
        finalization["reference"] = "winner_closed_m15"
        pack["opportunities"][0]["strategy_contract"] = {"target_max_reference_r": 3.0}
        pack["opportunities"][0]["reference_price"] = {"price": 100.0, "source": "selected_closed_m5"}
        original = copy.deepcopy(pack)
        sent = ai.compact_market_pack(pack)
        self.assertNotIn("reference", sent["rules"]["price_finalization"])
        self.assertNotIn("target_max_reference_r", sent["rules"]["price_finalization"])
        self.assertEqual(sent["opportunities"][0]["strategy_contract"]["target_max_reference_r"], 3.0)
        self.assertEqual(sent["opportunities"][0]["reference_price"]["source"], "selected_closed_m5")
        self.assertEqual(sent["rules"]["price_finalization"]["generation_min_risk"], .6)
        self.assertEqual(pack, original)
        selected = copy.deepcopy(pack)
        selected["opportunity_id"] = "selected"
        selected["rules"]["price_finalization"].update(reference="selected_closed_m5", target_max_reference_r=3.0)
        review = ai.compact_market_pack(selected)
        self.assertEqual(review["rules"]["price_finalization"]["reference"], "selected_closed_m5")
        self.assertEqual(review["rules"]["price_finalization"]["target_max_reference_r"], 3.0)


if __name__ == "__main__":
    unittest.main()
