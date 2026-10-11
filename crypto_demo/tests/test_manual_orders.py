import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from crypto_demo import manual_orders as manual
from crypto_demo.tests import test_crypto_integration as fixtures
from crypto_demo.tests import test_crypto_risk as risk_fixtures
from crypto_demo import manual_order as cli

ai, base = fixtures.ai, fixtures.base


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.queue = manual.ManualQueue(self.temp.name, "identity", "ETH", "ETHUSD.mock", 42)
        self.session = "a"*32
        self.now = 1700000000.

    def submit(self, **kwargs):
        self.queue.publish(self.session, self.now)
        return self.queue.submit("buy", 2900., 3300., now=self.now, **kwargs)

    def test_no_owner_stale_wrong_asset_or_mode(self):
        with self.assertRaises(manual.ManualOrderError):
            self.queue.submit("buy", 2900., 3300., now=self.now)
        self.queue.publish(self.session, self.now)
        for changes in ({"now": self.now+31}, {"now": self.now-1}):
            with self.assertRaises(manual.ManualOrderError):
                self.queue.submit("buy", 2900., 3300., **changes)
        for key, value in (("asset", "BTC"), ("mode", "observe"), ("magic", 43), ("account", "other")):
            with self.subTest(key=key):
                owner = dict(version=manual.VERSION, session=self.session, updated_at=self.now,
                             **self.queue.binding)
                owner[key] = value
                manual.write_json(self.queue.directory/"controller.json", owner)
                with self.assertRaises(manual.ManualOrderError):
                    self.queue.submit("buy", 2900., 3300., now=self.now)

    def test_expired_previous_session_and_nonfinite_are_rejected(self):
        request = self.submit()
        for now, session in ((self.now+60, self.session), (self.now, "b"*32)):
            with self.assertRaises(manual.ManualOrderError):
                manual.validate_request(request, self.queue.binding, session, now)
        for key, value in (("sl", float("nan")), ("tp", True), ("volume", float("inf")), ("reason", "")):
            bad = copy.deepcopy(request)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(manual.ManualOrderError):
                manual.validate_request(bad, self.queue.binding, self.session, self.now)

    def test_claim_survives_crash_and_replayed_file_never_executes(self):
        request = self.submit()
        identifier, claimed = self.queue.claim()
        self.assertEqual(identifier, request["request_id"])
        self.assertTrue(claimed.exists())
        restarted = manual.ManualQueue(self.temp.name, **self.queue.binding)
        manual.write_json(self.queue.directory/(identifier+".request.json"), request)
        self.assertIsNone(restarted.claim())
        self.assertEqual(restarted.pending(), [])
        restarted.finish(identifier, "uncertain", "核对历史")
        self.assertEqual(restarted.result(identifier)["status"], "uncertain")

    def test_distinct_requests_and_duplicate_json_fields(self):
        first, second = self.submit(), self.submit()
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(len(self.queue.pending()), 2)
        path = self.queue.directory/"bad.json"
        path.write_text('{"sl":2900,"sl":2950}', encoding="utf-8")
        with self.assertRaises(manual.ManualOrderError):
            manual.read_json(path)

    def test_cli_help_has_no_mt5_import(self):
        path = Path(cli.__file__).resolve()
        code = ("import builtins,runpy,sys; old=builtins.__import__; "
                "builtins.__import__=lambda name,*a,**k: (_ for _ in ()).throw(AssertionError(name)) "
                "if name in ('MetaTrader5','requests') else old(name,*a,**k); "
                "sys.argv=['manual_order.py','--help']; runpy.run_path(script_path,run_name='__main__')")
        # repr is Python source, never a shell interpolation of private configuration.
        result = subprocess.run([sys.executable, "-c", "script_path="+repr(str(path))+"; "+code],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--volume", result.stdout)

    def test_cli_submits_once_and_reports_result_without_broker(self):
        self.queue.publish(self.session)
        def configure(*args):
            return "ETHUSD.mock"
        def finished(identifier):
            self.assertEqual(len(self.queue.pending()), 1)
            return dict(status="filled", message="开仓成功", ticket=123)
        with patch.object(cli, "configure", side_effect=configure), \
             patch.object(cli, "account_id", return_value="identity"), \
             patch.dict(cli.os.environ, {"FIB_PA_AI_MAGIC":"42"}), \
             patch.object(cli, "ManualQueue", return_value=self.queue), \
             patch.object(self.queue, "result", side_effect=finished), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["--asset","ETH","--side","buy","--sl","2900","--tp","3300"]), 0)
        self.assertIn("ticket: 123", output.getvalue())


