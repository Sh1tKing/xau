"""Offline checks for research source closure and lookahead boundaries."""
import unittest

import pandas as pd
from crypto_market_probe import aggregate, analyze


def minutes(count=3000, scale=1):
    result = []
    for index in range(count):
        opened = (1000 + .003*index + (index % 30)*.03)*scale
        result.append([index*60, opened, opened+.35*scale, opened-.25*scale,
                       opened+.1*scale, 10.0])
    return pd.DataFrame(result, columns=["time", "open", "high", "low", "close", "volume"])


class ProbeTests(unittest.TestCase):
    def test_partial_and_gapped_buckets_are_not_closed_bars(self):
        source = minutes(11)
        bars = aggregate(source, 300)
        self.assertEqual(list(bars.time), [0, 300])
        self.assertEqual(list(aggregate(source.drop(index=2), 300).time), [300])

    def test_invalid_duplicate_and_nonfinite_inputs_rejected(self):
        duplicate = pd.concat([minutes(5), minutes(5)])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            aggregate(duplicate, 300)
        invalid = minutes(5)
        invalid.loc[2, "high"] = float("nan")
        with self.assertRaisesRegex(ValueError, "OHLC"):
            aggregate(invalid, 300)

    def test_proportional_prices_do_not_change_normalized_feasibility(self):
        small, large = analyze(minutes(), "BTC"), analyze(minutes(scale=100), "BTC")
        for key in ("evaluated", "neutral_h1", "chase_rejected", "structure_over_cap", "structurally_feasible"):
            self.assertEqual(small["counts"][key], large["counts"][key])
        self.assertGreater(large["counts"]["structurally_feasible"], 0)
        self.assertAlmostEqual(small["median_m15_atr_pct"], large["median_m15_atr_pct"])

    def test_future_wick_does_not_change_already_evaluated_checkpoint(self):
        # Final full M5 close at t=180000: future one-minute observations after
        # the next quote are drift context only, never protective/source bars.
        source = minutes(3003)
        before = analyze(source, "BTC")
        changed = source.copy()
        changed.loc[3001:, "high"] += 1000
        after = analyze(changed, "BTC")
        for key in ("evaluated", "neutral_h1", "chase_rejected", "structure_over_cap", "structurally_feasible"):
            self.assertEqual(before["counts"][key], after["counts"][key])
        self.assertEqual(before["research_pattern_counts"], after["research_pattern_counts"])


if __name__ == "__main__":
    unittest.main()
