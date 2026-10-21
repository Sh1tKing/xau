"""Research classification must stay symmetric and use only saved closed facts."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1]/"docs/experiments/audit_entry_location.py"
SPEC = importlib.util.spec_from_file_location("entry_location_audit", PATH)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class EntryLocationAuditTests(unittest.TestCase):
    def pack(self, side="buy"):
        return {"live_quote": {"observed_at": 1800000000.},
                "m15_bars": [{"time": 100}],
                "m5_bars": [{"time": 100+i*300, "low": 90, "high": 120} for i in range(12)],
                "computed_evidence": {"obstacles": {side: [
                    {"price": 106, "source": "recent_m15_extreme", "timeframe": "M15", "bar_time": 100,
                     "classification": "unknown", "closed_breakthrough": {"status": "not_observed_in_window"}}]}}}

    def test_distances_use_risk_without_promoting_unknown_to_strong(self):
        result = audit.location_facts(self.pack(), "buy", 100, 96)
        self.assertEqual(result["nearest_m15_r"], 1.5)
        self.assertEqual(result["unbroken_references"][0]["classification"], "unknown")
        self.assertEqual(result["obstacle_coverage"], "saved_references_only")

    def test_buy_and_sell_are_price_mirrors(self):
        buy = audit.location_facts(self.pack(), "buy", 100, 96)
        pack = self.pack("sell")
        pack["computed_evidence"]["obstacles"]["sell"][0]["price"] = 94
        for row in pack["m5_bars"]:
            row["low"], row["high"] = 80, 110
        sell = audit.location_facts(pack, "sell", 100, 104)
        self.assertEqual(buy["nearest_m15_r"], sell["nearest_m15_r"])
        self.assertAlmostEqual(buy["m5_12_location"], 1-sell["m5_12_location"])

    def test_already_broken_or_unknown_status_is_not_an_unbroken_level(self):
        for status in ("observed", "unknown", None):
            pack = self.pack()
            pack["computed_evidence"]["obstacles"]["buy"][0]["closed_breakthrough"]["status"] = status
            self.assertIsNone(audit.location_facts(pack, "buy", 100, 96)["nearest_m15_r"])

    def test_future_or_unsaved_source_does_not_enter_research(self):
        pack = self.pack()
        pack["computed_evidence"]["obstacles"]["buy"][0]["bar_time"] = 200
        with self.assertRaises(ValueError):
            audit.location_facts(pack, "buy", 100, 96)

    def test_saved_newer_m5_is_not_misclassified_by_last_m15_timestamp(self):
        pack = self.pack()
        row = pack["computed_evidence"]["obstacles"]["buy"][0]
        row.update(timeframe="M5", bar_time=3400, source="m5_process_extreme")
        facts = audit.location_facts(pack, "buy", 100, 96)
        self.assertEqual(facts["nearest_any_r"], 1.5)
        self.assertIsNone(facts["nearest_m15_r"])

    def test_missing_obstacle_facts_are_unavailable_and_input_is_unchanged(self):
        pack = self.pack()
        original = deepcopy(pack)
        audit.location_facts(pack, "buy", 100, 96)
        self.assertEqual(pack, original)
        del pack["computed_evidence"]["obstacles"]
        with self.assertRaises(ValueError):
            audit.location_facts(pack, "buy", 100, 96)
