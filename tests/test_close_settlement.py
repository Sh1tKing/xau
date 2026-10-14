"""Exact broker settlement, delayed history, and truthful closing notifications."""
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_trader as base
import xauusd_fib_volume_pa_ai3 as ai


def deal(ticket, entry, volume=.01, profit=0., price=4154.56, **changes):
    fields = dict(ticket=ticket, position_id=607686331, order=ticket, symbol=base.SYMBOL,
                  type=base.mt5.DEAL_TYPE_SELL if entry == base.mt5.DEAL_ENTRY_IN else base.mt5.DEAL_TYPE_BUY,
                  entry=entry, volume=volume, price=price, profit=profit, commission=0., swap=0., fee=0.,
                  time=10000+ticket, time_msc=(10000+ticket)*1000, reason=base.mt5.DEAL_REASON_TP)
    fields.update(changes)
    return SimpleNamespace(**fields)


class CloseSettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gold-close-test-')
        self.addCleanup(self.temp.cleanup)
        self.account = SimpleNamespace(login=777, server='offline', balance=156.21, equity=156.21)
        self.snapshot = base.PositionSnapshot(607686331, 607686331, base.SYMBOL, 'SELL', .01,
                                             4154.56, 4148.66, 4142.6, 11.94, 20800, 10000., 10020.)
        self.rows = [deal(1, base.mt5.DEAL_ENTRY_IN),
                     deal(2, base.mt5.DEAL_ENTRY_OUT, profit=11.96, price=4142.6)]
        self.history_calls = []
        self.messages = []
        self.trader = self.make_trader()

        def history(*args, **kwargs):
            self.history_calls.append((args, kwargs))
            return self.rows if kwargs.get('position') == 607686331 else ()

        for scope in (patch.object(base.mt5, 'history_deals_get', side_effect=history),
                      patch.object(base.mt5, 'account_info', return_value=self.account),
                      patch.object(base.mt5, 'positions_get', return_value=()),
                      patch.object(base.time, 'time', return_value=10030.),
                      patch.object(base, 'send_feishu_notification', side_effect=lambda title, lines:
                                   self.messages.append((title, lines)) or True)):
            scope.start()
            self.addCleanup(scope.stop)

    def make_trader(self):
        trader = object.__new__(base.FibVolumePATrader)
        trader.account_info = self.account
        trader.position_snapshots = {}
        trader.guardian_states = {}
        trader.notified_close_tickets = set()
        trader.entry_confidence = {607686331: 'AI-1质量分（SELL方案）: 80'}
        trader._close_state_path = str(Path(self.temp.name)/'close_notification_state.json')
        return trader

    def settle(self):
        return self.trader.find_recent_close_deal(self.snapshot.ticket, self.snapshot)

    def text(self):
        return '\n'.join(str(line) for _, lines in self.messages for line in lines)

    def test_broker_clock_ahead_does_not_create_a_date_window_or_miss_the_deal(self):
        result = self.settle()
        self.assertIsNotNone(result)
        self.assertEqual(self.history_calls, [((), {'position':607686331})])
        self.assertAlmostEqual(result.net_profit, 11.96)

    def test_identifier_is_authoritative_when_the_position_ticket_changed(self):
        self.snapshot.ticket = 888
        result = self.settle()
        self.assertIsNotNone(result)
        self.assertEqual(self.history_calls, [((), {'position':607686331})])

    def test_entry_exit_commission_swap_and_fees_are_all_included(self):
        self.rows[0].commission = -.2
        self.rows[0].fee = -.03
        self.rows[1].commission = -.2
        self.rows[1].swap = -.1
        self.rows[1].fee = -.02
        self.assertAlmostEqual(self.settle().net_profit, 11.41)

    def test_partial_closes_and_duplicate_rows_are_aggregated_once(self):
        self.rows = [deal(1, base.mt5.DEAL_ENTRY_IN, volume=.02, commission=-.4),
                     deal(2, base.mt5.DEAL_ENTRY_OUT, profit=3., price=4151.56, commission=-.2),
                     deal(3, base.mt5.DEAL_ENTRY_OUT, profit=11.96, price=4142.6, commission=-.2)]
        self.rows.append(self.rows[-1])
        result = self.settle()
        self.assertAlmostEqual(result.net_profit, 14.16)
        self.assertAlmostEqual(result.volume, .02)
        self.assertAlmostEqual(result.price_close, 4147.08)

    def test_split_opening_fills_are_included_in_price_and_fees(self):
        self.rows = [deal(1, base.mt5.DEAL_ENTRY_IN, volume=.004, price=4154., commission=-.08),
                     deal(2, base.mt5.DEAL_ENTRY_IN, volume=.006, price=4155., commission=-.12),
                     deal(3, base.mt5.DEAL_ENTRY_OUT, price=4142.6, profit=12., commission=-.2)]
        result = self.settle()
        self.assertAlmostEqual(result.price_open, 4154.6)
        self.assertAlmostEqual(result.net_profit, 11.6)

    def test_conflicting_duplicate_or_omitted_known_volume_is_not_final(self):
        original = list(self.rows)
        self.rows.append(deal(2, base.mt5.DEAL_ENTRY_OUT, profit=999., price=4142.6))
        self.assertIsNone(self.settle())
        self.rows = original
        self.snapshot.volume = .02
        self.assertIsNone(self.settle())

    def test_same_symbol_other_position_is_never_used_as_a_fallback(self):
        self.rows = [deal(1, base.mt5.DEAL_ENTRY_IN, position_id=999),
                     deal(2, base.mt5.DEAL_ENTRY_OUT, profit=999., position_id=999)]
        self.assertIsNone(self.settle())

    def test_incomplete_or_invalid_history_does_not_become_a_final_profit(self):
        for rows in (self.rows[:1], self.rows[1:],
                     [deal(1, base.mt5.DEAL_ENTRY_IN, volume=.02), self.rows[1]],
                     [self.rows[0], deal(2, base.mt5.DEAL_ENTRY_OUT, profit=float('nan'))],
                     [self.rows[0], deal(2, base.mt5.DEAL_ENTRY_INOUT, profit=11.96)]):
            with self.subTest(rows=rows):
                self.rows = rows
                self.assertIsNone(self.settle())

    def test_no_history_queues_reconciliation_without_unknown_or_floating_profit(self):
        self.trader.notify_position_closed(self.snapshot.ticket, self.snapshot)
        self.assertFalse(self.messages)
        self.assertIn(self.snapshot.ticket, self.trader.pending_close_notifications)

    def test_disappearance_retries_history_then_sends_realized_profit_once(self):
        complete = self.rows
        self.rows = None
        self.trader.position_snapshots[self.snapshot.ticket] = self.snapshot
        self.trader.sync_position_close_notifications()
        self.assertFalse(self.messages)
        self.assertNotIn(self.snapshot.ticket, self.trader.notified_close_tickets)
        self.rows = complete
        with patch.object(base.time, 'time', return_value=10032.):
            self.trader.sync_position_close_notifications()
            self.trader.sync_position_close_notifications()
        self.assertEqual(len(self.messages), 1)
        self.assertIn('本次净盈亏: +$11.96', self.text())
        self.assertNotIn('11.94', self.text())
        self.assertNotIn('未知', self.text())
        self.assertNotIn('最后浮动盈亏', self.text())

    def test_position_read_error_keeps_snapshots_and_does_not_claim_closed(self):
        self.trader.position_snapshots[self.snapshot.ticket] = self.snapshot
        with patch.object(base.mt5, 'positions_get', return_value=None):
            self.trader.sync_position_close_notifications()
        self.assertIn(self.snapshot.ticket, self.trader.position_snapshots)
        self.assertFalse(self.messages)
        self.assertFalse(self.history_calls)

    def test_restart_restores_pending_and_cached_score_then_settles(self):
        self.trader.notify_position_closed(self.snapshot.ticket, self.snapshot, close_reason='AI-3: close')
        restored = self.make_trader()
        restored.entry_confidence.clear()
        restored.sync_position_close_notifications()
        self.assertEqual(len(self.messages), 1)
        self.assertIn('AI-3: close', self.text())
        self.assertIn('AI-1质量分（SELL方案）: 80', self.text())
        self.assertIn('本次净盈亏: +$11.96', self.text())

    def test_missing_scores_are_omitted_and_renamed_stop_fields_are_truthful(self):
        self.trader.entry_confidence.clear()
        self.trader.notify_position_closed(self.snapshot.ticket, self.snapshot, self.settle())
        self.assertNotIn('未知', self.text())
        self.assertNotIn('开仓评分', self.text())
        self.assertIn('平仓前止损/止盈: 4148.66 / 4142.6', self.text())

    def test_loss_uses_real_deal_fees_even_when_the_cached_floating_profit_was_positive(self):
        self.rows[1].profit = -4.50
        self.rows[1].commission = -.20
        self.trader.notify_position_closed(self.snapshot.ticket, self.snapshot, self.settle())
        self.assertIn('本次净盈亏: -$4.70', self.text())
        self.assertNotIn('11.94', self.text())

    def test_unavailable_or_nonfinite_account_fields_are_omitted(self):
        self.account.balance = float('nan')
        self.account.equity = None
        self.trader.notify_position_closed(self.snapshot.ticket, self.snapshot, self.settle())
        self.assertNotIn('未知', self.text())
        self.assertNotIn('nan', self.text())
        self.assertNotIn('当前余额', self.text())

    def test_feishu_failure_keeps_the_settlement_until_successful_delivery(self):
        self.trader.position_snapshots[self.snapshot.ticket] = self.snapshot
        with patch.object(base, 'FEISHU_NOTIFY_ENABLED', True), \
             patch.object(base, 'FEISHU_WEBHOOK_URL', 'https://offline.invalid'), \
             patch.object(base, 'send_feishu_notification', side_effect=[False, True]) as send:
            self.trader.sync_position_close_notifications()
            self.assertNotIn(self.snapshot.ticket, self.trader.notified_close_tickets)
            with patch.object(base.time, 'time', return_value=10032.):
                self.trader.sync_position_close_notifications()
                self.trader.sync_position_close_notifications()
            self.assertEqual(send.call_count, 2)
            self.assertIn(self.snapshot.ticket, self.trader.notified_close_tickets)

    def test_still_active_partial_position_waits_for_full_settlement(self):
        self.trader.notify_position_closed(self.snapshot.ticket, self.snapshot)
        live = SimpleNamespace(ticket=self.snapshot.ticket, identifier=self.snapshot.identifier,
                               type=base.mt5.POSITION_TYPE_SELL, symbol=base.SYMBOL, volume=.005,
                               price_open=4154.56, sl=4148.66, tp=4142.6, profit=5., time=20800)
        with patch.object(base.mt5, 'positions_get', return_value=(live,)):
            self.trader.sync_position_close_notifications()
        self.assertFalse(self.messages)
        self.assertFalse(self.history_calls)

    def test_account_mismatch_does_not_replay_another_accounts_pending(self):
        self.trader.notify_position_closed(self.snapshot.ticket, self.snapshot)
        restored = self.make_trader()
        restored.account_info = SimpleNamespace(login=999, server='offline')
        restored.sync_position_close_notifications()
        self.assertFalse(self.messages)

    def test_ai_does_not_delete_entry_context_when_position_read_failed(self):
        trader = object.__new__(ai.ThreeAITrader)
        trader.__dict__.update(self.trader.__dict__)
        trader._mt5_lock = threading.RLock()
        trader._state_lock = threading.RLock()
        trader.entry_contexts = {str(self.snapshot.ticket): {'status':'complete'}}
        with patch.object(base.mt5, 'positions_get', return_value=None), \
             patch.object(trader, 'save_entry_contexts') as save:
            trader.sync_position_close_notifications()
        self.assertTrue(trader.entry_contexts)
        save.assert_not_called()

    def test_feishu_delivery_does_not_hold_the_ai_mt5_lock(self):
        trader = object.__new__(ai.ThreeAITrader)
        trader.__dict__.update(self.trader.__dict__)
        trader._mt5_lock = threading.RLock()
        trader._state_lock = threading.RLock()
        trader.entry_contexts = {}
        trader.position_snapshots[self.snapshot.ticket] = self.snapshot
        def sent(title, lines):
            self.assertFalse(trader._mt5_lock._is_owned())
            return True
        with patch.object(base, 'send_feishu_notification', side_effect=sent) as send:
            trader.sync_position_close_notifications()
        send.assert_called_once()


if __name__ == '__main__':
    unittest.main()
