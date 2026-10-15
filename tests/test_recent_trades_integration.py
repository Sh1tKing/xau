"""Exact host callbacks via AST extraction: no module startup, env, MT5 or HTTP.

Extracting the unchanged methods avoids the trader module's import-time logger
and configuration loading; fake broker objects only provide local history facts.
"""
import ast
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import math
from numbers import Real
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from strategies import recent_trades
from strategies.settlement import PositionSettlement, SettlementError, settle_position_deals


NOW = 1800000000.
TICKET = 71001


def host_types(directory, account, broker, clock, notify):
    source = Path(__file__).resolve().parents[1] / "xauusd_fib_volume_pa_trader.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    names = {"_recent_namespace", "_prepare_recent_trades", "recent_trade_context",
             "record_recent_settlement", "position_entry_facts", "create_position_snapshot",
             "_prepare_close_notifications", "_load_close_notifications", "_save_close_notifications",
             "notify_position_closed", "find_recent_close_deal", "sync_position_close_notifications",
             "get_positions", "deal_reason_text"}
    snapshot = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PositionSnapshot")
    trader = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FibVolumePATrader")
    trader.body = [node for node in trader.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                             snapshot, trader], type_ignores=[])
    scope = dict(dataclass=dataclass, field=field, asdict=asdict, recent_trades=recent_trades,
                 threading=threading, os=os, json=json, tempfile=tempfile, math=math,
                 Real=Real, time=clock, mt5=broker, SYMBOL="XAUUSD", MAGIC=777,
                 tick_clock_offset=lambda tick, observed: getattr(tick, "offset", None) if tick is not None else None,
                 PositionSettlement=PositionSettlement, settle_position_deals=settle_position_deals,
                 SettlementError=SettlementError, logger=Mock(),
                 CLOSE_SETTLEMENT_VERSION="close-settlement-v1", send_feishu_notification=notify,
                 FEISHU_NOTIFY_ENABLED=True, FEISHU_WEBHOOK_URL="offline-only",
                 format_signed_money=lambda value: f"{value:+.2f}",
                 account_balance_lines=lambda **kwargs: [], datetime=datetime)
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), scope)
    instance = scope["FibVolumePATrader"]()
    instance.account_info = account
    instance.position_snapshots, instance.guardian_states = {}, {}
    instance.notified_close_tickets, instance.entry_confidence = set(), {}
    instance._close_state_path = str(Path(directory) / "close_notification_state.json")
    instance._close_notification_lock = threading.RLock()
    instance._recent_trade_lock = threading.RLock()
    instance._recent_trade_path = str(Path(directory) / "recent_settled_trades.json")
    instance._recent_trade_state, instance._recent_trade_error = None, ""
    instance.entry_contexts = {}
    return instance, scope["PositionSnapshot"]


class RecentTradesIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recent-settlement-host-")
        self.addCleanup(self.temp.cleanup)
        self.account = SimpleNamespace(login=2345, server="isolated-demo")
        self.broker = Mock(POSITION_TYPE_BUY=0, POSITION_TYPE_SELL=1,
                           DEAL_REASON_SL=4, DEAL_REASON_TP=5, DEAL_REASON_SO=6,
                           DEAL_REASON_EXPERT=3, DEAL_REASON_CLIENT=0,
                           DEAL_REASON_MOBILE=1, DEAL_REASON_WEB=2)
        self.broker.account_info.return_value = self.account
        self.broker.symbol_info_tick.return_value = SimpleNamespace(offset=3)
        self.broker.positions_get.return_value = ()
        self.clock = SimpleNamespace(time=Mock(return_value=NOW))
        self.notify = Mock(return_value=True)
        self.trader, self.Snapshot = self.make()
        self.snapshot = self.Snapshot(TICKET, TICKET, "XAUUSD", "BUY", .01,
                                     4100., 4097., 4110., 999., 123, NOW-100, NOW-1,
                                     server_utc_offset=3,
                                     entry_context={"route": "m15_tactical", "h1_background": "bear"},
                                     settlement_namespace=recent_trades.namespace(
                                         self.account.login, self.account.server, "XAUUSD", 777))
        self.settled = PositionSettlement(TICKET, (90001, 90002), 4., -.2, -.1, -.05,
                                          .01, 4100., 4104., 5, int(NOW+10800-10))
        self.addCleanup(lambda: self.broker.order_send.assert_not_called())
        self.addCleanup(lambda: self.broker.initialize.assert_not_called())

    def make(self):
        return host_types(self.temp.name, self.account, self.broker, self.clock, self.notify)

    def cache(self):
        return self.trader.recent_trade_context()

    def test_complete_notification_callback_records_real_fees_once(self):
        self.assertTrue(self.trader.notify_position_closed(TICKET, self.snapshot, self.settled))
        row = self.cache()["records"][0]
        self.assertAlmostEqual(row["net_profit"], 3.65)
        self.assertEqual(row["price_open"], 4100.)
        self.assertEqual(row["price_close"], 4104.)
        self.assertEqual(row["close_utc"], NOW-10)
        self.assertEqual(row["entry_context"], self.snapshot.entry_context)
        self.assertNotEqual(row["net_profit"], self.snapshot.profit)
        self.assertEqual(row["reason_code"], 5)

    def test_retry_with_new_observation_keeps_original_identity_and_time(self):
        self.notify.return_value = False
        self.trader.notify_position_closed(TICKET, self.snapshot, self.settled)
        first = json.loads(Path(self.trader._recent_trade_path).read_text(encoding="utf-8"))
        self.clock.time.return_value = NOW+20
        self.trader.notify_position_closed(TICKET, self.snapshot, self.settled)
        self.assertEqual(first, json.loads(Path(self.trader._recent_trade_path).read_text(encoding="utf-8")))
        self.assertEqual(len(self.cache()["records"]), 1)

    def test_unknown_or_future_conversion_excluded_without_observation_substitution(self):
        for offset in (None, True, float("nan"), 15, -3):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as path:
                trader, _ = host_types(path, self.account, self.broker, self.clock, self.notify)
                self.snapshot.server_utc_offset = offset
                trader.record_recent_settlement(self.snapshot, self.settled)
                context = trader.recent_trade_context()
                self.assertEqual(context["records"], [])
                self.assertEqual(context["coverage"]["unknown_close_utc_records"], 1)

    def test_changed_broker_offset_does_not_claim_entry_offset_as_close_utc(self):
        self.broker.symbol_info_tick.return_value = SimpleNamespace(offset=2)
        self.trader.record_recent_settlement(self.snapshot, self.settled)
        context = self.cache()
        self.assertEqual(context["records"], [])
        self.assertEqual(context["coverage"]["unknown_close_utc_records"], 1)
        state = json.loads(Path(self.trader._recent_trade_path).read_text(encoding="utf-8"))
        row = next(iter(state["partitions"].values()))["records"][0]
        self.assertIsNone(row["close_utc"])
        self.assertEqual(row["broker_close_time"], self.settled.time)
        self.assertAlmostEqual(row["net_profit"], self.settled.net_profit)

    def test_old_positional_and_json_snapshots_have_unknown_utc_defaults(self):
        old = self.Snapshot(TICKET, TICKET, "XAUUSD", "BUY", .01, 4100., 4097.,
                            4110., 999., 123, NOW-100, NOW-1)
        restored = self.Snapshot(**{key: value for key, value in asdict(old).items()
                                   if key not in ("server_utc_offset", "entry_context", "settlement_namespace")})
        self.assertIsNone(restored.server_utc_offset)
        self.assertEqual(restored.entry_context, {})
        self.trader.record_recent_settlement(restored, self.settled)
        self.assertEqual(self.cache()["records"], [])

    def test_partial_or_wrong_identifier_never_reaches_cache_callback(self):
        self.assertFalse(self.trader.notify_position_closed(TICKET, self.snapshot, {}))
        other = PositionSettlement(TICKET+1, (90001, 90002), 4., 0., 0., 0.,
                                   .01, 4100., 4104., 5, int(NOW))
        self.assertFalse(self.trader.notify_position_closed(TICKET, self.snapshot, other))
        self.assertFalse(Path(self.trader._recent_trade_path).exists())
        self.assertEqual(self.notify.call_count, 0)

    def test_corrupt_cache_soft_fails_but_notification_continues(self):
        Path(self.trader._recent_trade_path).write_text('{"broken":true}', encoding="utf-8")
        self.assertTrue(self.trader.notify_position_closed(TICKET, self.snapshot, self.settled))
        self.assertTrue(self.notify.called)
        context = self.cache()
        self.assertFalse(context["available"])
        self.assertEqual(context["records"], [])
        self.assertEqual(json.loads(Path(self.trader._recent_trade_path).read_text()), {"broken": True})

    def test_replace_failure_preserves_old_file_and_cleans_temporary(self):
        self.trader.record_recent_settlement(self.snapshot, self.settled)
        cachepath = Path(self.trader._recent_trade_path)
        before, state = cachepath.read_bytes(), deepcopy(self.trader._recent_trade_state)
        self.clock.time.return_value = NOW+1
        with patch.object(os, "replace", side_effect=OSError("isolated persistence failure")):
            self.trader.record_recent_settlement(self.snapshot, self.settled)
        self.assertEqual(cachepath.read_bytes(), before)
        self.assertEqual(self.trader._recent_trade_state, state)
        self.assertEqual(self.trader._recent_trade_error, "cache_write_failed")
        self.assertEqual(sorted(path.name for path in Path(self.temp.name).iterdir()), [cachepath.name])

    def test_pending_restart_keeps_original_context_after_ledger_removal(self):
        metadata = {"server_utc_offset": 3,
                    "contract": {"id": "asia_adaptive", "direction_policy": {"version": "h1-or-m15-structure-v1"}},
                    "evidence": {"direction_context": {"h1_background": "bear"},
                                 "direction_permission": {"route": "m15_tactical", "structure_event_id": "event-a",
                                                          "anchor": {"kind": "local_pullback"}, "reason": "original structure"}}}
        ledger = {"positions": {str(TICKET): {"event_id": "entry-event"}},
                  "events": {"entry-event": {"metadata": metadata}}}
        self.trader.strategy_ledger = SimpleNamespace(snapshot=lambda: deepcopy(ledger))
        position = SimpleNamespace(ticket=TICKET, identifier=TICKET, symbol="XAUUSD", type=0,
                                   volume=.01, price_open=4100., sl=4097., tp=4110., profit=999., time=123, magic=777)
        snapshot = self.trader.create_position_snapshot(position)
        self.trader.notify_position_closed(TICKET, snapshot)
        ledger.clear()
        restored, _ = self.make()
        restored._prepare_close_notifications()
        pending = restored.pending_close_notifications[TICKET]
        restored_snapshot = self.Snapshot(**pending["snapshot"])
        self.assertEqual(restored_snapshot.entry_context["route"], "m15_tactical")
        self.assertEqual(restored_snapshot.entry_context["structure_event_id"], "event-a")
        self.assertEqual(restored_snapshot.server_utc_offset, 3)
        restored.notify_position_closed(TICKET, restored_snapshot, self.settled)
        self.assertEqual(restored.recent_trade_context()["records"][0]["entry_context"]["route"], "m15_tactical")

    def test_cache_restart_retains_original_coverage_and_record(self):
        self.trader.record_recent_settlement(self.snapshot, self.settled)
        first = self.cache()
        self.clock.time.return_value = NOW+30
        restored, _ = self.make()
        result = restored.recent_trade_context()
        self.assertEqual(first["records"], result["records"])
        self.assertEqual(first["coverage"]["started_utc"], result["coverage"]["started_utc"])

    def test_account_switch_cannot_relabel_pending_old_account_settlement(self):
        self.broker.account_info.return_value = SimpleNamespace(login=9999, server="different-demo")
        self.trader.record_recent_settlement(self.snapshot, self.settled)
        self.assertFalse(Path(self.trader._recent_trade_path).exists(),
                         "old snapshot must not be written into the current different account namespace")

    def test_foreign_magic_or_symbol_not_relabelled_as_this_system(self):
        for symbol, magic in (("XAUUSD", 999), ("XAUUSD", None), ("EURUSD", 777)):
            position = SimpleNamespace(ticket=TICKET, identifier=TICKET, symbol=symbol, type=0,
                                       volume=.01, price_open=4100., sl=4097., tp=4110., profit=999., time=123)
            if magic is not None:
                position.magic = magic
            snapshot = self.trader.create_position_snapshot(position)
            self.trader.record_recent_settlement(snapshot, self.settled)
            self.assertFalse(Path(self.trader._recent_trade_path).exists(), (symbol, magic))

    def test_partial_history_waits_and_only_complete_history_is_cached(self):
        def row(ticket, entry, volume, price, profit=0., commission=-.2, swap=0., fee=-.02):
            return SimpleNamespace(ticket=ticket, position_id=TICKET, symbol="XAUUSD", type=entry,
                                   entry=entry, volume=volume, price=price, profit=profit,
                                   commission=commission, swap=swap, fee=fee, reason=5,
                                   time=int(NOW+10800-10+entry), time_msc=int((NOW+10800-10+entry)*1000))
        opening = row(90001, 0, .01, 4100.)
        partial = row(90002, 1, .004, 4104., profit=1.)
        self.broker.history_deals_get.return_value = [opening, partial]
        self.trader.notify_position_closed(TICKET, self.snapshot)
        self.trader.sync_position_close_notifications()
        self.assertEqual(self.notify.call_count, 0)
        self.assertFalse(Path(self.trader._recent_trade_path).exists())
        final = row(90003, 1, .006, 4105., profit=3., commission=-.1, swap=-.1, fee=-.01)
        self.broker.history_deals_get.return_value = [opening, partial, final]
        self.clock.time.return_value = NOW+3
        self.trader.sync_position_close_notifications()
        context = self.cache()
        self.assertEqual(len(context["records"]), 1)
        actual = context["records"][0]
        self.assertAlmostEqual(actual["net_profit"], 3.35)
        self.assertAlmostEqual(actual["price_close"], 4104.6)
        self.assertEqual(actual["deal_ids"], [90001, 90002, 90003])
        self.assertEqual(self.broker.history_deals_get.call_args.kwargs, {"position": TICKET})

    def test_load_and_record_initialization_hold_shared_lock(self):
        observed = []
        real_prepare = self.trader._prepare_recent_trades
        def prepare(*args):
            observed.append(self.trader._recent_trade_lock._is_owned())
            return real_prepare(*args)
        with patch.object(self.trader, "_prepare_recent_trades", side_effect=prepare):
            self.trader.recent_trade_context()
            self.trader.record_recent_settlement(self.snapshot, self.settled)
        self.assertTrue(observed)
        self.assertTrue(all(observed))


if __name__ == "__main__":
    unittest.main()
