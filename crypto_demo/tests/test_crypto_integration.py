"""Actual coarse→mock HTTP→JSON quality→AI-2→broker submit→AI-3 paths."""
from contextlib import ExitStack
import copy
from datetime import datetime, timezone
import json
import hashlib
import logging
logging.disable(logging.CRITICAL)
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from crypto_demo.tests import bootstrap
from crypto_demo import crypto_ai3 as ai, risk
from crypto_demo.pending import PendingOrder
from crypto_demo.strategies.framework import Registry
from crypto_demo.tests.test_crypto_risk import specification, account

base = ai.base


class Response:
    status_code, text, headers = 200, "", {}
    def __init__(self, value):
        self.value = value
    def json(self):
        return {"type": "message", "content": [{"type": "text", "text": json.dumps(self.value)}], "stop_reason": "end_turn"}


class CryptoIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.dir = Path(self.temp.name)
        self.positions, self.sent, self.requests, self.snapshots = [], [], [], []
        for item in (patch.dict(ai.os.environ, {"FIB_PA_STRATEGY_STATE_FILE": str(self.dir/"events.json"), "CRYPTO_SERVER_UTC_OFFSET_HOURS": "0", "LOCALAPPDATA": self.temp.name}, clear=True),
                     patch.object(ai, "AI3_STATE_FILE", str(self.dir/"ai3.json")),
                     patch.object(base, "LOG_DIR", str(self.dir/"logs")),
                     patch.object(ai, "AI_USAGE_DIR", str(self.dir/"usage")),
                     patch.object(ai, "append_decision_snapshot", side_effect=lambda *args: self.snapshots.append(args)),
                     patch.object(ai, "AI_ENABLED", True), patch.object(base, "MODE", "demo"),
                     patch.object(base, "ENABLE_TRADING", True), patch.object(base, "send_feishu_notification"),
                     patch.object(ai, "AI1_URL", "https://mock.test/v1/messages"),
                     patch.object(ai, "AI2_URL", "https://mock.test/v1/messages"),
                     patch.object(ai, "AI3_URL", "https://mock.test/v1/messages")):
            self.stack.enter_context(item)
        self.trader = ai.ThreeAITrader()
        self.trader.pending_order = PendingOrder(self.dir/"pending.json", "offline-identity")

    def configure(self, asset="BTC", side="buy"):
        self.asset, self.side = asset, side
        self.price, self.unit = (100000., 200.) if asset == "BTC" else (3000., 6.)
        self.now = datetime(2026, 10, 11, 2, 35, 5, tzinfo=timezone.utc).timestamp()
        self.registry = Registry(Path(base.BASE_DIR)/"strategies"/"definitions"/asset.lower())
        self.spec = specification(trade_tick_size=.5 if asset == "BTC" else .05, point=.1 if asset == "BTC" else .01)
        self.acct = account()
        sign = 1 if side == "buy" else -1
        self.tick = NS(bid=self.price if side == "buy" else self.price-self.unit*.05,
                       ask=self.price+self.unit*.05 if side == "buy" else self.price,
                       time=int(self.now), time_msc=int(self.now*1000))
        self.frames = []
        for tf, step in (("M15", 900), ("H1", 3600), ("M5", 300)):
            end = int(self.now//step)*step
            rows = []
            for i in range(80):
                close = self.price+sign*i*self.unit*.02 if tf == "H1" else self.price
                rows.append(dict(time=end-(80-i)*step, open=close, high=close+self.unit,
                                 low=close-self.unit, close=close, tick_volume=100))
            self.frames.append(base.pd.DataFrame(rows))
        # An opposite closed M5 candle is a soft analysis fact.
        self.frames[2].loc[79, "open"] = self.price+sign*self.unit*.7
        self.frames[2].loc[79, "close"] = self.price-sign*self.unit*.7
        for item in (patch.object(base, "ASSET", asset), patch.object(base, "STRATEGIES", self.registry),
                     patch.object(base, "SYMBOL", asset+"USD.mock"), patch.object(base.time, "time", side_effect=lambda: self.now),
                     patch.object(self.trader, "get_context", side_effect=lambda: tuple(self.frames)),
                     patch.object(self.trader, "get_rates", side_effect=lambda tf, count, shift=1: self.frames[{base.mt5.TIMEFRAME_H1:1, base.mt5.TIMEFRAME_M15:0}.get(tf,2)]),
                     patch.object(base.mt5, "account_info", side_effect=lambda: self.acct),
                     patch.object(base.mt5, "symbol_info", side_effect=lambda _: self.spec),
                     patch.object(base.mt5, "symbol_info_tick", side_effect=lambda _: self.tick),
                     patch.object(base.mt5, "positions_get", side_effect=lambda **kwargs: list(self.positions)),
                     patch.object(base.mt5, "order_calc_profit", side_effect=lambda kind, sym, vol, entry, end: (end-entry)*vol*(1 if kind == 0 else -1)),
                     patch.object(base.mt5, "order_calc_margin", side_effect=lambda kind, sym, vol, entry: vol*entry*.01),
                     patch.object(base.mt5, "order_check", return_value=NS(retcode=0, margin=1, margin_free=99, equity=100)),
                     patch.object(base.mt5, "order_send", side_effect=self.send)):
            self.stack.enter_context(item)

    def send(self, request):
        self.sent.append(copy.deepcopy(request))
        if getattr(self, "uncertain", False):
            return None
        self.positions[:] = [NS(ticket=123, identifier=123, symbol=base.SYMBOL, magic=base.MAGIC,
                                type=request["type"], comment=request["comment"], price_open=request["price"],
                                sl=request["sl"], tp=request["tp"], time=int(self.now), volume=request["volume"], profit=0.)]
        return NS(retcode=base.mt5.TRADE_RETCODE_DONE, order=123, price=request["price"], comment="mock")

    def proposal(self, pack):
        selected = pack["opportunities"][0]
        sign = 1 if self.side == "buy" else -1
        return dict(action=self.side, opportunity_id=selected["opportunity_id"],
                    stop_loss=self.price-sign*3*self.unit, take_profit=self.price+sign*9*self.unit,
                    trigger_evidence="近期M5收盘位置及实体，H1许可的自主确认", invalidation="原保护结构收盘失效", target_basis="现有结构空间", fact_conflicts=[], unknowns=[], wait_basis=[],
                    quality_assessment=dict(version="entry-quality-v2", direction=self.side, dimensions={
                        key: dict(level="strong", reason="真实来源支持", evidence_refs=["/m5_bars/79/close"], reservations=[])
                        for key in ai.QUALITY_DIMENSIONS}))

    def run_entry(self, mutate=None, before_review=None, review="APPROVE", after_review=None):
        def post(url, **kwargs):
            self.requests.append(copy.deepcopy(kwargs["json"]))
            pack = json.loads(kwargs["json"]["messages"][-1]["content"].split("\n",1)[1])
            if "proposal" not in pack:
                value = self.proposal(pack)
                if mutate:
                    value = mutate(value)
                if before_review:
                    before_review()
            else:
                value = dict(decision=review, defects=[], fact_conflicts=[], reasoning="审核固定价格与来源")
                if after_review:
                    after_review()
            return Response(value)
        with patch.object(ai.requests, "post", side_effect=post) as mock:
            stamp = int(self.frames[0].time.iloc[-1])
            sequence = self.trader.claim_task("entry", stamp, 360)
            self.trader.entry_task(stamp, sequence)
        return mock

    def test_btc_buy_real_chain_and_original_contract_ai3(self):
        self.configure()
        self.assertEqual(self.trader.evaluate_context(*self.frames).action, "no")
        mock = self.run_entry()
        self.assertEqual(mock.call_count, 2, self.snapshots)
        self.assertEqual(len(self.sent), 1, self.snapshots)
        saved = self.trader.entry_contexts["123"]
        self.assertEqual(saved["strategy_contract"]["max_hold_minutes"], 90)
        self.assertLessEqual(self.sent[0]["volume"]*abs(self.sent[0]["price"]-self.sent[0]["sl"]), 8)
        review = json.loads(self.requests[1]["messages"][-1]["content"].split("\n",1)[1])
        self.assertEqual(review["python_finalization"]["final_stop_loss"], self.sent[0]["sl"])
        self.assertEqual(review["python_finalization"]["final_take_profit"], self.sent[0]["tp"])
        self.assertGreaterEqual(review["cash_risk"]["net_rr"], 1.5)
        original = copy.deepcopy(saved["strategy_contract"])
        temp_defs = self.dir/"defs"
        temp_defs.mkdir()
        source = Path(base.BASE_DIR)/"strategies/definitions/btc/adaptive.py"
        (temp_defs/"adaptive.py").write_text(source.read_text(encoding="utf-8").replace("max_hold_minutes=90", "max_hold_minutes=180"), encoding="utf-8")
        with patch.object(base, "STRATEGIES", Registry(temp_defs)):
            self.assertEqual(base.STRATEGIES.snapshot().get("btc_adaptive").max_hold_minutes, 180)
            snapshot = self.trader.collect_ai3_snapshot({123})
        prompt = json.loads(snapshot["prompt"].split("\n",1)[1])
        self.assertEqual(prompt["positions"][0]["entry_context"]["strategy_contract"], original)
        self.assertEqual(self.trader.strategy_ledger.snapshot()["positions"]["123"]["max_hold_minutes"], 90)

    def test_eth_sell_actual_chain(self):
        self.configure("ETH", "sell")
        self.assertEqual(self.run_entry().call_count, 2, self.snapshots)
        self.assertEqual(len(self.sent), 1, self.snapshots)
        self.assertEqual(self.trader.entry_contexts["123"]["strategy_contract"]["max_hold_minutes"], 60)

    def assert_shared_refs_entry(self, asset, side):
        self.configure(asset, side)
        def cite_shared_facts(value):
            dimensions = value["quality_assessment"]["dimensions"]
            dimensions["structure"]["evidence_refs"] = ["/m15_ema21_distance_atr"]
            dimensions["protection"]["evidence_refs"] = [
                "/rules/price_finalization/execution_min_stop_clearance"]
            return value
        status = lambda: [(stage, data.get("quality_error")) for _, stage, data in self.snapshots
                          if stage == "ai1_result"]
        self.assertEqual(self.run_entry(mutate=cite_shared_facts).call_count, 2, status())
        self.assertEqual(len(self.sent), 1, status())

    def test_btc_shared_ema_and_clearance_refs_reach_ai2(self):
        self.assert_shared_refs_entry("BTC", "buy")

    def test_eth_shared_ema_and_clearance_refs_reach_ai2(self):
        self.assert_shared_refs_entry("ETH", "sell")

    def test_btc_sell_actual_chain(self):
        self.configure("BTC", "sell")
        self.assertEqual(self.run_entry().call_count, 2, self.snapshots)
        self.assertEqual(len(self.sent), 1, self.snapshots)

    def test_eth_buy_actual_chain(self):
        self.configure("ETH", "buy")
        self.assertEqual(self.run_entry().call_count, 2, self.snapshots)
        self.assertEqual(len(self.sent), 1, self.snapshots)

    def run_ai3_hold(self):
        stamp = int(self.now//60)*60
        seq = self.trader.claim_task("ai3", stamp, 220)
        with patch.object(ai.requests, "post", return_value=Response(dict(action="hold", ticket=123, reasoning="原理由继续"))) as post:
            self.trader.ai3_task(stamp, seq)
        return post

    def assert_final_ai3_guard(self, failure, at_refresh=False):
        self.configure()
        self.run_entry()
        self.trader.get_or_create_guardian_state(self.positions[0], self.tick)
        if at_refresh:
            self.trader.collect_ai3_snapshot({123})
        before = self.protected_state()
        fresh = copy.deepcopy(self.tick)
        calls = 0
        def rates(tf, count, shift=1):
            nonlocal calls
            calls += 1
            if calls == (8 if at_refresh else 4):
                if failure == "account":
                    self.acct.login = 2
                elif failure == "spec":
                    self.spec.trade_tick_size = 0
                else:
                    self.tick = copy.deepcopy(fresh)
                    self.tick.time += -3601 if failure == "stale" else 1
            return self.frames[{base.mt5.TIMEFRAME_H1:1, base.mt5.TIMEFRAME_M15:0}.get(tf,2)]
        with patch.object(self.trader, "get_rates", side_effect=rates):
            self.assertEqual(self.run_ai3_hold().call_count, 0)
        self.assertEqual(self.protected_state(), before)
        self.acct.login = 1
        self.spec.trade_tick_size = .5
        self.tick = fresh
        self.assertEqual(self.run_ai3_hold().call_count, 1)

    def test_ai3_final_stale_tick_zero_posts_and_fresh_resume(self):
        self.assert_final_ai3_guard("stale")

    def test_ai3_final_future_tick_zero_posts_and_fresh_resume(self):
        self.assert_final_ai3_guard("future")

    def test_ai3_post_refresh_final_tick_zero_posts(self):
        self.assert_final_ai3_guard("stale", at_refresh=True)

    def test_ai3_post_refresh_final_future_tick_zero_posts(self):
        self.assert_final_ai3_guard("future", at_refresh=True)

    def test_ai3_final_spec_change_preserves_context(self):
        self.assert_final_ai3_guard("spec")

    def test_ai3_final_account_change_preserves_context(self):
        self.assert_final_ai3_guard("account")

    def test_ai3_final_explicit_three_hour_offset_is_fresh(self):
        self.configure()
        self.run_entry()
        with patch.dict(ai.os.environ, {"CRYPTO_SERVER_UTC_OFFSET_HOURS": "3"}):
            self.tick.time += 10800
            self.tick.time_msc += 10800000
            self.assertEqual(self.run_ai3_hold().call_count, 1)

    def reload_original_definition(self):
        directory = self.dir/"reload"
        directory.mkdir(exist_ok=True)
        source = Path(base.BASE_DIR)/("strategies/definitions/"+self.asset.lower()+"/adaptive.py")
        text = source.read_text(encoding="utf-8")
        text = text.replace("max_hold_minutes=90", "max_hold_minutes=180").replace("max_hold_minutes=60", "max_hold_minutes=180")
        (directory/"adaptive.py").write_text(text, encoding="utf-8")
        self.stack.enter_context(patch.object(base, "STRATEGIES", Registry(directory)))
        self.assertEqual(base.STRATEGIES.snapshot().get(self.asset.lower()+"_adaptive").max_hold_minutes, 180)

    def delayed_original_context(self, uncertain=False, restart=False, side="buy", polling=False):
        self.configure("BTC" if side == "buy" else "ETH", side)
        original = copy.deepcopy(self.registry.snapshot().get(self.asset.lower()+"_adaptive").manifest())
        original_hold = original["max_hold_minutes"]
        def delayed(request):
            self.sent.append(copy.deepcopy(request))
            return None if uncertain else NS(retcode=base.mt5.TRADE_RETCODE_DONE, order=123, price=request["price"], comment="mock")
        with patch.object(base.mt5, "order_send", side_effect=delayed):
            self.assertEqual(self.run_entry().call_count, 2)
        self.assertEqual(len(self.sent), 1, self.snapshots)
        saved = self.trader.strategy_ledger.snapshot()
        event_id = next(iter(saved["events"]))
        meta = saved["events"][event_id]["metadata"]
        self.assertTrue("ai3_entry_context" in meta, "original AI-3 thesis was not persisted before submission")
        self.assertEqual(meta["ai3_entry_context"]["strategy_contract"], original)
        if uncertain:
            self.assertIsNotNone(self.trader.pending_order.read())
        else:
            self.assertIsNone(self.trader.pending_order.read())
        self.reload_original_definition()
        request = self.sent[0]
        self.now += 45
        self.tick.time = int(self.now)
        # Broker SL has already tightened: recovery must keep the original SL.
        self.positions[:] = [NS(ticket=123, identifier=456, symbol=base.SYMBOL, magic=base.MAGIC,
                                type=request["type"], comment=request["comment"], price_open=request["price"]+(.5 if side == "buy" else -.05),
                                sl=request["sl"]+(10 if side == "buy" else -1), tp=request["tp"],
                                time=int(self.now-45), volume=request["volume"], profit=0.)]
        if restart:
            self.trader = ai.ThreeAITrader()
            self.trader.pending_order = PendingOrder(self.dir/"pending.json", "offline-identity")
            self.trader.reconcile_entry_contexts()
        elif polling:
            self.trader.sync_position_close_notifications()
        snapshot = self.trader.collect_ai3_snapshot({123})
        pack = json.loads(snapshot["prompt"].split("\n", 1)[1])
        context = pack["positions"][0]["entry_context"]
        self.assertEqual(context["status"], "complete")
        self.assertEqual(context["strategy_contract"], original)
        self.assertEqual(context["initial_sl"], request["sl"])
        self.assertEqual(context["initial_tp"], request["tp"])
        self.assertEqual(context["entry_price"], self.positions[0].price_open)
        self.assertEqual(context["identifier"], 456)
        self.assertEqual(context["trigger_evidence"], "近期M5收盘位置及实体，H1许可的自主确认")
        self.assertEqual(context["invalidation"], "原保护结构收盘失效")
        self.assertEqual(context["target_basis"], "现有结构空间")
        self.assertEqual(snapshot["profile_rules"], ai.role_rules("AI-3", [original]))
        lifetime = self.trader.strategy_ledger.snapshot()["positions"]["123"]
        self.assertEqual(lifetime["max_hold_minutes"], original_hold)
        self.assertEqual(lifetime["cooldown_seconds"], original["cooldown_seconds"])
        self.assertEqual(lifetime["opened_at_utc"], self.now-45)
        complete_saved = copy.deepcopy(self.trader.entry_contexts["123"])
        self.trader.reconcile_entry_contexts()
        self.trader.sync_position_close_notifications()
        self.assertEqual(self.trader.entry_contexts["123"], complete_saved)
        posts = self.run_ai3_hold()
        self.assertEqual(posts.call_count, 1)
        paid = json.loads(posts.call_args.kwargs["json"]["messages"][-1]["content"].split("\n",1)[1])
        self.assertEqual(paid["positions"][0]["entry_context"]["strategy_contract"], original)
        self.assertEqual(self.trader.strategy_ledger.snapshot()["events"][event_id], saved["events"][event_id])

    def test_done_delayed_visibility_polling_keeps_original_context(self):
        self.delayed_original_context(polling=True)

    def test_done_delayed_visibility_restart_keeps_original_context(self):
        self.delayed_original_context(restart=True)

    def test_uncertain_visibility_ai3_keeps_original_sell_context(self):
        self.delayed_original_context(uncertain=True, side="sell")

    def test_uncertain_visibility_restart_keeps_original_sell_context(self):
        self.delayed_original_context(uncertain=True, restart=True, side="sell")

    def test_ai3_recovery_refuses_nonmatching_bindings_and_historical_unknown(self):
        self.configure()
        def uncertain(request):
            self.sent.append(copy.deepcopy(request))
            return None
        with patch.object(base.mt5, "order_send", side_effect=uncertain):
            self.run_entry()
        request = self.sent[0]
        position = NS(ticket=123, identifier=456, symbol=base.SYMBOL, magic=base.MAGIC,
                      type=request["type"], comment=request["comment"], price_open=request["price"],
                      sl=request["sl"], tp=request["tp"], time=int(self.now), volume=request["volume"], profit=0.)
        original_state = self.trader.strategy_ledger.snapshot()
        event_id = next(iter(original_state["events"]))
        for mismatch in ("token", "symbol", "magic", "direction", "account", "event", "historical", "duplicate"):
            with self.subTest(mismatch=mismatch):
                self.trader.entry_contexts.clear()
                self.positions[:] = [copy.deepcopy(position)]
                changed = copy.deepcopy(original_state)
                meta = changed["events"][event_id]["metadata"]
                if mismatch == "token":
                    self.positions[0].comment = "unrelated"
                elif mismatch == "symbol":
                    self.positions[0].symbol = "ETHUSD.mock"
                elif mismatch == "magic":
                    self.positions[0].magic += 1
                elif mismatch == "direction":
                    self.positions[0].type = base.mt5.POSITION_TYPE_SELL
                elif mismatch == "account":
                    meta["account_identity"]["server"] = "other-account-server"
                elif mismatch == "event":
                    meta["ai3_entry_context"]["execution_binding"]["event_id"] = "other-event"
                elif mismatch == "historical":
                    meta.pop("ai3_entry_context")
                else:
                    duplicate = copy.deepcopy(position)
                    duplicate.ticket = 124
                    self.positions.append(duplicate)
                self.assertTrue(self.trader.strategy_ledger._persist(changed))
                self.trader.reconcile_entry_contexts()
                self.assertFalse(any(c.get("status") == "complete" for c in self.trader.entry_contexts.values()))
                self.assertEqual(self.trader.strategy_ledger.snapshot()["positions"], {})
        self.assertTrue(self.trader.strategy_ledger._persist(original_state))
        self.positions[:] = [position]
        self.trader.reconcile_entry_contexts()
        self.assertEqual(self.trader.entry_contexts["123"]["status"], "complete")

    def test_original_context_serialization_failure_stops_actual_send(self):
        self.configure()
        original = self.trader.build_entry_context
        def unsafe(*args):
            context = original(*args)
            context["trigger_evidence"] = object()
            return context
        with patch.object(self.trader, "build_entry_context", side_effect=unsafe):
            self.assertEqual(self.run_entry().call_count, 2)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.trader.strategy_ledger.snapshot()["events"], {})
        self.assertIsNone(self.trader.pending_order.read())

    def test_original_context_atomic_write_failure_stops_actual_send(self):
        self.configure()
        with patch("crypto_demo.strategies.lifecycle.os.replace", side_effect=OSError("offline write failure")):
            self.assertEqual(self.run_entry().call_count, 2)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.trader.strategy_ledger.snapshot()["events"], {})
        self.assertIsNone(self.trader.pending_order.read())
        self.assertIn("write failed", self.trader.strategy_ledger.error)

    def test_every_quote_refreshed_fixed_sl_tp_through_submit(self):
        self.configure()
        def change(amount):
            self.tick.bid += amount
            self.tick.ask += amount
        self.assertEqual(self.run_entry(before_review=lambda: change(5), after_review=lambda: change(5)).call_count, 2, self.snapshots)
        first = json.loads(self.requests[0]["messages"][-1]["content"].split("\n", 1)[1])
        review = json.loads(self.requests[1]["messages"][-1]["content"].split("\n", 1)[1])
        self.assertEqual(first["live_quote"]["ask"], 100010)
        self.assertEqual(review["market_evidence"]["live_quote"]["ask"], 100015)
        self.assertEqual(self.sent[0]["price"], 100020)
        self.assertEqual(self.sent[0]["sl"], review["python_finalization"]["final_stop_loss"])
        self.assertEqual(self.sent[0]["tp"], review["python_finalization"]["final_take_profit"])

    def test_bad_quality_never_ai2_or_order(self):
        self.configure()
        self.assertEqual(self.run_entry(mutate=lambda value: {key: item for key, item in value.items() if key != "quality_assessment"}).call_count, 1)
        self.assertEqual(self.sent, [])

    def test_normal_no_never_ai2_or_order(self):
        self.configure()
        def no(value):
            value["action"] = "no"
            value["quality_assessment"]["dimensions"]["trigger"].update(level="weak", reservations=[dict(cause="trigger_weakness", event_group="current", reason="真实触发重大反证", evidence_refs=["/m5_bars/79/close"])])
            value["wait_basis"] = [dict(code="material_counter_evidence", reason="重大反证", evidence_refs=["/m5_bars/79/close"])]
            return value
        self.assertEqual(self.run_entry(mutate=no).call_count, 1)
        self.assertEqual(self.sent, [])

    def test_far_structural_stop_never_shortened_to_cap(self):
        self.configure()
        self.assertEqual(self.run_entry(mutate=lambda value: dict(value, stop_loss=98000)).call_count, 1)
        self.assertEqual(self.sent, [])

    def test_order_check_failure_no_submit(self):
        self.configure()
        with patch.object(base.mt5,"order_check",return_value=NS(retcode=10019)):
            self.assertEqual(self.run_entry().call_count,2,self.snapshots)
        self.assertEqual(self.sent,[])

    def test_ownership_account_and_modes_protect_guardian(self):
        self.configure()
        self.run_entry()
        position = self.positions[0]
        before = len(self.sent)
        self.acct.login = 2
        self.assertFalse(self.trader.modify_position_sl(position,99500,"test"))
        self.assertFalse(self.trader.close_position_by_ticket(position,"test"))
        with self.assertRaises(risk.RiskError):
            self.trader.collect_ai3_snapshot({123})
        self.acct.login = 1
        self.acct.trade_mode = base.mt5.ACCOUNT_TRADE_MODE_REAL
        self.assertFalse(self.trader.modify_position_sl(position,99500,"test"))
        self.assertFalse(self.trader.close_position_by_ticket(position,"test"))
        self.acct.trade_mode = base.mt5.ACCOUNT_TRADE_MODE_DEMO
        position.magic = 99
        self.assertFalse(self.trader.modify_position_sl(position,99500,"test"))
        self.assertFalse(self.trader.close_position_by_ticket(position,"test"))
        self.assertEqual(len(self.sent),before)

    def test_identity_switch_stops_before_ai2(self):
        self.configure()
        mock = self.run_entry(before_review=lambda: setattr(self.acct, "login", 2))
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(self.sent, [])

    def test_identity_switch_cannot_clear_matching_original_pending_token(self):
        self.configure()
        self.trader.pending_order.reserve(dict(comment="token", symbol=base.SYMBOL, magic=base.MAGIC, type=0))
        self.positions[:] = [NS(comment="token",symbol=base.SYMBOL,magic=base.MAGIC,type=0)]
        self.acct.login = 2
        with self.assertRaises(risk.RiskError):
            self.trader.crypto_account_ready(empty=True)
        self.assertIsNotNone(self.trader.pending_order.read())

    def test_two_directory_copies_use_same_durable_latch(self):
        self.configure()
        with patch.dict(ai.os.environ,{"LOCALAPPDATA":self.temp.name}):
            with patch.object(base,"LOG_DIR",str(self.dir/"copy-A")):
                first = ai.ThreeAITrader()
            with patch.object(base,"LOG_DIR",str(self.dir/"copy-B")):
                second = ai.ThreeAITrader()
            self.assertEqual(first.pending_order.path,second.pending_order.path)
            first.pending_order.reserve(dict(comment="unknown-BTC",symbol=base.SYMBOL,magic=base.MAGIC,type=0))
            self.assertTrue(second.pending_order.unresolved(()))

    def test_unknown_positions_stop_http(self):
        self.configure()
        with patch.object(base.mt5, "positions_get", return_value=None):
            self.assertEqual(self.run_entry().call_count, 0)

    def test_fixed_prices_new_quote_rejected_not_moved(self):
        self.configure()
        self.assertEqual(self.run_entry(before_review=lambda: setattr(self.tick,"ask",self.price+5*self.unit)).call_count, 1)
        self.assertEqual(self.sent, [])

    def test_no_reject_bad_json_quality_never_order(self):
        self.configure()
        self.assertEqual(self.run_entry(review="REJECT").call_count, 2)
        self.assertEqual(self.sent, [])

    def test_observe_no_ai_no_mutations_dry_run_no_guardian_mutations(self):
        self.configure()
        with patch.object(base,"MODE","observe"), patch.object(ai.requests,"post") as post:
            self.assertEqual(self.run_entry().call_count, 0)
            self.assertFalse(self.trader.modify_position_sl(NS(), 1, "test"))
            self.assertFalse(self.trader.close_position_by_ticket(NS(), "test"))
            post.assert_not_called()
        with patch.object(base,"MODE","dry-run"), patch.object(base,"ENABLE_TRADING",False):
            self.assertEqual(self.run_entry().call_count,2,self.snapshots)
            self.assertEqual(self.sent,[])
            self.assertFalse(self.trader.modify_position_sl(NS(),1,"test"))
            self.assertFalse(self.trader.close_position_by_ticket(NS(),"test"))

    def observe_once(self, occupied=False):
        self.configure()
        if occupied:
            self.run_entry()
        before = copy.deepcopy(self.sent)
        self.snapshots.clear()
        with ExitStack() as stack:
            stack.enter_context(patch.object(base, "MODE", "observe"))
            stack.enter_context(patch.object(base, "ENABLE_TRADING", False))
            stack.enter_context(patch.object(ai, "AI_ENABLED", False))
            stack.enter_context(patch.object(self.trader, "connect_mt5", return_value=True))
            stack.enter_context(patch.object(ai, "attach_ai_logger", return_value="offline"))
            stack.enter_context(patch.object(self.trader, "notify_lines"))
            stack.enter_context(patch.object(self.trader, "latest_bar_time", side_effect=lambda tf: int(self.now//({base.mt5.TIMEFRAME_M15:900, base.mt5.TIMEFRAME_M5:300}.get(tf,60)))*({base.mt5.TIMEFRAME_M15:900,base.mt5.TIMEFRAME_M5:300}.get(tf,60))))
            post = stack.enter_context(patch.object(ai.requests, "post"))
            shutdown = stack.enter_context(patch.object(base.mt5, "shutdown"))
            submit = stack.enter_context(patch.object(base.mt5, "order_send"))
            self.assertEqual(self.trader.run(once=True), 0)
            post.assert_not_called()
            submit.assert_not_called()
            shutdown.assert_called_once_with()
        candidates = [args for args in self.snapshots if args[1] == "entry_candidate"]
        self.assertEqual(len(candidates), 1, self.snapshots)
        self.assertEqual(candidates[0][2]["market_evidence"]["live_quote"]["bid"], self.tick.bid)
        self.assertEqual(self.sent, before)
        self.assertTrue(self.trader._stop.is_set())

    def test_observe_full_startup_local_snapshot_shutdown(self):
        self.observe_once()

    def test_observe_existing_demo_position_still_collects_local_snapshot(self):
        self.observe_once(occupied=True)

    def test_observe_readonly_backup_and_actual_request_boundary(self):
        self.configure()
        with patch.object(base, "MODE", "observe"), patch.object(ai.requests, "post") as post:
            self.assertEqual(ai.backup_chat_config("AI-1", "mock", "low", "https://mock.test/v1/messages", "offline")["model"], "mock")
            with self.assertRaises(ai.AIRequestError):
                ai.call_chat("AI-1", "mock", "low", "rules", "prompt", 1, "https://mock.test/v1/messages", "offline")
            post.assert_not_called()

    def test_observe_after_request_refresh_still_never_posts(self):
        self.configure()
        def refresh():
            base.MODE = "observe"
            return {"prompt": "fresh prompt"}
        with patch.object(base, "MODE", "dry-run"), patch.object(ai.requests, "post") as post:
            with self.assertRaises(ai.AIRequestError):
                ai.call_chat("AI-1", "mock", "low", "rules", "prompt", 1,
                             "https://mock.test/v1/messages", "offline", refresh_request=refresh)
            post.assert_not_called()

    def test_observe_invalid_startup_query_shutdown_preserves_context(self):
        self.seed_protected_position()
        before = self.protected_state()
        with patch.object(base, "MODE", "observe"), patch.object(self.trader, "connect_mt5", return_value=True), patch.object(base.mt5, "positions_get", return_value=None), patch.object(base.mt5, "shutdown") as shutdown, patch.object(ai.requests, "post") as post:
            with self.assertRaises(risk.RiskError):
                self.trader.run(once=True)
            shutdown.assert_called_once_with()
            post.assert_not_called()
        self.assertEqual(self.protected_state(), before)

    def protected_state(self):
        paths = (Path(ai.AI3_STATE_FILE), self.trader.strategy_ledger.path, self.trader.pending_order.path)
        return copy.deepcopy((self.trader.position_snapshots, self.trader.guardian_states,
                              self.trader.entry_contexts, self.trader.entry_confidence,
                              self.trader.notified_close_tickets, self.trader.strategy_ledger.snapshot(),
                              {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for path in paths}))

    def seed_protected_position(self, side="buy"):
        self.configure(side=side)
        self.run_entry()
        self.trader.sync_position_close_notifications()
        self.trader.get_or_create_guardian_state(self.positions[0], self.tick)
        self.trader.pending_order.reserve(dict(comment="unresolved", symbol=base.SYMBOL, magic=base.MAGIC, type=self.positions[0].type))
        self.assertIn("123", self.trader.strategy_ledger.snapshot()["positions"])

    def assert_poll_failure_preserves_state(self, native_none=False, during_query=False):
        self.seed_protected_position()
        before = self.protected_state()
        original = copy.deepcopy(self.positions)
        if native_none:
            query = lambda **kwargs: None
        elif during_query:
            def query(**kwargs):
                self.acct.login = 2
                return []
        else:
            self.acct.login = 2
            query = lambda **kwargs: []
        with patch.object(base.mt5, "positions_get", side_effect=query):
            for operation in (self.trader.sync_position_close_notifications,
                              self.trader.reconcile_entry_contexts,
                              self.trader.run_guardian,
                              lambda: self.trader.guard_strategy_lifecycle([]),
                              lambda: self.trader.crypto_account_ready(empty=True)):
                try:
                    operation()
                except (risk.RiskError, ai.MarketSnapshotError):
                    pass
                self.assertEqual(self.protected_state(), before, operation)
            self.assertIs(self.trader.positions_known, False)
        self.acct.login = 1
        self.positions[:] = original
        self.trader.sync_position_close_notifications()
        self.assertEqual(self.trader.entry_contexts["123"], before[2]["123"])
        self.assertIs(self.trader.positions_known, True)

    def test_switched_demo_polling_preserves_memory_and_disk(self):
        self.assert_poll_failure_preserves_state()

    def test_native_none_polling_preserves_memory_and_disk(self):
        self.assert_poll_failure_preserves_state(native_none=True)

    def test_identity_switch_during_query_preserves_memory_and_disk(self):
        self.assert_poll_failure_preserves_state(during_query=True)

    def test_identity_switch_during_query_cannot_resolve_matching_pending(self):
        self.seed_protected_position()
        self.positions[0].comment = "unresolved"
        before = self.protected_state()
        def switched(**kwargs):
            self.acct.login = 2
            return list(self.positions)
        with patch.object(base.mt5, "positions_get", side_effect=switched):
            with self.assertRaises(risk.RiskError):
                self.trader.crypto_account_ready(empty=True)
        self.assertEqual(self.protected_state(), before)

    def test_known_empty_configured_account_reconciles_close_and_cooldown(self):
        self.seed_protected_position()
        self.positions.clear()
        self.now += 31
        self.trader.sync_position_close_notifications()
        self.trader.run_guardian()
        self.assertEqual(self.trader.entry_contexts, {})
        self.assertEqual(self.trader.position_snapshots, {})
        self.assertEqual(self.trader.guardian_states, {})
        saved = self.trader.strategy_ledger.snapshot()
        self.assertEqual(saved["positions"], {})
        self.assertEqual(saved["cooldown_until"], self.now+60)
        self.assertIsNotNone(self.trader.pending_order.read())

    def test_invalid_quote_spec_block_frozen_deadline_even_guard_disabled(self):
        self.seed_protected_position()
        self.now += 91*60
        self.tick.time = int(self.now-31)
        before = self.protected_state()
        with patch.object(base, "GUARDIAN_ENABLED", False), patch.object(self.trader, "close_position_by_ticket", return_value=False) as close:
            self.trader.run_guardian()
            close.assert_not_called()
            self.assertEqual(self.protected_state(), before)
            self.tick.time = int(self.now)
            with patch.object(base.mt5, "symbol_info", return_value=specification(trade_tick_size=0)):
                self.trader.run_guardian()
                close.assert_not_called()
                self.assertEqual(self.protected_state(), before)
            self.trader.run_guardian()
            close.assert_called_once()

    def guardian_invalid_then_fresh(self, side, failure):
        self.seed_protected_position(side)
        before = self.protected_state()
        fresh = copy.deepcopy(self.tick)
        if failure == "infinite":
            self.tick.bid = float("inf") if side == "buy" else self.tick.bid
            self.tick.ask = float("inf")
        elif failure == "nan":
            self.tick.bid = self.tick.ask = float("nan")
        elif failure == "crossed":
            self.tick.ask = self.tick.bid - 1
        elif failure == "stale":
            self.tick.time -= 31
        elif failure == "future":
            self.tick.time += 1
        with ExitStack() as stack:
            stack.enter_context(patch.object(base, "GUARDIAN_ENABLED", True))
            stack.enter_context(patch.object(base, "is_structure_failure", return_value=(False, "")))
            close = stack.enter_context(patch.object(self.trader, "close_position_by_ticket", return_value=False))
            modify = stack.enter_context(patch.object(self.trader, "modify_position_sl", return_value=False))
            invalid = ExitStack()
            if failure == "spec":
                invalid.enter_context(patch.object(base.mt5, "symbol_info", return_value=specification(trade_tick_size=float("nan"))))
            elif failure in ("atr_nan", "atr_infinite", "atr_zero", "atr_negative"):
                invalid.enter_context(patch.object(base, "calculate_atr", return_value={"atr_nan":float("nan"), "atr_infinite":float("inf"), "atr_zero":0, "atr_negative":-1}[failure]))
            with invalid:
                try:
                    self.trader.run_guardian()
                except risk.RiskError:
                    pass
                self.assertEqual(self.protected_state(), before, failure)
                close.assert_not_called()
                modify.assert_not_called()
            self.tick = fresh
            sign = 1 if side == "buy" else -1
            self.tick.bid += sign*1000
            self.tick.ask += sign*1000
            self.trader.run_guardian()
            state = self.trader.guardian_states[123]
            self.assertNotEqual(state, before[1][123])
            self.assertTrue(base.math.isfinite(state.virtual_sl))
            self.assertGreater(state.virtual_sl, 0)
            self.assertTrue(modify.called)
            close.assert_not_called()

    def test_invalid_quotes_preserve_buy_guard_and_resume_fresh(self):
        for failure in ("infinite", "nan", "crossed", "stale", "future"):
            with self.subTest(failure=failure):
                try:
                    self.guardian_invalid_then_fresh("buy", failure)
                finally:
                    self.tearDown_guardian_case()

    def test_invalid_quotes_preserve_sell_guard_and_resume_fresh(self):
        for failure in ("infinite", "nan", "crossed", "stale", "future"):
            with self.subTest(failure=failure):
                try:
                    self.guardian_invalid_then_fresh("sell", failure)
                finally:
                    self.tearDown_guardian_case()

    def tearDown_guardian_case(self):
        # Isolate subcases while retaining this test's temporary mocked environment.
        self.trader.pending_order.clear()
        self.trader.strategy_ledger.path.unlink(missing_ok=True)
        Path(ai.AI3_STATE_FILE).unlink(missing_ok=True)
        self.positions.clear()
        self.trader = ai.ThreeAITrader()
        self.trader.pending_order = PendingOrder(self.dir/"pending.json", "offline-identity")

    def test_invalid_spec_atr_preserve_guard_and_resume_fresh(self):
        for side in ("buy", "sell"):
            for failure in ("spec", "atr_nan", "atr_infinite", "atr_zero", "atr_negative"):
                with self.subTest(side=side, failure=failure):
                    try:
                        self.guardian_invalid_then_fresh(side, failure)
                    finally:
                        self.tearDown_guardian_case()

    def test_frozen_deadline_runs_with_guard_disabled_and_no_atr(self):
        self.seed_protected_position()
        self.now += 91*60
        self.tick.time = int(self.now)
        with patch.object(base, "GUARDIAN_ENABLED", False), patch.object(base, "calculate_atr", return_value=float("nan")) as atr, patch.object(self.trader, "close_position_by_ticket", return_value=False) as close:
            self.trader.run_guardian()
            close.assert_called_once()
            self.assertEqual(close.call_args.args[0].ticket, 123)
            atr.assert_not_called()

    def test_uncertain_send_retains_account_latch(self):
        self.configure()
        self.uncertain = True
        self.assertEqual(self.run_entry().call_count,2,self.snapshots)
        self.assertEqual(len(self.sent),1,self.snapshots)
        self.assertIsNotNone(self.trader.pending_order.read())
        with self.assertRaises(risk.RiskError):
            self.trader.crypto_account_ready(empty=True)

    def test_freshness_exact_hour_stale_and_explicit_offset(self):
        self.configure()
        self.tick.time = int(self.now-3600)
        self.assertIsNone(base.tick_clock_offset(self.tick,self.now))
        self.assertEqual(self.run_entry().call_count,0)
        self.tick.time = int(self.now+10800)
        with patch.dict(ai.os.environ,{"CRYPTO_SERVER_UTC_OFFSET_HOURS":"3"}):
            self.assertEqual(base.tick_clock_offset(self.tick,self.now),3)


class OfflineEntryTests(unittest.TestCase):
    def test_help_check_import_no_mt5(self):
        root = Path(base.BASE_DIR).parent
        for args in (("--help",),("--asset","BTC","--check"),("--asset","ETH","--check")):
            result = subprocess.run([sys.executable,str(Path(base.BASE_DIR)/"run.py"),*args],cwd=root,capture_output=True,text=True,encoding="utf-8",errors="replace",env=dict(ai.os.environ,PYTHONIOENCODING="utf-8"))
            self.assertEqual(result.returncode,0,result.stderr)

    def test_offline_check_does_not_import_mt5(self):
        script = "import runpy,sys; sys.argv=['run.py','--check']; runpy.run_module('crypto_demo.run',run_name='__main__')"
        # Catch normal SystemExit so modules can be inspected after the CLI finished.
        script = "import runpy,sys\nsys.argv=['run.py','--check']\ntry: runpy.run_module('crypto_demo.run',run_name='__main__')\nexcept SystemExit as exc: assert exc.code == 0\nassert 'MetaTrader5' not in sys.modules\n"
        result = subprocess.run([sys.executable,"-c",script],cwd=Path(base.BASE_DIR).parent,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_account_lock_other_process_and_crash_release(self):
        from crypto_demo.instance_lock import AccountLock
        with tempfile.TemporaryDirectory() as folder:
            script = "from crypto_demo.instance_lock import AccountLock; import os,sys\ntry: AccountLock(1,'server',sys.argv[1]).acquire()\nexcept RuntimeError: sys.exit(23)\nos._exit(0)"
            command = [sys.executable,"-c",script,folder]
            with AccountLock(1,"server",folder):
                self.assertEqual(subprocess.run(command,cwd=Path(base.BASE_DIR).parent).returncode,23)
            self.assertEqual(subprocess.run(command,cwd=Path(base.BASE_DIR).parent).returncode,0)
            with AccountLock(1,"server",folder):
                pass


if __name__ == "__main__":
    unittest.main()
