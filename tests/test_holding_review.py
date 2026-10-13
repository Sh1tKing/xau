"""Holding time is a review checkpoint for autonomous positions, offline only."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import xauusd_fib_volume_pa_ai3 as ai
from strategies import lifecycle
import test_adaptive_ai_integration as integration
import test_profit_guardian as protection
import xauusd_fib_pa_backtest as replay

base = ai.base


class HoldingReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.json"
        scope = patch.dict(base.os.environ, {
            "FIB_PA_STRATEGY_STATE_FILE": str(self.path),
            "FIB_PA_GUARDIAN_STATE_FILE": str(Path(self.temp.name) / "guard.json")})
        scope.start(); self.addCleanup(scope.stop)
        self.trader = base.FibVolumePATrader()
        self.trader.positions_known = True

    def saved(self, adaptive=True, minutes=90, bound=True):
        contract = {"id": "us_adaptive" if adaptive else "session_retest",
                    "analysis_mode": adaptive, "max_hold_minutes": minutes,
                    "cooldown_seconds": 60, "ai_rules": {
                        "guardian": "Python%d分钟期限不可延长。" % minutes}}
        event = "analysis-checkpoint-v1/old" if adaptive else "old-window"
        token = lifecycle.execution_token(event, base.SYMBOL, base.MAGIC, "sell")
        self.assertTrue(self.trader.strategy_ledger.reserve(event, 900., {
            "contract": contract, "execution_token": token, "event_id": event,
            "symbol": base.SYMBOL, "magic": base.MAGIC, "action": "sell",
            "server_utc_offset": 3., "max_hold_minutes": minutes,
            "cooldown_seconds": 60}))
        if bound:
            self.assertTrue(self.trader.strategy_ledger.record_position(123, event, 1000., minutes, 60))
        return SimpleNamespace(ticket=123, identifier=123, symbol=base.SYMBOL,
            magic=base.MAGIC, type=base.mt5.POSITION_TYPE_SELL, comment=token,
            time=11800, price_open=4164.95, sl=4160.63, tp=4151.08, volume=.01,
            profit=9.13)

    def test_expired_existing_autonomous_winner_is_not_closed_or_rewritten(self):
        position = self.saved()
        before = self.trader.strategy_ledger.snapshot()
        raw = self.path.read_bytes()
        with patch.object(base.time, "time", return_value=6401.), \
                patch.object(self.trader, "close_position_by_ticket", return_value=True) as close:
            self.assertEqual(self.trader.guard_strategy_lifecycle([position]), [position])
        close.assert_not_called()
        self.assertEqual(self.trader.strategy_ledger.snapshot(), before)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual((position.sl, position.tp), (4160.63, 4151.08))

    def test_expired_autonomous_loser_is_not_closed_only_for_elapsed_time(self):
        position = self.saved(); position.profit = -2.
        with patch.object(base.time, "time", return_value=6401.), \
                patch.object(self.trader, "close_position_by_ticket", return_value=True) as close:
            self.assertEqual(self.trader.guard_strategy_lifecycle([position]), [position])
        close.assert_not_called()

    def test_non_autonomous_frozen_hard_exit_is_retained(self):
        position = self.saved(adaptive=False, minutes=180)
        with patch.object(base.time, "time", return_value=11800.), \
                patch.object(self.trader, "close_position_by_ticket", return_value=True) as close:
            self.assertEqual(self.trader.guard_strategy_lifecycle([position]), [])
        close.assert_called_once_with(position, "策略最长持有期限到达，Python强制退出")

    def test_recovered_expired_autonomous_receipt_is_bound_without_time_close(self):
        position = self.saved(bound=False)
        with patch.object(base.time, "time", return_value=6401.), \
                patch.object(self.trader, "close_position_by_ticket", return_value=True) as close:
            self.assertEqual(self.trader.guard_strategy_lifecycle([position]), [position])
        close.assert_not_called()
        saved = self.trader.strategy_ledger.snapshot()["positions"]["123"]
        self.assertEqual(saved["opened_at_utc"], 1000.)
        self.assertEqual(saved["max_hold_minutes"], 90)

    def test_review_message_once_per_saved_position(self):
        position = self.saved()
        with patch.object(base.time, "time", return_value=6401.), \
                patch.object(base.logger, "info") as log:
            for _ in range(4): self.trader.guard_strategy_lifecycle([position])
        messages = [call for call in log.call_args_list if "持仓到期复核" in str(call)]
        self.assertEqual(len(messages), 1)

    def test_status_uses_saved_threshold_and_survives_restart(self):
        self.saved(minutes=120)
        original = self.trader.strategy_ledger.snapshot()
        restored = lifecycle.EventLedger(self.path)
        for state in (original, restored.snapshot()):
            before = lifecycle.holding_status(state, 123, 8199.999)
            after = lifecycle.holding_status(state, 123, 8200.)
            self.assertFalse(before["review_due"])
            self.assertTrue(after["review_due"])
            self.assertEqual(after["policy"], "ai_review")
            self.assertEqual(after["threshold_minutes"], 120)
            self.assertEqual(after["threshold_at_utc"], 8200.)
            self.assertFalse(after["time_alone_can_close"])
        self.assertEqual(restored.snapshot(), original)

    def test_policy_uses_original_explicit_analysis_flag_not_strategy_name(self):
        self.assertEqual(lifecycle.holding_policy({"id": "us_adaptive"}), "hard_exit")
        self.assertEqual(lifecycle.holding_policy({"id": "custom", "analysis_mode": True}), "ai_review")
        for value in ("true", 1, False, None):
            self.assertEqual(lifecycle.holding_policy({"analysis_mode": value}), "hard_exit")

    def test_unknown_position_and_invalid_clock_do_not_fabricate_review(self):
        self.saved()
        state = self.trader.strategy_ledger.snapshot()
        self.assertIsNone(lifecycle.holding_status(state, 456, 6401.))
        for clock in (float("nan"), float("inf"), -1., True):
            self.assertIsNone(lifecycle.holding_status(state, 123, clock))


class HoldingReviewAITests(unittest.TestCase):
    def entered(self, profile="us_adaptive", side="sell"):
        other = integration.AdaptiveAITests(); other.setUp()
        self.addCleanup(other.doCleanups)
        other.configure(profile, side); other.run_entry()
        self.assertEqual(len(other.sent), 1, other.snapshots)
        other.now += 5401.; other.tick.time += 5401
        return other

    def test_complete_snapshot_has_review_status_and_original_contract(self):
        for profile in ("asia_adaptive", "london_adaptive", "us_adaptive"):
            with self.subTest(profile=profile):
                other = self.entered(profile)
                saved = copy.deepcopy(other.trader.entry_contexts["123"]["strategy_contract"])
                snapshot = other.trader.collect_ai3_snapshot()
                payload = json.loads(snapshot["prompt"].split("\n", 1)[1])
                item = payload["positions"][0]
                self.assertTrue(item["holding_review"]["review_due"])
                self.assertFalse(item["holding_review"]["time_alone_can_close"])
                self.assertEqual(item["entry_context"]["strategy_contract"], saved)
                self.assertIn("不能仅因持仓时间", snapshot["instructions"])

    def test_expired_hold_decision_does_not_cancel_protected_stop(self):
        other = self.entered()
        position = other.positions[0]
        original = (position.sl, position.tp)
        with patch.object(other.trader, "close_position_by_ticket") as close, \
                patch.object(other.trader, "modify_position_sl") as modify:
            self.assertEqual(other.trader.guard_strategy_lifecycle(other.positions), other.positions)
            other.trader.execute_ai3({"action": "hold", "ticket": 123, "reasoning": "原入场理由有效"})
        close.assert_not_called(); modify.assert_not_called()
        self.assertEqual((position.sl, position.tp), original)

    def test_valid_strategy_exit_still_reaches_existing_execution_checks(self):
        other = self.entered()
        with patch.object(other.trader, "close_position_by_ticket") as close:
            other.trader.execute_ai3({"action": "close", "ticket": 123, "reasoning": "指定M15失效边界已收盘越过"})
        close.assert_called_once_with(other.positions[0], "AI-3: 指定M15失效边界已收盘越过")

    def test_ai3_timeout_after_review_threshold_does_not_force_close(self):
        other = self.entered()
        bar = int(other.tick.time)
        seq = other.trader.claim_task("ai3", bar, 220)
        with patch.object(ai, "call_chat", side_effect=ai.AIRequestError("AI-3", "timeout")), \
                patch.object(other.trader, "alert_api_failure") as alert, \
                patch.object(other.trader, "close_position_by_ticket") as close:
            other.trader.ai3_task(bar, seq)
            self.assertEqual(other.trader.guard_strategy_lifecycle(other.positions), other.positions)
        close.assert_not_called(); alert.assert_called_once()

    def test_bad_ai3_json_after_review_threshold_does_not_force_close(self):
        other = self.entered(); bar = int(other.tick.time)
        seq = other.trader.claim_task("ai3", bar, 220)
        with patch.object(ai, "call_chat", return_value="{incomplete"), \
                patch.object(other.trader, "close_position_by_ticket") as close:
            other.trader.ai3_task(bar, seq)
            self.assertEqual(other.trader.guard_strategy_lifecycle(other.positions), other.positions)
        close.assert_not_called()

    def test_new_contract_declares_review_policy(self):
        for name in ("asia_adaptive", "london_adaptive", "us_adaptive"):
            contract = base.STRATEGIES.snapshot().get(name).manifest()
            self.assertEqual(contract["holding_policy"], "ai_review")
            self.assertIn("复核", contract["ai_rules"]["guardian"])
            self.assertNotIn("期限不可延长", contract["ai_rules"]["guardian"])

    def test_old_saved_prompt_is_overridden_without_rewriting_original_context(self):
        other = self.entered()
        ledger = other.trader.strategy_ledger.snapshot()
        event = ledger["positions"]["123"]["event_id"]
        old = copy.deepcopy(other.trader.entry_contexts["123"]["strategy_contract"])
        old.pop("holding_policy", None)
        old["ai_rules"]["guardian"] = "原契约冻结，Python90分钟期限不可延长。"
        ledger["events"][event]["metadata"]["contract"] = copy.deepcopy(old)
        path = other.directory / "events.json"
        path.write_text(json.dumps(ledger), encoding="utf-8")
        other.trader.strategy_ledger = lifecycle.EventLedger(path)
        other.trader.entry_contexts["123"]["strategy_contract"] = copy.deepcopy(old)
        snapshot = other.trader.collect_ai3_snapshot()
        item = json.loads(snapshot["prompt"].split("\n", 1)[1])["positions"][0]
        self.assertEqual(item["entry_context"]["strategy_contract"], old)
        self.assertEqual(item["holding_review"]["policy"], "ai_review")
        self.assertIn("优先于旧自主契约中的强制期限文字", snapshot["instructions"])
        self.assertEqual(other.trader.strategy_ledger.snapshot(), ledger)


class HoldingReviewProtectionTests(unittest.TestCase):
    def matured(self, sell=False):
        other = protection.ProfitExecutionTests(); other.setUp()
        self.addCleanup(other.doCleanups)
        if sell: other.sell()
        contract = {"analysis_mode": True, "max_hold_minutes": 90, "cooldown_seconds": 60}
        self.assertTrue(other.trader.strategy_ledger.reserve("review", other.now, {
            "contract": contract, "max_hold_minutes": 90, "cooldown_seconds": 60}))
        self.assertTrue(other.trader.strategy_ledger.record_position(123, "review", other.now, 90, 60))
        other.lock(95. if sell else 105.)
        other.now += 5401.
        return other

    def test_real_guardian_preserves_and_advances_both_sides_floor_after_review(self):
        for sell in (False, True):
            with self.subTest(sell=sell):
                other = self.matured(sell)
                original = other.position.sl
                tp = other.position.tp
                with patch.object(other.trader, "close_position_by_ticket") as close:
                    other.poll(96. if sell else 104.)
                    self.assertEqual(other.position.sl, original)
                    other.lock(93. if sell else 107.)
                close.assert_not_called()
                self.assertTrue(base.sl_change_is_tighter("SELL" if sell else "BUY", original, other.position.sl))
                self.assertEqual(other.position.sl, other.state.profit_floor.protected_sl)
                self.assertEqual(other.position.tp, tp)

    def test_removed_stop_is_restored_after_review(self):
        other = self.matured()
        stop = other.position.sl
        other.position.sl = 0.
        with patch.object(other.trader, "close_position_by_ticket") as close:
            other.poll(105.)
        close.assert_not_called()
        self.assertEqual(other.position.sl, stop)

    def test_price_crossing_confirmed_floor_can_still_close_after_review(self):
        other = self.matured()
        with patch.object(other.trader, "close_position_by_ticket", return_value=True) as close:
            other.poll(102.)
        close.assert_called_once()
        self.assertIn("底线", close.call_args.args[1])

    def test_local_replay_does_not_fabricate_an_ai_time_exit(self):
        engine = object.__new__(replay.Backtest)
        engine.t = [6400.]
        engine.position = {"open_ts": 1000., "max_hold_minutes": 90, "holding_policy": "ai_review"}
        self.assertFalse(engine.time_exit_due(0))
        engine.position["holding_policy"] = "hard_exit"
        self.assertTrue(engine.time_exit_due(0))
        engine.position.pop("holding_policy")
        self.assertTrue(engine.time_exit_due(0))


if __name__ == "__main__":
    unittest.main()
