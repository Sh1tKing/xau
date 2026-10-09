"""Protective stop precision regressions; no terminal, network, or orders."""
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai

base = ai.base


def m5_frame(extreme, side="buy"):
    rows = [{"time": 1000 + i * 300, "open": extreme + 2,
             "high": extreme + 6, "low": extreme, "close": extreme + 4,
             "tick_volume": 100} for i in range(8)]
    if side == "sell":
        rows = [{**r, **{key: 8400 - r[key] for key in ("open", "close")},
                 "high": 8400 - r["low"], "low": 8400 - r["high"]} for r in rows]
    return base.pd.DataFrame(rows)


class StopPriceRoundingTests(unittest.TestCase):
    def setUp(self):
        for scope in (patch.object(base, "MAX_SL_ABS", 8.),
                      patch.object(base, "ENTRY_MIN_SL_ABS", .8),
                      patch.object(base, "ENTRY_MIN_SL_ATR", .6),
                      patch.object(base, "MIN_REWARD_RISK", 1.5)):
            scope.start()
            self.addCleanup(scope.stop)

    def test_archived_stops_and_sell_mirrors_cover_the_unrounded_buffer(self):
        cases = [(4182.39, 4178.93, 6.417142857142933, 4177.96),
                 (4181.87, 4176.97, 5.55, 4176.13),
                 (4187.06, 4181.77, 5.088571428571478, 4181.00)]
        for entry, extreme, atr, expected in cases:
            for side in ("buy", "sell"):
                ref, anchor, stop = ((entry, extreme, expected) if side == "buy"
                                     else (8400-entry, 8400-extreme, 8400-expected))
                with self.subTest(side=side, ref=ref):
                    sl, tp = base.calculate_targets(side, ref, atr, None, anchor, anchor)
                    self.assertAlmostEqual(sl, stop)
                    self.assertTrue(base.stop_covers_m5_extreme(side, sl, anchor, atr))
                    self.assertLessEqual(base.directional_stop_distance(side, ref, sl), 8)
                    self.assertGreaterEqual(base.reward_risk_ratio(side, ref, sl, tp), 1.5)

    def test_decimal_grid_and_half_cent_are_quantized_outward(self):
        for side, raw, expected in (("buy", 4176.1375, 4176.13),
                                    ("sell", 4223.8625, 4223.87),
                                    ("buy", 100.005, 100.00),
                                    ("sell", 100.005, 100.01),
                                    ("buy", 100.01, 100.01),
                                    ("sell", 100.01, 100.01)):
            self.assertEqual(base.round_protective_stop(side, raw), expected)

    def test_inside_buffer_stop_is_replaced_without_false_over_cap_reason(self):
        for side, ref, sl, tp in (("buy", 4181.87, 4176.14, 4192.20),
                                  ("sell", 4218.13, 4223.86, 4207.80)):
            with self.subTest(side=side):
                frame = m5_frame(4176.97, side)
                fixed_sl, fixed_tp, note = base.reject_inside_m5_wick(side, ref, sl, tp, 5.55, None, frame)
                extreme = base.recent_m5_extreme(frame, side)
                self.assertTrue(base.stop_covers_m5_extreme(side, fixed_sl, extreme, 5.55))
                self.assertGreater(fixed_tp, 0)
                self.assertNotIn("超过", note)

    def test_true_over_cap_stops_remain_rejected(self):
        for side, ref, extreme in (("buy", 100., 91.), ("sell", 100., 109.)):
            self.assertEqual(base.calculate_targets(side, ref, 5.55, None, extreme, extreme), (0., 0.))

    def test_outward_quantization_cannot_expand_the_eight_dollar_cap(self):
        # Raw risk fits; the nearest safe two-decimal price would exceed the cap.
        for side, ref, extreme in (("buy", 100.004, 92.838),
                                   ("sell", 199.996, 207.162)):
            self.assertEqual(base.calculate_targets(side, ref, 5.55, None, extreme, extreme), (0., 0.))
        frame = m5_frame(92.838)
        _, _, note = base.reject_inside_m5_wick("buy", 100.004, 92.01, 114., 5.55, None, frame)
        self.assertIn("量化后", note)
        self.assertIn("8.00", note)

    def test_generation_failure_with_small_risk_is_not_reported_as_over_cap(self):
        with patch.object(base, "calculate_targets", return_value=(0., 0.)):
            sl, tp, note = base.reject_inside_m5_wick("buy", 4181.87, 4176.14, 4192.2,
                                                    5.55, None, m5_frame(4176.97))
        self.assertEqual((sl, tp), (0., 0.))
        self.assertIn("无法", note)
        self.assertNotIn("超过", note)

    def test_ai_clamp_preserves_the_same_safe_stop(self):
        for side, ref, sl, tp, expected in (("buy", 4181.87, 4176.1375, 4192.2, 4176.13),
                                            ("sell", 4218.13, 4223.8625, 4207.8, 4223.87)):
            result = ai.clamp_proposal(side, ref, sl, tp, 5.55)
            self.assertAlmostEqual(result[0], expected)

    def test_ai_evidence_uses_an_executable_outward_stop(self):
        local = base.StrategyResult("no", 0, False, "test", 0., 0., 0, 0, {"setup": "pullback"})
        for side, direction in (("buy", "bull"), ("sell", "bear")):
            frame = m5_frame(4176.97, side)
            m15 = base.pd.concat([frame] * 4, ignore_index=True)
            with patch.object(base, "trend_from_ema", return_value=direction):
                facts = ai.build_computed_entry_evidence(m15, m15, frame, local, None, 5.55,
                                                        4181.87 if side == "buy" else 4218.13)
            timing = facts["m5_timing"][side]
            self.assertTrue(base.stop_covers_m5_extreme(side, timing["protected_stop"],
                                                      timing["extreme"], 5.55))
            self.assertEqual(timing["risk_from_reference"], 5.74)

    def test_ema_chase_is_still_the_rejection_after_stop_rounding_is_fixed(self):
        payload = {"action": "buy", "confidence": 80, "stop_loss": 4176.1375,
                   "take_profit": 4192.2, "reasoning": "valid trigger",
                   "trigger_evidence": "M5 confirmation", "invalidation": "protected low lost",
                   "target_basis": "independent structural target", "fact_conflicts": []}
        with patch.object(base, "chase_blocks_at", return_value=(True, "EMA21过远(2.43ATR)，不追多")):
            proposal, reason = ai.finalize_ai_proposal(payload, 4181.87, 5.55, "bull", "bull", None,
                                                      df_m15=base.pd.DataFrame(),
                                                      df_m5=m5_frame(4176.97))
        self.assertIsNone(proposal)
        self.assertIn("EMA21", reason)
        self.assertNotIn("超过小资金", reason)


if __name__ == "__main__":
    unittest.main()
