"""Regressions for archived entry failures; no HTTP, terminal, or trading I/O."""
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
import xauusd_fib_volume_pa_ai3 as ai


def quality_sample(action="buy"):
    pack = {"h1_trend": "bull" if action == "buy" else "bear",
            "local_plan_feasibility": {"executable": True},
            "computed_evidence": {"price_action": {"M15": {action: {"score": 20}},
                                                    "M5": {action: {"score": 0}}}}}
    payload = {"action": action, "stop_loss": 98 if action == "buy" else 102,
               "take_profit": 103.6 if action == "buy" else 96.4,
               "reasoning": "Closed trigger with structural protection and target",
               "trigger_evidence": "M15 closed confirmation", "invalidation": "M15 boundary lost",
               "target_basis": "Supplied structural target", "fact_conflicts": [], "unknowns": [],
               "wait_basis": [], "quality_assessment": {
                   "version": "entry-quality-v2", "direction": action,
                   "dimensions": {name: {"level": "adequate", "reason": "Supported facts",
                                         "evidence_refs": ["/h1_trend"], "reservations": []}
                                  for name in ai.QUALITY_DIMENSIONS}}}
    return pack, payload


def add_reservation(payload, dimension, cause="target_uncertainty", event="M15_1_high"):
    payload["quality_assessment"]["dimensions"][dimension]["reservations"].append({
        "cause": cause, "event_group": event, "reason": "Independent evidence-backed reservation",
        "evidence_refs": ["/h1_trend"]})


