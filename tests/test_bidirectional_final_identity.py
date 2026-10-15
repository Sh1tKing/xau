"""Source account and strategy identity stay fixed through the last broker gate."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_bidirectional_ai_integration as fixtures
from strategies.lifecycle import EventLedger

base, ai = fixtures.base, fixtures.ai


def account(login=111, server="independent-demo", mode=None):
    return SimpleNamespace(login=login, server=server,
        trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO if mode is None else mode)


class BidirectionalFinalIdentityTests(unittest.TestCase):
    def fixture(self, side):
        owner = fixtures.BidirectionalAITests()
        self.addCleanup(owner.doCleanups)
        return owner.fixture(side)

    def test_request_scope_survives_direction_route_anchor_id_and_build_changes(self):
        f = self.fixture("buy")
        _, _, _, _, pack, _, _, _ = f.trader.collect_entry_snapshot("shared-budget")
        original = ai.analysis_scope_keys(pack)
        self.assertTrue(original)
        for mutation in ("build", "route", "anchor", "opportunity_id", "direction"):
            with self.subTest(mutation=mutation):
                changed = deepcopy(pack)
                candidate = changed["opportunities"][0]
                side = candidate["action"]
                evidence = candidate["strategy_evidence"][side]["evidence"]
                if mutation == "build":
                    candidate["strategy_fingerprint"] = "a-different-definition-build"
                elif mutation == "route":
                    evidence["direction_permission"]["route"] = "h1_recovery"
                elif mutation == "anchor":
                    evidence["direction_permission"]["anchor"] = {"kind": "local_pullback", "price": 103.}
                elif mutation == "opportunity_id":
                    candidate["opportunity_id"] = "another-candidate-id"
                else:
                    opposite = "sell" if side == "buy" else "buy"
                    candidate["action"] = opposite
                    candidate["strategy_evidence"][opposite] = candidate["strategy_evidence"].pop(side)
                    evidence["analysis_checkpoint"]["action"] = opposite
                self.assertEqual(ai.analysis_scope_keys(changed), original)

    def test_final_fingerprint_change_after_reserve_stops_both_directions(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                f = self.fixture(side)
                ledger, changed = f.trader.strategy_ledger, [False]
                reserve, require_current = ledger.reserve, base.STRATEGIES.require_current
                scopes = []

                def current(fingerprint):
                    if changed[0]:
                        raise base.StrategyError("strategy changed during durable reservation")
                    return require_current(fingerprint)

                def reserve_then_change(event, now, metadata):
                    accepted = reserve(event, now, metadata)
                    if accepted:
                        changed[0] = True
                        scopes.append(metadata["analysis_order_scope"])
                    return accepted

                with patch.object(base.STRATEGIES, "require_current", side_effect=current), \
                     patch.object(ledger, "reserve", side_effect=reserve_then_change):
                    http = f.run_entry()
                self.assertEqual(http.call_count, 2, f.snapshots)
                self.assertTrue(changed[0], f.snapshots)
                self.assertEqual(f.sent, [], "old strategy must stop after durable reservation")
                restored = EventLedger(ledger.path)
                self.assertIn("consumed", restored.analysis_scope_error(scopes[0], f.now))
                f.doCleanups()

    def test_account_switch_after_reserve_or_during_final_source_fetch_stops_send(self):
        for side in ("buy", "sell"):
            for timing in ("reserve", "final_source_fetch", "final_account_read"):
                for changed_account in (account(login=222), account(server="other-demo"),
                                        account(login=None), account(server=None)):
                    with self.subTest(side=side, timing=timing, account=vars(changed_account)):
                        f = self.fixture(side)
                        ledger = f.trader.strategy_ledger
                        reserve = ledger.reserve
                        current_account, reserved, after_reserve_reads = [account()], [False], [0]
                        scopes = []

                        def reserve_then_change(event, now, metadata):
                            accepted = reserve(event, now, metadata)
                            if accepted:
                                reserved[0] = True
                                scopes.append(metadata["analysis_order_scope"])
                                if timing == "reserve":
                                    current_account[0] = changed_account
                            return accepted

                        def source_then_change():
                            if reserved[0] and timing == "final_source_fetch":
                                current_account[0] = changed_account
                            return tuple(f.data)

                        def live_account():
                            if reserved[0] and timing == "final_account_read":
                                after_reserve_reads[0] += 1
                                # Original namespace is checked before and after
                                # final source collection. The final account read
                                # must itself match the frozen identity too.
                                if after_reserve_reads[0] >= 3:
                                    current_account[0] = changed_account
                            return current_account[0]

                        with patch.object(base.mt5, "account_info", side_effect=live_account), \
                             patch.object(f.trader, "get_context", side_effect=source_then_change), \
                             patch.object(ledger, "reserve", side_effect=reserve_then_change):
                            http = f.run_entry()
                        self.assertEqual(http.call_count, 2, f.snapshots)
                        self.assertTrue(reserved[0], f.snapshots)
                        self.assertEqual(f.sent, [], "source proposal must not execute on a different account/server")
                        restored = EventLedger(ledger.path)
                        self.assertIn("consumed", restored.analysis_scope_error(scopes[0], f.now))
                        f.doCleanups()

    def test_missing_account_identity_prevents_first_paid_request(self):
        for side in ("buy", "sell"):
            for source_account in (account(login=None), account(server=None)):
                with self.subTest(side=side, account=vars(source_account)):
                    f = self.fixture(side)
                    with patch.object(base.mt5, "account_info", return_value=source_account):
                        http = f.run_entry()
                    self.assertEqual(http.call_count, 0, f.snapshots)
                    self.assertEqual(f.sent, [])
                    f.doCleanups()

    def test_account_switch_after_ai1_blocks_ai2_without_extra_http(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                f = self.fixture(side)
                current_account = [account()]
                with patch.object(base.mt5, "account_info", side_effect=lambda: current_account[0]):
                    http = f.run_entry(before_review=lambda: current_account.__setitem__(0, account(login=222)))
                self.assertEqual(http.call_count, 1, f.snapshots)
                self.assertEqual(f.sent, [])
                f.doCleanups()

    def test_ai2_retry_rechecks_original_account_before_another_post(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                f = self.fixture(side)
                current_account, reviews = [account()], []

                def post(url, **kwargs):
                    request = kwargs["json"]
                    pack = json.loads(request["messages"][-1]["content"].split("\n", 1)[1])
                    if "proposal" not in pack:
                        return fixtures.existing.Response(f.proposal(request))
                    reviews.append(pack)
                    current_account[0] = account(server="different-demo")
                    raise ai.requests.ConnectionError("mock primary transport failure")

                with patch.object(base.mt5, "account_info", side_effect=lambda: current_account[0]), \
                     patch.dict(ai.os.environ, {"AI2_BACKUP_URL": "https://backup.test/chat", "AI2_BACKUP_KEY": "mock"}), \
                     patch.object(ai.requests, "post", side_effect=post) as http:
                    stamp = int(f.data[0].time.iloc[-1])
                    f.trader.entry_task(stamp, f.trader.claim_task("entry", stamp, 360))
                self.assertEqual(http.call_count, 2, f.snapshots)
                self.assertEqual(len(reviews), 1)
                self.assertEqual(f.sent, [])
                f.doCleanups()


if __name__ == "__main__":
    unittest.main()
