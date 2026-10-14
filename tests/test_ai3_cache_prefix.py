"""Guardian request ordering preserves all evidence and refreshes live fields."""
import copy
import json
import logging
import unittest

logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai


def payload():
    return {
        "symbol": "XAUUSD", "snapshot_id": "first-snapshot",
        "live_quote": {"bid": 100., "ask": 100.3, "observed_at": 1000.},
        "bid": 100., "ask": 100.3,
        "m1_bars": [{"time": 900, "open": 100., "high": 101., "low": 99., "close": 100.}],
        "positions": [{
            "ticket": 123, "direction": "BUY", "volume": .01, "entry": 99.,
            "sl": 97., "tp": 105., "profit": 1., "initial_risk": 2.,
            "profit_guard": {"protected_sl": 99.2, "desired_sl": 99.4, "broker_sl": 99.2},
            "holding_review": {"policy": "ai_review", "review_due": False},
            "entry_context": {
                "status": "complete", "observed_mfe": 1., "last_observed_at": 1000.,
                "strategy_contract": {"id": "asia_adaptive", "max_hold_minutes": 90},
                "strategy_evidence": {
                    "buy": {"trigger": {"time": 850, "boundary": 98.}, "unknown": None},
                    "sell": {"opposing_structure": {"time": 850, "boundary": 103.}}},
                "entry_reasoning": "已收盘证据支持回收；反向高点仍为已知风险",
                "invalidation": "M15收盘破坏原边界", "coverage": "from_entry",
                "observed_mae": .3}}]}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def shared_prefix(left, right):
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return left[:index]
    return left[:min(len(left), len(right))]


class GuardianCachePrefixTests(unittest.TestCase):
    def test_all_values_and_both_directions_are_preserved_without_mutating_state(self):
        original = payload()
        before = copy.deepcopy(original)
        ordered = ai.cache_ordered_guardian_pack(original)
        self.assertEqual(json.loads(encoded(ordered)), json.loads(encoded(before)))
        self.assertEqual(original, before)
        self.assertEqual(len(encoded(ordered)), len(encoded(before)))
        ordered["positions"][0]["entry_context"]["strategy_evidence"]["sell"]["opposing_structure"]["boundary"] = 104.
        self.assertEqual(original, before)

    def test_consecutive_live_changes_share_the_full_original_thesis_prefix(self):
        first, second = payload(), payload()
        second.update(snapshot_id="second-snapshot", bid=102., ask=102.3,
                      live_quote={"bid": 102., "ask": 102.3, "observed_at": 1060.})
        position = second["positions"][0]
        position.update(profit=3., sl=100., holding_review={"policy": "ai_review", "review_due": True})
        position["profit_guard"].update(protected_sl=100., desired_sl=100.5, broker_sl=100.)
        position["entry_context"].update(last_observed_at=1060., observed_mfe=3., observed_mae=.4)
        second["m1_bars"][0].update(time=960, close=102.)
        left = encoded(ai.cache_ordered_guardian_pack(first))
        right = encoded(ai.cache_ordered_guardian_pack(second))
        common = shared_prefix(left, right)
        self.assertIn(encoded(first["positions"][0]["entry_context"]["entry_reasoning"]), common)
        self.assertIn('"strategy_evidence"', common)
        self.assertIn('"opposing_structure"', common)
        self.assertGreater(len(common), len(shared_prefix(encoded(first), encoded(second))))
        self.assertEqual(json.loads(right), second)

    def test_persisted_key_order_does_not_change_the_frozen_prefix(self):
        original = payload()
        restored = copy.deepcopy(original)
        restored["positions"][0]["entry_context"] = json.loads(json.dumps(
            original["positions"][0]["entry_context"], ensure_ascii=False, sort_keys=True))
        self.assertEqual(encoded(ai.cache_ordered_guardian_pack(original)),
                         encoded(ai.cache_ordered_guardian_pack(restored)))

    def test_changed_original_evidence_is_sent_and_breaks_the_previous_prefix(self):
        first, second = payload(), payload()
        second["positions"][0]["entry_context"]["invalidation"] = "H1收盘跌破原边界"
        left, right = map(lambda x: encoded(ai.cache_ordered_guardian_pack(x)), (first, second))
        self.assertNotEqual(left, right)
        self.assertEqual(json.loads(right), second)
        self.assertNotIn('"strategy_evidence"', shared_prefix(left, right))

    def test_unknown_or_missing_context_is_preserved_without_inventing_a_thesis(self):
        for context in (None, {"status": "unknown_after_restart", "initial_risk": None}, {}):
            original = payload()
            original["positions"][0]["entry_context"] = context
            self.assertEqual(json.loads(encoded(ai.cache_ordered_guardian_pack(original))), original)
        original = {"symbol": "XAUUSD", "positions": [], "snapshot_id": "empty"}
        self.assertEqual(ai.cache_ordered_guardian_pack(original), original)


if __name__ == "__main__":
    unittest.main()
