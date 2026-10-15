"""The real ledger shares one persisted attempt across directional proposals."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from strategies.adaptive import checkpoint_order_scope
from strategies.lifecycle import EventLedger, execution_token
from test_adaptive_profiles import context
from strategies.framework import DEFAULT_REGISTRY


class DirectionOrderScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.json"
        self.ledger = EventLedger(self.path)
        self.evidence = DEFAULT_REGISTRY.snapshot().get("asia_adaptive").assess(context()).evidence
        self.scope = checkpoint_order_scope(self.evidence)

    def metadata(self, event, side="buy", route="h1_trend", fingerprint="first"):
        return {"action": side, "symbol": "XAUUSD", "magic": 240611,
                "execution_token": execution_token(event, "XAUUSD", 240611, side),
                "analysis_order_scope": self.scope, "strategy_fingerprint": fingerprint,
                "direction_permission": {"route": route, "anchor": {"kind": "origin", "price": 99.}},
                "max_hold_minutes": 90, "cooldown_seconds": 60}

    def test_actual_checkpoint_identity_excludes_direction_route_anchor_and_fingerprint(self):
        original = self.evidence
        for side, route, fingerprint, price in (("sell", "m15_tactical", "changed", 105.),
                                                ("buy", "h1_recovery", "newer", 98.)):
            changed = deepcopy(original)
            changed["action"] = changed["analysis_checkpoint"]["action"] = side
            changed["strategy_fingerprint"] = fingerprint
            changed["direction_permission"] = {"route": route, "anchor": {"price": price}}
            changed["opportunity_id"] = "different-candidate"
            self.assertEqual(checkpoint_order_scope(changed), self.scope)
        for key, value in (("m5_bar_time", original["analysis_checkpoint"]["m5_bar_time"]+300),
                           ("m15_bar_time", original["analysis_checkpoint"]["m15_bar_time"]+900),
                           ("profile", "london_adaptive")):
            changed = deepcopy(original)
            changed["analysis_checkpoint"][key] = value
            self.assertNotEqual(checkpoint_order_scope(changed), self.scope)

    def test_uncertain_reservation_persists_across_restart_direction_route_and_build(self):
        # Reserving happens before broker POST. An uncertain or absent response
        # provides no authority to erase this attempt or try another candidate.
        self.assertTrue(self.ledger.reserve("first-buy", 100., self.metadata("first-buy")))
        initial = self.path.read_bytes()
        for ledger in (self.ledger, EventLedger(self.path)):
            self.assertFalse(ledger.error)
            self.assertIn("consumed", ledger.analysis_scope_error(self.scope, 101.))
            for side, route in (("sell", "m15_tactical"), ("buy", "h1_recovery")):
                event = "replacement-" + side + "-" + route
                self.assertFalse(ledger.reserve(event, 101., self.metadata(event, side, route, "changed")))
            self.assertEqual(self.path.read_bytes(), initial)
            self.assertEqual(len(ledger.snapshot()["events"]), 1)
        changed = deepcopy(self.evidence)
        changed["analysis_checkpoint"]["m5_bar_time"] += 300
        new_scope = checkpoint_order_scope(changed)
        meta = self.metadata("new-real-checkpoint", "sell")
        meta["analysis_order_scope"] = new_scope
        self.assertEqual(self.ledger.analysis_scope_error(new_scope, 400.), "")
        self.assertTrue(self.ledger.reserve("new-real-checkpoint", 400., meta))

    def test_concurrent_buy_sell_distinct_events_cannot_both_reserve_same_scope(self):
        def reserve(index):
            event = "candidate-" + str(index)
            side = "buy" if index % 2 else "sell"
            return self.ledger.reserve(event, 100., self.metadata(event, side, "m15_tactical", str(index)))
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(reserve, range(24)))
        self.assertEqual(sum(results), 1)
        restored = EventLedger(self.path)
        self.assertFalse(restored.error)
        self.assertEqual(len(restored.snapshot()["events"]), 1)


if __name__ == "__main__":
    unittest.main()