class EntryGateV2Tests(unittest.TestCase):
    def test_favorable_archived_quotes_pass_without_changing_sl_tp(self):
        # Five archived snapshots with adequate remaining protection; the close-stop
        # 00:56 case is separately required to stay blocked.
        cases = [
            ("sell", 4123.83, 4129.76, 4113.16, 4123.89, 4124.22, 9.88857142857153),
            ("sell", 4131.90, 4137.97, 4120.97, 4134.68, 4135.01, 10.11642857142864),
            ("buy", 4127.61, 4122.14, 4137.46, 4125.83, 4126.16, 9.12),
            ("sell", 4130.10, 4135.28, 4121.11, 4130.83, 4131.16, 8.632857142857185),
            ("sell", 4130.07, 4135.17, 4121.11, 4130.37, 4130.70, 8.49214285714288),
        ]
        for side, ref, sl, tp, bid, ask, atr in cases:
            with self.subTest(side=side, ref=ref):
                facts = ai.plan_feasibility(side, ref, sl, tp, {"bid": bid, "ask": ask}, atr,
                                            (bid + ask) / 2)
                self.assertTrue(facts["executable"], facts)
                self.assertEqual((facts["stop_loss"], facts["take_profit"]), (sl, tp))
                self.assertLess(facts["live_risk"], ai.base.entry_risk_limits(atr)[0])

    def test_archived_0056_near_stop_remains_blocked_even_with_huge_rr(self):
        facts = ai.plan_feasibility("sell", 4119.41, 4125.22, 4108.95,
                                    {"bid": 4123.88, "ask": 4124.21}, 9.6814285714287, 4124)
        self.assertGreater(facts["live_rr"], 11)
        self.assertFalse(facts["protection_space_passes"])
        self.assertFalse(facts["executable"])
        self.assertIn("保护空间", ai.execution_rejection(facts))

    def test_stop_side_spread_is_included_in_symmetric_entry_bounds(self):
        for side, sl, tp, lower, upper in (("buy", 96., 108., 97.3, 100.8),
                                           ("sell", 104., 92., 99.2, 102.7)):
            with self.subTest(side=side):
                facts = ai.plan_feasibility(side, 100., sl, tp, {"bid": 99.9, "ask": 100.}, 1., 100.)
                self.assertAlmostEqual(facts["entry_bounds"]["lower"], lower)
                self.assertAlmostEqual(facts["entry_bounds"]["upper"], upper)
                self.assertEqual(facts["min_stop_clearance"], 1.2)
                self.assertEqual(facts["generation_min_risk"], .8)

    def test_old_instant_stop_and_buy_mirror_never_send_an_order(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(ai, "AI3_STATE_FILE", str(Path(folder)/"state.json")):
            trader = ai.ThreeAITrader()
            for side, sl, tp, bid, ask in (("sell", 4128.63, 4117.91, 4128.12, 4128.45),
                                          ("buy", 4128.12, 4138.84, 4128.30, 4128.63)):
                result = ai.base.StrategyResult(side, 80, False, "approved", sl, tp, 80, 80,
                                                {"execution_guard": {"atr": 2., "ema21": 4128.}})
                with patch.object(trader, "current_position_count", return_value=0), \
                     patch.object(ai.base, "ENABLE_TRADING", True), \
                     patch.object(ai.base.mt5, "symbol_info_tick", return_value=SimpleNamespace(bid=bid, ask=ask)), \
                     patch.object(ai.base.mt5, "order_send") as send:
                    self.assertFalse(ai.base.FibVolumePATrader.place_order(trader, result))
                send.assert_not_called()

    def test_rr_cap_chase_and_invalid_quotes_stay_blocked(self):
        for quote, atr, ema in (({"bid": 4127.50, "ask": 4127.83}, 8.79, 4127.),
                                 ({"bid": 4130., "ask": 4130.33}, 8.79, 4127.),
                                 ({"bid": 4125., "ask": 4125.33}, 1., 4100.),
                                 ({"bid": 4125.5, "ask": 4125.3}, 8.79, 4125.),
                                 ({"bid": float("nan"), "ask": 4125.3}, 8.79, 4125.)):
            self.assertFalse(ai.plan_feasibility("buy", 4125.3, 4120.03, 4134.54,
                                                 quote, atr, ema).get("executable", False))

    def test_historical_buy_finalization_still_cannot_round_rr_up_to_15(self):
        sl, tp = ai.clamp_proposal("buy", 4134.65, 4131.01, 4146.04, 6.717857142857221)
        facts = ai.plan_feasibility("buy", 4134.65, sl, tp, {"bid": 4134.81, "ask": 4135.14},
                                    6.717857142857221, 4127.652983931848)
        self.assertEqual((sl, tp), (4130.61, 4141.92))
        self.assertAlmostEqual(facts["live_rr"], 1.496689)
        self.assertFalse(facts["rr_passes"])
        self.assertFalse(facts["executable"])

    def test_new_schema_and_template_remove_duplicate_ownership_fields(self):
        card = ai.response_json_schema("AI-1")["properties"]["quality_assessment"]
        self.assertEqual(card["properties"]["version"]["enum"], [ai.QUALITY_VERSION])
        self.assertNotIn("reservations", card["properties"])
        dimension = ai.response_json_schema("AI-1")["$defs"]["quality_dimension"]
        self.assertEqual(set(dimension["properties"]), {"level", "reason", "evidence_refs"})
        issue = card["properties"]["issues"]["items"]["anyOf"][0]["properties"]
        self.assertEqual(set(issue), {"dimension", "kind", "cause", "event_group", "reason", "evidence_refs"})
        self.assertEqual(ai.ai1_json_template()["quality_assessment"]["version"], ai.QUALITY_VERSION)

    def test_new_cards_score_symmetrically_without_mutating_model_evidence(self):
        for side in ("buy", "sell"):
            pack, raw = quality_sample(side)
            add_reservation(raw, "target")
            original = copy.deepcopy(raw)
            normalized, error = ai.normalize_ai1_quality(raw, pack)
            self.assertFalse(error, error)
            self.assertEqual(raw, original)
            self.assertEqual(normalized["confidence"], 80)
            self.assertEqual(normalized["quality_version"], "entry-quality-v2")
            issues = normalized["quality_assessment"]["reservations"]
            self.assertEqual([(i["dimension"], i["severity"]) for i in issues], [("target", "minor")])
            proposal, reason = ai.finalize_ai_proposal(normalized, 100., 1., pack["h1_trend"], pack["h1_trend"], None,
                                                       min_confidence=75)
            self.assertIsNotNone(proposal, reason)

    def test_material_and_blocking_follow_explicit_grade_contract(self):
        pack, raw = quality_sample()
        raw["quality_assessment"]["dimensions"]["target"]["level"] = "weak"
        add_reservation(raw, "target")
        normalized, error = ai.normalize_ai1_quality(raw, pack)
        self.assertFalse(error, error)
        self.assertEqual(normalized["confidence"], 72)
        self.assertEqual(normalized["quality_assessment"]["reservations"][0]["severity"], "material")
        raw["quality_assessment"]["dimensions"]["target"]["level"] = "missing"
        self.assertIn("阻断", ai.normalize_ai1_quality(raw, pack)[1])
        raw["action"] = "no"
        raw["wait_basis"] = [{"code": "target_space", "reason": "RR unavailable",
                              "evidence_refs": ["/local_plan_feasibility"]}]
        normalized, error = ai.normalize_ai1_quality(raw, pack)
        self.assertFalse(error, error)
        self.assertEqual(normalized["action"], "no")
        self.assertEqual(normalized["confidence"], 64)

    def test_duplicate_event_cause_and_misleading_minor_stay_invalid(self):
        pack, raw = quality_sample()
        add_reservation(raw, "target")
        add_reservation(raw, "counter_evidence")
        self.assertIn("重复", ai.normalize_ai1_quality(raw, pack)[1])
        pack, raw = quality_sample()
        add_reservation(raw, "structure", cause="shallow_retrace")
        raw["quality_assessment"]["dimensions"]["structure"]["level"] = "weak"
        self.assertIn("轻微", ai.normalize_ai1_quality(raw, pack)[1])

    def test_invalid_refs_and_existing_trigger_cannot_be_guessed_away(self):
        pack, raw = quality_sample()
        raw["quality_assessment"]["dimensions"]["target"]["evidence_refs"] = [
            "/computed_evidence/local_plan_feasibility"]
        error = ai.normalize_ai1_quality(raw, pack)[1]
        self.assertIn("/computed_evidence/local_plan_feasibility", error)
        pack, raw = quality_sample()
        raw["action"] = "no"
        add_reservation(raw, "trigger", cause="missing_data")
        raw["quality_assessment"]["dimensions"]["trigger"]["level"] = "missing"
        raw["wait_basis"] = [{"code": "missing_trigger", "reason": "M5 is zero",
                              "evidence_refs": ["/h1_trend"]}]
        self.assertIn("已有触发", ai.normalize_ai1_quality(raw, pack)[1])

    def test_grade_missing_reservation_and_unknown_version_fail_closed(self):
        pack, raw = quality_sample()
        raw["quality_assessment"]["dimensions"]["target"]["level"] = "weak"
        self.assertTrue(ai.normalize_ai1_quality(raw, pack)[1])
        raw["quality_assessment"]["version"] = "entry-quality-v999"
        self.assertIn("版本", ai.normalize_ai1_quality(raw, pack)[1])

    def test_malformed_reservations_return_errors_without_raising(self):
        for field in ("cause", "event_group", "reason", "evidence_refs"):
            pack, raw = quality_sample()
            add_reservation(raw, "target")
            raw["quality_assessment"]["dimensions"]["target"]["reservations"][0][field] = []
            self.assertTrue(ai.normalize_ai1_quality(raw, pack)[1])

    def test_json_pointer_array_indices_follow_json_pointer_not_python_indexing(self):
        pack = {"bars": [{"close": 100}]}
        self.assertTrue(ai._evidence_pointer_exists(pack, "/bars/0/close"))
        for ref in ("/bars/-1/close", "/bars/+0/close", "/bars/00/close"):
            self.assertFalse(ai._evidence_pointer_exists(pack, ref))

    def test_quality_failure_is_not_reported_as_normal_watch(self):
        self.assertEqual(ai.ai1_result_status({"action": "no"}, "bad card", None), "contract_invalid")
        self.assertEqual(ai.ai1_result_status({"action": "no"}, "", None), "watch")
        self.assertEqual(ai.ai1_result_status({"action": "buy"}, "", None), "python_rejected")
        self.assertEqual(ai.ai1_result_status({"action": "buy"}, "", object()), "review_pending")

    def test_actual_bad_card_pipeline_stops_once_and_records_contract_failure(self):
        frame = ai.base.pd.DataFrame([dict(time=1800000000+i*900, open=100+i*.1,
                                          high=100.5+i*.1, low=99.5+i*.1, close=100.1+i*.1,
                                          tick_volume=100) for i in range(80)])
        local = ai.base.StrategyResult("no", 43, False, "local below threshold", 0., 0., 43, 0,
                                       {"setup": "pullback"})
        _, raw = quality_sample()
        add_reservation(raw, "target")
        add_reservation(raw, "counter_evidence")
        with tempfile.TemporaryDirectory() as folder, patch.object(ai, "AI3_STATE_FILE", str(Path(folder)/"state.json")):
            trader = ai.ThreeAITrader()
            with patch.object(trader, "get_context", return_value=(frame, frame, frame)), \
                 patch.object(ai.base, "build_fib_volume_pa_decision", return_value=local), \
                 patch.object(ai.base.mt5, "symbol_info_tick", return_value=SimpleNamespace(bid=108.3, ask=108.4, time=1)), \
                 patch.object(trader, "entry_is_current", return_value=True), \
                 patch.object(ai, "call_chat", return_value=ai.json.dumps(raw)) as chat, \
                 patch.object(ai, "finalize_ai_proposal") as finalize, \
                 patch.object(ai, "append_decision_snapshot") as snapshot, \
                 patch.object(trader, "place_order") as place, \
                 patch.object(trader, "notify_lines") as notify, patch.object(trader, "release_task"):
                trader.entry_task(123, 1)
            self.assertEqual(chat.call_count, 1)
            finalize.assert_not_called()
            place.assert_not_called()
            result = next(c.args[2] for c in snapshot.call_args_list if c.args[1] == "ai1_result")
            self.assertEqual(result["status"], "contract_invalid")
            self.assertEqual(ai.json.loads(result["raw"]), raw)
            self.assertIn("契约错误", notify.call_args.args[0])
            self.assertIn("输出无效", notify.call_args.args[1][0])


if __name__ == "__main__":
    unittest.main()
