"""Actual coarse→mock HTTP→JSON quality→AI-2→broker submit→AI-3 paths."""
from contextlib import ExitStack
import copy
from datetime import datetime, timezone
import json
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

    def test_btc_sell_actual_chain(self):
        self.configure("BTC", "sell")
        self.assertEqual(self.run_entry().call_count, 2, self.snapshots)
        self.assertEqual(len(self.sent), 1, self.snapshots)

    def test_eth_buy_actual_chain(self):
        self.configure("ETH", "buy")
        self.assertEqual(self.run_entry().call_count, 2, self.snapshots)
        self.assertEqual(len(self.sent), 1, self.snapshots)

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