class ManualIntegrationTests(unittest.TestCase):
    setUp = fixtures.CryptoIntegrationTests.setUp
    configure = fixtures.CryptoIntegrationTests.configure
    send = fixtures.CryptoIntegrationTests.send
    run_ai3_hold = fixtures.CryptoIntegrationTests.run_ai3_hold

    def prepare(self, asset="BTC", side="buy", **kwargs):
        self.configure(asset, side)
        self.queue = manual.ManualQueue(self.dir/"manual", base.account_id(base.MT5_LOGIN, base.MT5_SERVER),
                                       asset, base.SYMBOL, base.MAGIC)
        self.trader.manual_queue = self.queue
        self.queue.publish(self.trader._manual_session, self.now)
        sign = 1 if side == "buy" else -1
        sl = kwargs.pop("sl", self.price-sign*3*self.unit)
        tp = kwargs.pop("tp", self.price+sign*9*self.unit)
        return self.queue.submit(side, sl, tp,
                                 reason="原M5位置支持进场", invalidation="原M5保护结构收盘失效", now=self.now, **kwargs)

    def execute(self):
        with patch.object(ai.requests, "post", side_effect=AssertionError("AI1/2 must not run")) as post:
            self.assertTrue(self.trader.process_manual_requests())
        self.assertEqual(post.call_count, 0)

    def test_both_assets_both_directions_reach_existing_ai3_and_feishu(self):
        # Each subcase has a separate fixture and account/event state.
        for asset in ("BTC", "ETH"):
            for side in ("buy", "sell"):
                with self.subTest(asset=asset, side=side):
                    case = ManualIntegrationTests()
                    case.setUp()
                    try:
                        request = case.prepare(asset, side, volume=.001)
                        case.execute()
                        response = case.queue.result(request["request_id"])
                        self.assertEqual(response["status"], "filled", response)
                        self.assertEqual(len(case.sent), 1)
                        self.assertEqual(case.sent[0]["sl"], request["sl"])
                        self.assertEqual(case.sent[0]["tp"], request["tp"])
                        self.assertEqual(case.sent[0]["volume"], .001)
                        context = case.trader.entry_contexts["123"]
                        self.assertEqual(context["source"], "manual")
                        self.assertIsNone(context["ai_confidence"])
                        self.assertEqual(context["entry_reasoning"], request["reason"])
                        self.assertEqual(case.trader.entry_confidence_line(123), "")
                        notice = " ".join(str(x) for x in base.send_feishu_notification.call_args.args)
                        self.assertIn("开仓成功", notice)
                        self.assertNotIn("手动", notice)
                        self.assertNotIn("置信度", notice)
                        self.assertEqual(case.run_ai3_hold().call_count, 1)
                        prompt = json.loads(case.trader.collect_ai3_snapshot({123})["prompt"].split("\n",1)[1])
                        self.assertEqual(prompt["positions"][0]["entry_context"]["source"], "manual")
                    finally:
                        case.doCleanups()

    def test_inflight_waits_then_expired_command_rejects_without_order(self):
        request = self.prepare()
        self.trader._entry_inflight = True
        self.execute()
        self.assertIsNone(self.queue.result(request["request_id"]))
        self.assertEqual(len(self.queue.pending()), 1)
        self.now += 61
        self.execute()
        response = self.queue.result(request["request_id"])
        self.assertEqual(response["status"], "rejected")
        self.assertIn("expired", response["message"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.queue.pending(), [])
        self.trader._entry_inflight = False
        self.assertFalse(self.trader.process_manual_requests())

    def test_invalid_requested_volume_rejected_but_spread_ratio_does_not_filter_manual(self):
        request = self.prepare(volume=1.)
        self.execute()
        response = self.queue.result(request["request_id"])
        self.assertEqual(response["status"], "rejected")
        self.assertIn("requested volume", response["message"])
        self.assertEqual(self.sent, [])
        request = self.queue.submit("buy", self.price-3*self.unit, self.price+9*self.unit, now=self.now)
        self.tick.ask = self.tick.bid+2*self.unit
        self.execute()
        self.assertEqual(self.queue.result(request["request_id"])["status"], "filled")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["sl"], request["sl"])
        self.assertEqual(self.sent[0]["tp"], request["tp"])

    def test_eth_user_sell_prices_execute_and_existing_ai3_takes_over(self):
        request = self.prepare("ETH", "sell", sl=2503., tp=2490., volume=.01)
        self.tick.bid, self.tick.ask = 2500., 2502.
        for frame in self.frames:
            for key in ("open", "high", "low", "close"):
                frame[key] -= 500.
        self.execute()
        self.assertEqual(self.queue.result(request["request_id"])["status"], "filled")
        self.assertEqual((self.sent[0]["price"], self.sent[0]["sl"], self.sent[0]["tp"], self.sent[0]["volume"]),
                         (2500., 2503., 2490., .01))
        self.assertEqual(self.run_ai3_hold().call_count, 1)

    def future_tick(self):
        self.now += .3754342
        self.stack.enter_context(patch.dict(ai.os.environ, {"CRYPTO_SERVER_UTC_OFFSET_HOURS": "3"}))
        for frame in self.frames:
            frame["time"] += 10800
        self.tick.time = int(self.now)+1+10800
        self.tick.time_msc = self.tick.time*1000+253

    def test_future_quote_waits_then_uses_new_prices_once_without_ai(self):
        request = self.prepare("BTC", "sell", sl=83850., tp=83000., volume=.01)
        self.tick.bid, self.tick.ask = 83741., 83755.
        self.future_tick()
        self.execute()
        self.assertIsNone(self.queue.result(request["request_id"]))
        self.assertTrue((self.queue.directory/(request["request_id"]+".claimed.json")).is_file())
        self.assertEqual(self.sent, [])
        # It is still the original command/TTL, but the market has moved.
        self.now += .8
        self.tick.bid, self.tick.ask = 83762., 83776.
        self.execute()
        self.assertEqual(self.queue.result(request["request_id"])["status"], "filled")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual((self.sent[0]["price"], self.sent[0]["sl"], self.sent[0]["tp"]),
                         (83762., 83850., 83000.))
        self.assertFalse(self.trader.process_manual_requests())
        self.assertEqual(len(self.sent), 1)

    def test_reported_sell_sl_stays_invalid_after_quote_recovers(self):
        request = self.prepare("BTC", "sell", sl=83700., tp=83000., volume=.01)
        self.tick.bid, self.tick.ask = 83741., 83755.
        self.future_tick()
        self.execute()
        self.assertIsNone(self.queue.result(request["request_id"]))
        self.now += .8
        self.execute()
        result = self.queue.result(request["request_id"])
        self.assertEqual(result["status"], "rejected")
        for value in ("SELL", "SL", "Ask", "83700", "83755"):
            self.assertIn(value, result["message"])
        self.assertEqual(self.sent, [])

    def test_quote_wait_expiry_and_stale_crossed_are_never_accepted(self):
        for reason in ("future", "stale", "crossed"):
            case = ManualIntegrationTests()
            case.setUp()
            try:
                request = case.prepare()
                if reason == "future":
                    case.tick.time += 1
                elif reason == "stale":
                    case.tick.time -= 60
                else:
                    case.tick.ask = case.tick.bid-1
                case.execute()
                self.assertIsNone(case.queue.result(request["request_id"]), reason)
                case.now += 60
                case.execute()
                result = case.queue.result(request["request_id"])
                self.assertEqual(result["status"], "rejected")
                self.assertIn("expired", result["message"])
                self.assertEqual(case.sent, [])
                self.assertFalse(case.trader.process_manual_requests())
            finally:
                case.doCleanups()

    def test_quote_observed_after_rate_read_and_expiry_rechecked(self):
        for delay in (2, 61):
            case = ManualIntegrationTests()
            case.setUp()
            try:
                request = case.prepare()
                def rates():
                    case.now += delay
                    case.tick.time = int(case.now)
                    return tuple(case.frames)
                with patch.object(case.trader, "get_context", side_effect=rates):
                    case.execute()
                result = case.queue.result(request["request_id"])
                self.assertEqual(result["status"], "filled" if delay == 2 else "rejected", result)
                self.assertEqual(len(case.sent), 1 if delay == 2 else 0)
                if delay == 61:
                    self.assertIn("expired", result["message"])
            finally:
                case.doCleanups()

    def test_quote_failure_inside_final_preflight_can_wait_before_submission(self):
        request = self.prepare()
        sizing = self.trader.crypto_sizing
        calls = 0
        def sizing_with_clock_change(result, tick):
            nonlocal calls
            calls += 1
            if calls == 3:  # The actual base place_order final sizing stage.
                self.tick.time = int(self.now)+1
            return sizing(result, tick)
        with patch.object(self.trader, "crypto_sizing", side_effect=sizing_with_clock_change):
            self.execute()
            self.assertIsNone(self.queue.result(request["request_id"]))
            self.assertEqual(self.sent, [])
            self.assertEqual(self.trader.strategy_ledger.snapshot()["events"], {})
            self.now += 1.1
            self.execute()
        self.assertEqual(self.queue.result(request["request_id"])["status"], "filled")
        self.assertEqual(len(self.sent), 1)

    def test_waiting_claim_cannot_replay_after_restart_or_account_change(self):
        request = self.prepare()
        self.tick.time += 1
        self.execute()
        self.assertIsNone(self.queue.result(request["request_id"]))
        restarted = ai.ThreeAITrader()
        restarted.manual_queue = self.queue
        self.assertFalse(restarted.process_manual_requests())
        self.assertEqual(self.sent, [])
        self.now += 1.1
        self.acct.trade_mode = 2
        self.execute()
        self.assertEqual(self.queue.result(request["request_id"])["status"], "rejected")
        self.assertEqual(self.sent, [])

    def test_quote_exception_after_broker_send_never_replays(self):
        request = self.prepare()
        with patch.object(base, "send_feishu_notification", side_effect=base.risk.QuoteError("future", {})):
            self.execute()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.queue.result(request["request_id"])["status"], "uncertain")
        self.assertFalse(self.trader.process_manual_requests())
        self.assertEqual(len(self.sent), 1)

    def test_manual_user_plan_bypasses_ema_and_atr_m5_minimum(self):
        request = self.prepare(sl=99999.5, tp=100020., volume=.01)
        for key in ("open", "high", "low", "close"):
            self.frames[0][key] -= 20*self.unit
        self.execute()
        response = self.queue.result(request["request_id"])
        self.assertEqual(response["status"], "filled", response)
        self.assertEqual(self.sent[0]["sl"], request["sl"])
        self.assertEqual(self.sent[0]["tp"], request["tp"])
        self.assertEqual(self.sent[0]["volume"], .01)

    def test_explicit_manual_volume_and_rr_are_user_decisions(self):
        request = self.prepare(sl=96000., tp=101000., volume=.01)
        self.execute()
        response = self.queue.result(request["request_id"])
        self.assertEqual(response["status"], "filled", response)
        self.assertEqual(self.sent[0]["volume"], .01)
        self.assertEqual(self.sent[0]["sl"], request["sl"])
        self.assertEqual(self.sent[0]["tp"], request["tp"])

    def test_manual_broker_price_lattice_stops_and_margin_still_reject(self):
        for change in ("direction", "lattice", "stops", "margin"):
            with self.subTest(change=change):
                case = ManualIntegrationTests()
                case.setUp()
                try:
                    sl = 100020. if change == "direction" else 99400.01 if change == "lattice" else 99400.
                    request = case.prepare(sl=sl, volume=.01)
                    if change == "stops":
                        case.spec.trade_stops_level = 100000
                    elif change == "margin":
                        case.acct.margin_free = .001
                    case.execute()
                    self.assertEqual(case.queue.result(request["request_id"])["status"], "rejected")
                    self.assertEqual(case.sent, [])
                finally:
                    case.doCleanups()

    def test_uncertain_broker_receipt_is_never_replayed_and_context_recovers(self):
        request = self.prepare()
        self.uncertain = True
        self.execute()
        self.assertEqual(self.queue.result(request["request_id"])["status"], "uncertain")
        self.assertEqual(len(self.sent), 1)
        manual.write_json(self.queue.directory/(request["request_id"]+".request.json"), request)
        self.execute()
        self.assertEqual(len(self.sent), 1)
        # Delayed actual position becomes visible; original context is recovered.
        self.uncertain = False
        self.send(self.sent[0])
        self.trader.entry_contexts = {}
        self.trader.restore_entry_contexts(self.positions)
        saved = self.trader.entry_contexts["123"]
        self.assertEqual(saved["source"], "manual")
        self.assertEqual(saved["entry_reasoning"], request["reason"])
        self.assertEqual(saved["initial_sl"], request["sl"])
        self.assertEqual(saved["strategy_contract"]["max_hold_minutes"], 90)

    def test_non_demo_and_manual_intent_tampering_cannot_submit(self):
        request = self.prepare()
        with patch.object(base, "MODE", "dry-run"):
            self.assertFalse(self.trader.process_manual_requests())
        self.trader._active_manual_request = copy.deepcopy(request)
        result = manual.prepare_result(base, self.trader, request)
        result.details["manual_volume"] = 1.
        self.assertIn("改变", self.trader.validate_strategy_entry(result))
        self.assertFalse(self.trader.place_order(result))
        self.assertEqual(self.sent, [])

    def test_prior_session_and_broken_durable_state_no_order(self):
        request = self.prepare()
        self.trader._manual_session = "b"*32
        self.execute()
        self.assertEqual(self.queue.result(request["request_id"])["status"], "rejected")
        self.assertEqual(self.sent, [])
        request = self.queue.submit("buy", self.price-3*self.unit, self.price+9*self.unit, now=self.now)
        with patch.object(self.trader.strategy_ledger, "reserve", return_value=False):
            self.execute()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.queue.result(request["request_id"])["status"], "rejected")

    def test_manual_h1_m5_are_context_but_account_and_clock_still_guard(self):
        for change in ("h1", "m5", "account", "tick"):
            with self.subTest(change=change):
                case = ManualIntegrationTests()
                case.setUp()
                try:
                    request = case.prepare()
                    if change == "h1":
                        frame = case.frames[1]
                        for key in ("open", "high", "low", "close"):
                            frame[key] = frame[key].iloc[::-1].to_numpy()
                    elif change == "m5":
                        case.frames[2].loc[79, "low"] = request["sl"]
                    elif change == "account":
                        case.acct.trade_mode = 2
                    else:
                        case.tick.time -= 60
                        case.tick.time_msc -= 60000
                    case.execute()
                    if change == "tick":
                        self.assertIsNone(case.queue.result(request["request_id"]))
                        case.now += 61
                        case.execute()
                    self.assertEqual(case.queue.result(request["request_id"])["status"],
                                     "filled" if change in ("h1", "m5") else "rejected")
                    self.assertEqual(len(case.sent), 1 if change in ("h1", "m5") else 0)
                finally:
                    case.doCleanups()

    def test_original_rules_survive_definition_reload_restart_and_close_notice(self):
        request = self.prepare()
        self.execute()
        original = copy.deepcopy(self.trader.entry_contexts["123"]["strategy_contract"])
        temp_defs = self.dir/"defs"
        temp_defs.mkdir()
        source = Path(base.BASE_DIR)/"strategies/definitions/btc/adaptive.py"
        (temp_defs/"adaptive.py").write_text(source.read_text(encoding="utf-8").replace(
            "max_hold_minutes=90", "max_hold_minutes=180"), encoding="utf-8")
        with patch.object(base, "STRATEGIES", fixtures.Registry(temp_defs)):
            self.assertEqual(base.STRATEGIES.snapshot().get("btc_adaptive").max_hold_minutes, 180)
            restarted = ai.ThreeAITrader()
            restarted.entry_contexts = {}
            restarted.restore_entry_contexts(self.positions)
            prompt = json.loads(restarted.collect_ai3_snapshot({123})["prompt"].split("\n",1)[1])
            saved = prompt["positions"][0]["entry_context"]
            self.assertEqual(saved["strategy_contract"], original)
            self.assertEqual(saved["entry_reasoning"], request["reason"])
            self.assertEqual(restarted.strategy_ledger.snapshot()["positions"]["123"]["max_hold_minutes"], 90)
            snapshot = restarted.create_position_snapshot(self.positions[0])
            restarted.notify_position_closed(123, snapshot, close_reason="AI-3 原结构失效")
            notice = " ".join(str(x) for x in base.send_feishu_notification.call_args.args)
            self.assertIn("平仓", notice)
            self.assertNotIn("手动", notice)
            self.assertNotIn("开仓置信度", notice)

    def test_main_loop_consumes_before_new_ai_entry_and_dispatches_existing_guardian(self):
        self.prepare()
        with patch.object(self.trader, "connect_mt5", return_value=True), \
             patch.object(ai, "attach_ai_logger", return_value="offline"), \
             patch.object(self.trader, "notify_lines"), \
             patch.object(self.trader, "run_guardian") as guardian, \
             patch.object(self.trader, "latest_bar_time", return_value=int(self.now)), \
             patch.object(self.trader, "entry_clock_ready", return_value=True), \
             patch.object(self.trader, "dispatch_entry") as entry, \
             patch.object(self.trader, "dispatch_m5_entry") as m5_entry, \
             patch.object(self.trader, "dispatch_ai3") as ai3, \
             patch.object(self.trader._stop, "is_set", side_effect=[False, False, True]), \
             patch.object(base.time, "sleep"), patch.object(base.mt5, "shutdown"):
            self.assertIsNone(self.trader.run())
        self.assertEqual(len(self.sent), 1)
        entry.assert_not_called()
        m5_entry.assert_not_called()
        guardian.assert_called_once()
        ai3.assert_called_once()

    def test_quote_wait_does_not_block_guardian_or_start_automatic_entry(self):
        request = self.prepare()
        self.tick.time += 1
        with patch.object(self.trader, "connect_mt5", return_value=True), \
             patch.object(ai, "attach_ai_logger", return_value="offline"), \
             patch.object(self.trader, "notify_lines"), \
             patch.object(self.trader, "run_guardian") as guardian, \
             patch.object(self.trader, "latest_bar_time", return_value=int(self.now)), \
             patch.object(self.trader, "entry_clock_ready", return_value=True), \
             patch.object(self.trader, "dispatch_entry") as entry, \
             patch.object(self.trader, "dispatch_m5_entry") as m5_entry, \
             patch.object(self.trader, "dispatch_ai3") as ai3, \
             patch.object(self.trader._stop, "is_set", side_effect=[False, False, True]), \
             patch.object(base.time, "sleep"), patch.object(base.mt5, "shutdown"):
            self.assertIsNone(self.trader.run())
        self.assertIsNone(self.queue.result(request["request_id"]))
        self.assertEqual(self.sent, [])
        entry.assert_not_called()
        m5_entry.assert_not_called()
        guardian.assert_called_once()
        ai3.assert_called_once()


class RequestedVolumeTests(unittest.TestCase):
    sizing = risk_fixtures.CashRiskTests.sizing

    def test_smaller_requested_volume_exact_and_all_limits_retained(self):
        self.assertEqual(self.sizing(requested_volume=.005)["volume"], .005)
        for value in (1., .0001, .0055, float("nan"), True):
            with self.subTest(value=value), self.assertRaises(risk_fixtures.risk.RiskError):
                self.sizing(requested_volume=value)
