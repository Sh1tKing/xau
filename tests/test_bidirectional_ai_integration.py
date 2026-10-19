"""Real host proofs through AI1/v4/AI2/broker, with every external call mocked."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import xauusd_fib_volume_pa_ai3 as ai
from strategies import citations
from strategies.direction import VERSION
import test_adaptive_ai_integration as existing

base = ai.base


class BidirectionalAITests(unittest.TestCase):
    def fixture(self, side="buy", profile="asia_adaptive"):
        fixture = existing.AdaptiveAITests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.configure(profile, "sell" if side == "buy" else "buy")
        fixture.side = side
        # Preserve validated server times and opposite H1. M15 has one real
        # upside break followed by two holds; the full M5 history remains closed.
        m15, _, m5 = fixture.data
        for name, value in (("open", 104.), ("high", 104.5), ("low", 103.5), ("close", 104.)):
            m5[name] = value
        for i in m15.index[-3:]:
            m15.loc[i, ["open", "high", "low", "close"]] = [104., 105., 100., 104.]
        m15.loc[m15.index[-3], ["open", "low"]] = [100., 99.]
        if side == "sell":
            for frame in (m15, m5):
                old = frame.copy()
                frame["open"], frame["close"] = 300.-old["open"], 300.-old["close"]
                frame["high"], frame["low"] = 300.-old["low"], 300.-old["high"]
        fixture.tick.bid, fixture.tick.ask = (104., 104.2) if side == "buy" else (195.8, 196.)
        info = SimpleNamespace(trade_tick_size=.01, point=.01, digits=2,
                               trade_stops_level=0, trade_freeze_level=0)
        scope = patch.object(base.mt5, "symbol_info", return_value=info)
        scope.start(); self.addCleanup(scope.stop)
        fixture.packs = []
        def proposal(request):
            from test_ai_citations import cite
            pack = json.loads(request["messages"][-1]["content"].split("\n", 1)[1])
            fixture.packs.append(deepcopy(pack))
            own = next(item for item in pack["opportunities"] if item["action"] == side)
            from test_ai_quality_ledger_v4 import card
            _, value = card(side)
            value.update(opportunity_id=own["opportunity_id"],
                         stop_loss=97. if side == "buy" else 203.,
                         take_profit=120. if side == "buy" else 180.,
                         trigger_evidence="真实M15收盘突破后M5保持，交易这次短线延续",
                         invalidation="原结构起点失守",
                         target_basis="已提供反向障碍与原结构延展空间")
            refs = ["/direction_context/m15_leg/confirmation/close"]
            for item in value["quality_assessment"]["dimensions"].values():
                item["evidence_refs"] = list(refs)
            return cite(pack,value)
        fixture.proposal = proposal
        return fixture

    def test_counter_h1_buy_sell_all_profiles_reach_actual_mocked_broker(self):
        for profile in ("asia_adaptive", "london_adaptive", "us_adaptive"):
            for side in ("buy", "sell"):
                with self.subTest(profile=profile, side=side):
                    f = self.fixture(side, profile)
                    post = f.run_entry()
                    self.assertEqual(post.call_count, 2, f.snapshots)
                    self.assertEqual(len(f.sent), 1, f.snapshots)
                    source = f.packs[0]
                    self.assertEqual(source["direction_context"]["h1_background"], "bear" if side == "buy" else "bull")
                    self.assertFalse(source["rules"]["h1_sets_direction"])
                    self.assertEqual(set(source["computed_evidence"]["price_action"]["M15"]), {"buy", "sell"})
                    self.assertNotIn("host_verified_direction", json.dumps(source))
                    self.assertIn("recent_trades", source)
                    saved = f.trader.entry_contexts["123"]
                    self.assertEqual(saved["quality_version"], "entry-quality-v4")
                    self.assertEqual(saved["direction_context"]["version"], VERSION)
                    self.assertEqual(saved["direction_permission"]["route"], "m15_tactical")
                    self.assertEqual(saved["direction_permission"]["anchor"]["kind"], "origin")
                    self.assertEqual(f.sent[0]["sl"], saved["initial_sl"])
                    self.assertEqual(f.sent[0]["tp"], saved["initial_tp"])
                    # Isolate each subcase before the next fixture's global patches.
                    f.doCleanups()

    def test_missing_mutated_or_ai_asserted_proof_cannot_finalize_counter_h1(self):
        f = self.fixture()
        _, _, _, local, pack, _, atr, _ = f.trader.collect_entry_snapshot("source-test")
        candidate = next(item for item in pack["opportunities"] if item["action"] == "buy")
        evidence = candidate["strategy_evidence"]
        payload = {"action": "buy", "confidence": 80, "stop_loss": 97., "take_profit": 120.,
                   "trigger_evidence": "real closed trigger", "invalidation": "real origin", "target_basis": "real target"}
        args = dict(df_m15=f.data[0], df_m5=f.data[2], strategy_contract=candidate["strategy_contract"])
        for kind in ("missing", "changed_anchor", "ai_boolean", "wrong_side", "legacy"):
            changed = deepcopy(evidence)
            facts = changed["buy"]["evidence"]
            contract = deepcopy(args["strategy_contract"])
            if kind == "missing": facts.pop("host_verified_direction")
            if kind == "changed_anchor": facts["host_verified_direction"]["permission"]["anchor"]["price"] += .01
            if kind == "ai_boolean": facts["host_verified_direction"] = {"allowed": True}
            if kind == "wrong_side": facts["host_verified_direction"]["action"] = "sell"
            if kind == "legacy": contract.pop("direction_policy")
            result, reason = ai.finalize_ai_proposal(payload, f.tick.ask, atr, "bear", "bull", None,
                **{**args, "strategy_contract": contract}, strategy_evidence=changed)
            self.assertIsNone(result, kind)
            self.assertTrue(reason, kind)
        self.assertEqual(local.details["strategy_contract"]["id"], "asia_adaptive")

    def test_quote_break_or_new_closed_source_before_review_blocks_without_ai2(self):
        for side in ("buy", "sell"):
            for mutation in ("quote", "source", "anchor", "h1_revision", "fingerprint"):
                with self.subTest(side=side, mutation=mutation):
                    f = self.fixture(side)
                    def change():
                        if mutation == "quote":
                            f.tick.bid, f.tick.ask = (100., 100.2) if side == "buy" else (199.8, 200.)
                        elif mutation == "source":
                            f.now += 300; f.tick.time += 300; f.data[2]["time"] += 300
                        elif mutation == "anchor":
                            f.data[2].loc[f.data[2].index[-1], "low" if side == "buy" else "high"] += (-1. if side == "buy" else 1.)
                        elif mutation == "h1_revision":
                            # Same closed bar/time/EMA label, different original OHLC is still a new source.
                            f.data[1].loc[f.data[1].index[-1], "high"] += .01
                        else:
                            ai.base.STRATEGIES.require_current = lambda _: (_ for _ in ()).throw(base.StrategyError("changed fingerprint"))
                    original = ai.base.STRATEGIES.require_current
                    try:
                        post = f.run_entry(before_review=change)
                    finally:
                        ai.base.STRATEGIES.require_current = original
                    self.assertEqual(post.call_count, 1, f.snapshots)
                    self.assertEqual(f.sent, [], f.snapshots)
                    f.doCleanups()

    def test_quote_invalidated_after_atomic_reservation_consumes_budget_without_send(self):
        from strategies.lifecycle import EventLedger
        from strategies.adaptive import checkpoint_order_scope
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                f = self.fixture(side)
                ledger = f.trader.strategy_ledger
                reserve = ledger.reserve
                scopes = []
                def reserve_then_move(event, now, metadata):
                    accepted = reserve(event, now, metadata)
                    if accepted:
                        scopes.append(metadata["analysis_order_scope"])
                        f.tick.bid, f.tick.ask = (100., 100.2) if side == "buy" else (199.8, 200.)
                    return accepted
                with patch.object(ledger, "reserve", side_effect=reserve_then_move):
                    post = f.run_entry()
                self.assertEqual(post.call_count, 2)
                self.assertEqual(f.sent, [])
                self.assertEqual(len(scopes), 1)
                restored = EventLedger(ledger.path)
                self.assertIn("consumed", restored.analysis_scope_error(scopes[0], f.now))
                self.assertEqual(len(restored.snapshot()["events"]), 1)
                f.doCleanups()

    def test_shared_citation_whitelist_and_foreign_candidate_isolation(self):
        f = self.fixture()
        *_, pack, fib, atr, price = f.trader.collect_entry_snapshot("citations")
        request = ai.prepare_ai1_request_pack(pack)
        paths = set(citations.catalog_bindings(request).values())
        self.assertIn("/direction_context/m15_leg/confirmation/close", paths)
        self.assertIn("/recent_trades/available", paths)
        for path in ("/direction_context/permissions/sell/stop_loss", "/direction_context/private/attestation",
                     "/direction_context/m15_leg/other_candidate", "/recent_trades/records/0/entry_context/quality_assessment",
                     "/recent_trades/partitions/account", "/opportunities/0/strategy_evidence/buy/evidence/host_verified_direction"):
            self.assertFalse(citations.scope_allowed(path, 0, "buy"), path)
        self.assertFalse(citations.scope_allowed("/opportunities/1/local_plan/stop_loss", 0, "buy"))

    def test_guardian_pack_hides_private_proof_and_keeps_original_public_context(self):
        f = self.fixture()
        f.run_entry()
        saved = deepcopy(f.trader.entry_contexts["123"])
        compact = ai.compact_market_pack({"positions": [{"entry_context": saved}]})
        self.assertNotIn("host_verified_direction", json.dumps(compact))
        self.assertEqual(compact["positions"][0]["entry_context"]["direction_permission"], saved["direction_permission"])
        public = compact["positions"][0]["entry_context"]
        self.assertEqual(public["direction_context"], saved["direction_context"])
        for assessment in public["strategy_evidence"].values():
            self.assertNotIn("direction_context", assessment["evidence"])
        prompt = ai.role_instructions("AI-3", [saved["strategy_contract"]])
        self.assertIn("逆H1是入场时已知事实", prompt)
        self.assertIn("TTL到期不代表持仓失效", prompt)

    def test_ai2_failover_rechecks_original_quote_without_changing_fixed_prices(self):
        f = self.fixture()
        reviews = []
        def post(url, **kwargs):
            request = kwargs["json"]
            pack = json.loads(request["messages"][-1]["content"].split("\n", 1)[1])
            if "proposal" not in pack:
                return existing.Response(f.proposal(request))
            reviews.append(deepcopy(pack))
            f.tick.bid, f.tick.ask = 100., 100.2
            raise ai.requests.ConnectionError("mock unavailable primary")
        with patch.dict(ai.os.environ, {"AI2_BACKUP_URL": "https://backup.test/chat", "AI2_BACKUP_KEY": "mock"}), \
             patch.object(ai.requests, "post", side_effect=post) as http:
            stamp = int(f.data[0].time.iloc[-1])
            f.trader.entry_task(stamp, f.trader.claim_task("entry", stamp, 360))
        self.assertEqual(http.call_count, 2, f.snapshots)
        self.assertEqual(len(reviews), 1)
        fixed = reviews[0]["python_finalization"]
        self.assertEqual((fixed["final_stop_loss"], fixed["final_take_profit"]), (97., 120.))
        self.assertEqual(f.sent, [])

    def test_final_execution_revalidates_quote_after_ai2_approval(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                f = self.fixture(side)
                def post(url, **kwargs):
                    request = kwargs["json"]
                    pack = json.loads(request["messages"][-1]["content"].split("\n", 1)[1])
                    if "proposal" not in pack:
                        return existing.Response(f.proposal(request))
                    f.tick.bid, f.tick.ask = (100., 100.2) if side == "buy" else (199.8, 200.)
                    return existing.Response({"decision": "APPROVE", "defects": [], "fact_conflicts": [], "reasoning": "mock approved"})
                with patch.object(ai.requests, "post", side_effect=post) as http:
                    stamp = int(f.data[0].time.iloc[-1])
                    f.trader.entry_task(stamp, f.trader.claim_task("entry", stamp, 360))
                self.assertEqual(http.call_count, 2, f.snapshots)
                self.assertEqual(f.sent, [], f.snapshots)
                f.doCleanups()


if __name__ == "__main__":
    unittest.main()
