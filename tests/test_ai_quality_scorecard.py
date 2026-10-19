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
import xauusd_fib_volume_pa_ai3 as ai


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


class QualityScorecardTests(unittest.TestCase):
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

    def test_actual_order_gate_still_blocks_live_rr_below_15(self):
        # No terminal: only exercise the existing pre-order arithmetic with stubs.
        trader = SimpleNamespace(current_position_count=lambda: 0)
        trader.validate_strategy_entry = lambda result: ""
        trader.entry_news_error = lambda since=None: ""  # Isolate live RR arithmetic; dated news gates have their own suite.
        trader.validate_order_prices = lambda *args: ai.base.FibVolumePATrader.validate_order_prices(trader, *args)
        result = ai.base.StrategyResult("sell", 96, False, "quality passed", 4125.07, 4110.15, 0, 0, {})
        trader.validate_execution_prices = lambda result, tick: ""
        with patch.object(ai.base.mt5, "symbol_info_tick", return_value=SimpleNamespace(bid=4118.34, ask=4118.67)), \
             patch.object(ai.base.mt5, "order_send") as order_send:
            self.assertFalse(ai.base.FibVolumePATrader.place_order(trader, result))
            order_send.assert_not_called()

    def test_entry_task_applies_quality_before_finalizing(self):
        frame = ai.base.pd.DataFrame([dict(time=1800000000+i*900, open=100+i*.1,
                                          high=100.5+i*.1, low=99.5+i*.1, close=100.1+i*.1,
                                          tick_volume=100) for i in range(80)])
        local = ai.base.StrategyResult("buy", 60, False, "local candidate", 106, 113, 60, 0, {"setup": "pullback"})
        def response(*args, **kwargs):
            from test_ai_citations import cite
            _, payload = sample("buy")
            # Refer to an actual field shared by the real input pack.
            payload["quality_assessment"]["dimensions"]["trigger"]["evidence_refs"] = ["/h1_trend"]
            request=ai.json.loads(kwargs['refresh_request']()['prompt'].split('\n',1)[1])
            return ai.json.dumps(cite(request,payload))
        with tempfile.TemporaryDirectory() as folder, patch.object(ai, "AI3_STATE_FILE", str(Path(folder)/"state.json")):
            trader = ai.ThreeAITrader()
            with patch.object(trader, "get_context", return_value=(frame, frame, frame)), \
                 patch.object(ai.base, "build_fib_volume_pa_decision", return_value=local), \
                 patch.object(ai.base.mt5, "symbol_info_tick", return_value=SimpleNamespace(bid=108, ask=108.1, time=1800009000)), \
                 patch.object(trader, "entry_is_current", return_value=True), \
                 patch.object(ai, "call_chat", side_effect=response), \
                 patch.object(ai, "finalize_ai_proposal", return_value=(None, "test stop after finalization")) as finalize, \
                 patch.object(ai, "append_decision_snapshot") as snapshot, \
                 patch.object(trader, "notify_lines"), patch.object(trader, "release_task"):
                trader.entry_task(1, 1)
            self.assertEqual(finalize.call_count, 1)
            self.assertEqual(finalize.call_args.args[0]["confidence"], 80)
            result_snapshot = next(c.args[2] for c in snapshot.call_args_list if c.args[1] == "ai1_result")
            self.assertEqual(result_snapshot["quality_error"], "")

    def test_malformed_levels_are_reported_without_raising(self):
        pack, payload = sample()
        payload["quality_assessment"]["dimensions"]["trigger"]["level"] = []
        self.assertTrue(ai.normalize_ai1_quality(payload, pack)[1])


if __name__ == "__main__":
    unittest.main()
