"""Production-policy replay gates using synthetic data; no terminal or AI calls."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import xauusd_fib_pa_backtest as replay

base = replay.base


def market(offset=3, zone="Europe/London", day="2026-07-21"):
    """Late-window confirmation needs more than the old 40 bars for opening ATR."""
    opening = datetime.fromisoformat(day + "T08:00:00").replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    rows = []
    for number in range(-46, 34):
        row = dict(time=int((opening + timedelta(minutes=number * 5)).timestamp() + offset * 3600),
                   open=100., high=101., low=99., close=100., tick_volume=100, spread=20)
        if number == 32:
            row.update(open=100.5, high=102., low=100.4, close=101.8)
        if number == 33:
            row.update(open=101.7, high=102., low=101.1, close=101.6)
        rows.append(row)
    decision = int(rows[-1]["time"] + 300)
    data = {"M5": base.pd.DataFrame(rows)}
    for name, count in (("M15", 100), ("H1", 80)):
        seconds = replay.FRAMES[name][1]
        end = decision // seconds * seconds
        data[name] = base.pd.DataFrame([
            dict(time=end - (count - number) * seconds, open=100., high=101., low=99.,
                 close=100., tick_volume=100, spread=20) for number in range(count)])
    data["M1"] = base.pd.DataFrame([
        dict(time=decision + seconds, open=101.6, high=101.9, low=101.5,
             close=101.7, tick_volume=100, spread=20) for seconds in (0, 60, 10800)])
    # Future/current M5 data may be present in the history but cannot enter the evidence.
    data["M5"] = base.pd.concat([data["M5"], base.pd.DataFrame([
        dict(time=decision, open=104., high=105., low=0., close=104., tick_volume=100, spread=20)])],
        ignore_index=True)
    return data, dict(server_offset=offset, point=.01, contract_size=100, spread_now=20), decision


def proposal(side="buy", event="Europe/London/2026-07-21/480-660", **changes):
    zone, day, _ = event.rsplit("/", 2)
    entry_start = datetime.fromisoformat(day + "T08:30:00").replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    entry_end = datetime.fromisoformat(day + "T11:00:00").replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    confirmation = int((entry_end - timedelta(minutes=15)).timestamp()) + 3 * 3600
    evidence = dict(event_id=event, zone=zone, local_date=day,
                    closed_trigger={"confirmed": True, "timeframe": "M5", "bar_time": confirmation},
                    windows=[dict(zone=zone, local_date=day, active=True,
                                  entry_start_utc=entry_start.isoformat(), entry_end_utc=entry_end.isoformat())])
    contract = dict(id="session_retest", name="欧美时段突破回踩", demo_only=True,
                    one_entry_per_event=True, max_hold_minutes=180, cooldown_seconds=600)
    details = dict(session="london", setup="session_retest", strategy_event_id=event,
                   strategy_contract=contract, entry_guard_atr=2., entry_guard_ema21=101.6,
                   reference_bar_time=confirmation, strategy_assessments={side: {"evidence": evidence}})
    sl, tp = (98., 113.) if side == "buy" else (105., 90.)
    return base.StrategyResult(side, 85, False, "confirmed", changes.get("sl", sl),
                               changes.get("tp", tp), 85, 0, details)


class SessionRetestBacktestTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(base, "MAX_SL_ABS", 8.).start()
        patch.object(base, "MIN_REWARD_RISK", 1.5).start()
        self.data, self.meta, self.decision = market()

    def backtest(self, data=None, meta=None):
        return replay.Backtest(data or self.data, meta or self.meta, 55)

    def evaluate(self, test, stamp):
        # Synthetic flat H1 represents an allowed BUY; session rules and all price guards are real.
        with patch.object(base, "trend_from_ema", return_value="bull"):
            return test.evaluate(stamp)

    def test_explicit_offset_long_window_and_closed_evidence_reach_real_builder(self):
        for offset in (0, 3, 5.5, -4):
            with self.subTest(offset=offset):
                data, meta, decision = market(offset)
                test = self.backtest(data, meta)
                observed = []
                original_gate = test.entry_gate
                def gate(event, stamp):
                    observed.append(stamp)
                    return original_gate(event, stamp)
                test.entry_gate = gate
                result = self.evaluate(test, decision)
                self.assertIsNotNone(result, test.rejects)
                self.assertEqual(result.action, "buy")
                details = result.details
                facts = details["strategy_assessments"]["buy"]["evidence"]
                self.assertEqual(facts["server_offset_hours"], offset)
                self.assertEqual(len(facts["opening_bars"]), 6)
                self.assertEqual(facts["closed_trigger"]["bar_time"], decision - 300)
                self.assertEqual(details["reference_bar_time"], decision - 300)
                self.assertEqual(details["reference_price_source"], "confirmed_closed_m5_close")
                self.assertEqual(float(details["entry_price"]), 101.6)
                self.assertEqual(details["entry_guard_atr"], 2.)
                self.assertEqual(details["entry_guard_ema21"], 100.)
                self.assertEqual(observed, [decision - offset * 3600])
                self.assertNotIn(decision, facts["candidate_bar_times"])
                self.assertLessEqual(result.stop_loss, 99. - base.stop_buffer(2.))
                test.open_position(result, 0, decision)
                self.assertIsNotNone(test.position)
                self.assertEqual(test.position["event_id"], facts["event_id"])
                # The old window loses the history needed for opening-window ATR.
                test.windows["M5"] = replay.Window(data["M5"].iloc[-41:], 300)
                test.consumed_events.clear()
                self.assertIsNone(self.evaluate(test, decision))

    def test_failed_quote_consumes_event_and_builder_blocks_second_attempt(self):
        test = self.backtest()
        result = self.evaluate(test, self.decision)
        self.assertIsNotNone(result)
        test.o[0] = result.stop_loss + .5  # Insufficient trigger-side clearance despite favorable RR.
        test.open_position(result, 0, self.decision)
        self.assertIsNone(test.position)
        event = result.details["strategy_event_id"]
        self.assertIn(event, test.consumed_events)
        test.o[0] = 101.6
        test.open_position(result, 0, self.decision)
        self.assertIsNone(test.position)
        self.assertEqual(test.stats["事件或冷却阻止成交"], 1)
        self.assertIsNone(self.evaluate(test, self.decision))
        self.assertTrue(any("不能重复" in reason for reason in test.rejects))

    def test_cooldown_uses_utc_and_freezes_contract_at_fill(self):
        test = self.backtest()
        result = proposal()
        test.open_position(result, 0, self.decision)
        result.details["strategy_contract"].update(cooldown_seconds=0, max_hold_minutes=1)
        self.assertEqual(test.position["max_hold_minutes"], 180)
        test.close(self.decision + 60, 101.6, "测试平仓")
        utc_close = self.decision + 60 - self.meta["server_offset"] * 3600
        self.assertEqual(test.cooldown_until_utc, utc_close + 600)
        self.assertTrue(test.entry_gate("America/New_York/2026-07-21/480-660", utc_close + 599))
        self.assertEqual(test.entry_gate("America/New_York/2026-07-21/480-660", utc_close + 600), "")
        second = proposal(event="America/New_York/2026-07-21/480-660")
        self.assertNotIn(second.details["strategy_event_id"], test.consumed_events)
        # A genuinely fresh, later New York confirmation is eligible after cooling.
        ny_data, _, ny_decision = market(zone="America/New_York")
        test.windows["M5"] = replay.Window(ny_data["M5"], 300)
        test.t[1] = ny_decision
        test.open_position(second, 1, ny_decision)
        self.assertIsNotNone(test.position)
        self.assertEqual(test.trades[0]["event_id"], result.details["strategy_event_id"])

    def test_builder_cooldown_blocker_uses_decision_datetime_timestamp(self):
        test = self.backtest()
        test.cooldown_until_utc = test.utc_timestamp(self.decision) + 1
        self.assertIsNone(self.evaluate(test, self.decision))
        self.assertTrue(any("冷却" in reason for reason in test.rejects))
        test.cooldown_until_utc = test.utc_timestamp(self.decision)
        self.assertIsNotNone(self.evaluate(test, self.decision))

    def test_time_exit_at_open_ignores_later_intrabar_sl_tp_both_sides(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                test = self.backtest()
                result = proposal(side)
                test.open_position(result, 0, self.decision)
                test.o[2], test.h[2], test.l[2] = 102., 120., 80.
                self.assertTrue(test.check_exit(2))
                self.assertEqual(test.trades[0]["exit_reason"], "策略持仓时限")
                self.assertEqual(test.trades[0]["hold_min"], 180)
                self.assertEqual(test.trades[0]["exit"], 102. if side == "buy" else 102.2)
                self.assertEqual(test.cooldown_until_utc, test.utc_timestamp(test.t[2]) + 600)

    def test_opening_gap_stop_precedes_time_exit_both_sides_and_virtual_stop(self):
        for side, virtual in (("buy", False), ("sell", False), ("buy", True), ("sell", True)):
            with self.subTest(side=side, virtual=virtual):
                test = self.backtest()
                test.open_position(proposal(side), 0, self.decision)
                if virtual:
                    test.position["state"].virtual_sl = 101. if side == "buy" else 102.
                test.o[2] = 97. if side == "buy" else 106.
                self.assertTrue(test.check_exit(2))
                reason = test.trades[0]["exit_reason"]
                self.assertEqual(reason, ("虚拟锁盈线" if virtual else "初始止损") + "(跳空)")
                self.assertEqual(test.trades[0]["exit"], 97. if side == "buy" else 106.2)

    def test_due_exit_in_run_does_not_skip_gap_for_scheduled_structure_failure(self):
        test = self.backtest()
        test.open_position(proposal(), 0, self.decision)
        test.position["exit_next"] = "结构失效"
        test.o[2] = 97.
        # Only replay the expiry bar, with the original frozen opening timestamp.
        for attribute in ("t", "o", "h", "l", "c", "spreads"):
            setattr(test, attribute, [getattr(test, attribute)[2]])
        trades = test.run()
        self.assertEqual(trades[0]["exit_reason"], "初始止损(跳空)")

    def test_before_time_limit_intrabar_stop_still_precedes_target(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                test = self.backtest()
                test.open_position(proposal(side), 0, self.decision)
                test.h[1], test.l[1] = 120., 80.
                self.assertTrue(test.check_exit(1))
                self.assertEqual(test.trades[0]["exit_reason"], "初始止损")
                self.assertEqual(test.trades[0]["exit"], 98. if side == "buy" else 105.)

    def test_fill_guards_reject_max_stop_rr_clearance_and_chase_both_sides(self):
        for side in ("buy", "sell"):
            for category in ("止损距离", "盈亏比", "触发侧", "EMA21"):
                with self.subTest(side=side, category=category):
                    test = self.backtest()
                    result = proposal(side)
                    if category == "止损距离":
                        result.stop_loss = 90. if side == "buy" else 112.
                    elif category == "盈亏比":
                        result.take_profit = 103. if side == "buy" else 100.
                    elif category == "触发侧":
                        result.stop_loss = 101. if side == "buy" else 102.4
                    else:
                        result.details["entry_guard_ema21"] = 90.
                    test.open_position(result, 0, self.decision)
                    self.assertIsNone(test.position)
                    self.assertEqual(test.stats["研究策略成交保护拒绝"], 1)
                    self.assertTrue(any(category in reason for reason in test.rejects), test.rejects)
                    self.assertIn(result.details["strategy_event_id"], test.consumed_events)

    def test_missing_protection_metadata_rejects_without_releasing_event(self):
        test = self.backtest()
        result = proposal()
        del result.details["entry_guard_atr"]
        test.open_position(result, 0, self.decision)
        self.assertIsNone(test.position)
        self.assertIn(result.details["strategy_event_id"], test.consumed_events)

    def test_missing_fill_minute_discards_pending_even_with_valid_quote_or_outside_window(self):
        for delay in (60, 600, 900, 3600):
            with self.subTest(delay=delay):
                original = self.backtest()
                result = self.evaluate(original, self.decision)
                self.assertIsNotNone(result)
                # The price alone would accept this old plan, even at/after local 11:00.
                quote = SimpleNamespace(bid=101.6, ask=101.8)
                self.assertEqual(base.research_execution_error(result, quote), "")
                data = dict(self.data)
                data["M1"] = self.data["M1"].iloc[:1].copy()
                data["M1"]["time"] = self.decision + delay
                test = self.backtest(data)
                test.pending = (result, self.decision)
                trades = test.run()
                self.assertEqual(trades, [])
                self.assertIsNone(test.pending)
                self.assertFalse(test.consumed_events)
                self.assertEqual(test.stats["研究策略信号过期丢弃"], 1)
                self.assertEqual(test.stats["研究策略成交保护拒绝"], 0)

    def test_own_m5_confirmation_must_still_match_latest_closed_bar(self):
        test = self.backtest()
        result = self.evaluate(test, self.decision)
        self.assertIsNotNone(result)
        details = result.details
        details["reference_bar_time"] -= 300
        details["strategy_assessments"]["buy"]["evidence"]["closed_trigger"]["bar_time"] -= 300
        test.open_position(result, 0, self.decision)
        self.assertIsNone(test.position)
        self.assertFalse(test.consumed_events)
        self.assertTrue(any("最新闭合K线" in reason for reason in test.rejects))

    def test_own_event_window_end_is_exclusive_even_when_confirmation_and_quote_look_fresh(self):
        test = self.backtest()
        result = self.evaluate(test, self.decision)
        self.assertIsNotNone(result)
        details = result.details
        facts = details["strategy_assessments"]["buy"]["evidence"]
        own_window = next(window for window in facts["windows"] if window["active"])
        end = int(datetime.fromisoformat(own_window["entry_end_utc"]).timestamp() + self.meta["server_offset"] * 3600)
        # Keep an apparent latest confirmation at the exact end, isolating the window guard.
        frame = self.data["M5"].copy()
        extra = frame.iloc[-1:].copy()
        extra["time"] = end - 300
        frame = base.pd.concat([frame, extra], ignore_index=True)
        test.windows["M5"] = replay.Window(frame, 300)
        details["reference_bar_time"] = end - 300
        facts["closed_trigger"]["bar_time"] = end - 300
        test.t[0] = end
        self.assertEqual(base.research_execution_error(result, SimpleNamespace(bid=test.o[0], ask=test.o[0] + test.spreads[0])), "")
        test.open_position(result, 0, end)
        self.assertIsNone(test.position)
        self.assertFalse(test.consumed_events)
        self.assertTrue(any("原事件时段已过期" in reason for reason in test.rejects))

    def test_missing_confirmation_metadata_drops_signal_before_attempt_reservation(self):
        test = self.backtest()
        result = proposal()
        del result.details["strategy_assessments"]
        test.open_position(result, 0, self.decision)
        self.assertIsNone(test.position)
        self.assertFalse(test.consumed_events)
        self.assertEqual(test.stats["研究策略信号过期丢弃"], 1)

    def test_legacy_strategy_replay_prices_and_exit_semantics_remain_unchanged(self):
        test = self.backtest()
        result = proposal(sl=90., tp=125.)
        result.details = dict(setup="pullback", session="london",
                              strategy_contract={"id": "pullback", "name": "旧策略"})
        test.open_position(result, 0, self.decision - 600)  # Legacy delayed-fill semantics remain.
        self.assertIsNotNone(test.position)  # Existing replay applies RR, not new candidate guards.
        self.assertEqual(test.position["state"].entry_price, 101.8)
        self.assertEqual(test.position["max_hold_minutes"], 0)
        test.l[2], test.h[2] = 89., 126.
        self.assertTrue(test.check_exit(2))
        self.assertEqual(test.trades[0]["exit_reason"], "初始止损")
        self.assertFalse(test.consumed_events)


if __name__ == "__main__":
    unittest.main()
