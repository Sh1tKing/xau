"""Latest broker state and monotonic guardian stops; terminal calls are mocked."""
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai

base = ai.base


class GuardianStopExecutionTests(unittest.TestCase):
    def setUp(self):
        self.trader = object.__new__(base.FibVolumePATrader)
        self.position = SimpleNamespace(ticket=123, identifier=456, symbol=base.SYMBOL,
                                        magic=base.MAGIC, type=base.mt5.POSITION_TYPE_BUY,
                                        sl=95., tp=115.)
        self.live = SimpleNamespace(**vars(self.position))
        self.tick = SimpleNamespace(bid=105., ask=105.33)
        self.info = SimpleNamespace(point=.01, digits=2, trade_tick_size=.01,
                                    trade_stops_level=20, trade_freeze_level=10)
        self.requests = []
        self.reply = SimpleNamespace(retcode=base.mt5.TRADE_RETCODE_DONE, comment='ok')

        def send(request):
            self.requests.append(dict(request))
            if self.reply and self.reply.retcode == base.mt5.TRADE_RETCODE_DONE:
                self.live.sl = request['sl']
            return self.reply

        for scope in (patch.object(base, 'ENABLE_TRADING', True),
                      patch.object(base.mt5, 'positions_get', side_effect=lambda **kw:(self.live,)),
                      patch.object(base.mt5, 'symbol_info', side_effect=lambda symbol:self.info),
                      patch.object(base.mt5, 'symbol_info_tick', side_effect=lambda symbol:self.tick),
                      patch.object(base.mt5, 'order_send', side_effect=send)):
            scope.start()
            self.addCleanup(scope.stop)

    def sell(self):
        self.position.type = self.live.type = base.mt5.POSITION_TYPE_SELL
        self.position.sl = self.live.sl = 115.
        self.position.tp = self.live.tp = 95.

    def modify(self, stop):
        return self.trader.modify_position_sl(self.position, stop, 'offline test')

    def test_buy_stale_object_cannot_undo_breakeven_in_same_iteration(self):
        self.assertTrue(self.modify(100.5))
        self.assertFalse(self.modify(99.))
        self.assertEqual(self.live.sl, 100.5)
        self.assertEqual([r['sl'] for r in self.requests], [100.5])

    def test_sell_stale_object_cannot_undo_breakeven(self):
        self.sell()
        self.assertTrue(self.modify(109.))
        self.assertFalse(self.modify(110.))
        self.assertEqual(self.live.sl, 109.)
        self.assertEqual(len(self.requests), 1)

    def test_ai3_and_local_proposals_compare_the_same_latest_stop(self):
        self.live.sl = 101.5  # A previous AI/local update, absent from the old object.
        self.assertFalse(self.modify(101.))
        self.assertTrue(self.modify(102.))
        self.assertEqual([r['sl'] for r in self.requests], [102.])

    def test_same_price_is_not_resent_and_current_tp_is_preserved(self):
        self.live.tp = 116.
        self.assertTrue(self.modify(101.))
        self.assertFalse(self.modify(101.))
        self.assertEqual(self.requests[0]['tp'], 116.)

    def test_disappeared_or_unknown_position_stops_submission(self):
        for result in (None, (), (self.live,self.live)):
            with self.subTest(result=result), patch.object(base.mt5, 'positions_get', return_value=result):
                self.assertFalse(self.modify(101.))
        self.assertFalse(self.requests)

    def test_wrong_symbol_magic_direction_identifier_or_ticket_is_rejected(self):
        for name, value in (('symbol','OTHER'),('magic',base.MAGIC+1),
                            ('type',base.mt5.POSITION_TYPE_SELL),('identifier',789),('ticket',789)):
            with self.subTest(field=name), patch.object(self.live,name,value):
                self.assertFalse(self.modify(101.))
        self.assertFalse(self.requests)

    def test_nonfinite_proposals_and_live_prices_never_reach_broker(self):
        for stop in (float('nan'), float('inf'), -1., 'invalid'):
            self.assertFalse(self.modify(stop))
        for name in ('sl','tp'):
            with patch.object(self.live,name,float('nan')):
                self.assertFalse(self.modify(101.))
        self.assertFalse(self.requests)

    def test_crossed_or_unknown_quote_is_rejected(self):
        for tick in (None, SimpleNamespace(bid=105.,ask=104.),
                     SimpleNamespace(bid=float('inf'),ask=106.)):
            with self.subTest(tick=tick), patch.object(self,'tick',tick):
                self.assertFalse(self.modify(101.))
        self.assertFalse(self.requests)

    def test_broker_stop_distance_uses_buy_bid_and_sell_ask(self):
        self.assertFalse(self.modify(104.80))  # Need .20 stop distance plus one tick.
        self.assertTrue(self.modify(104.79))
        self.sell()
        self.assertFalse(self.modify(105.53))
        self.assertTrue(self.modify(105.54))

    def test_stop_on_wrong_side_is_rejected_without_clamping(self):
        self.assertFalse(self.modify(106.))
        self.sell()
        self.assertFalse(self.modify(105.))
        self.assertFalse(self.requests)

    def test_frozen_current_sl_or_tp_defers_the_whole_modification(self):
        with patch.object(self.live,'sl',104.91):
            self.assertFalse(self.modify(104.95))
        with patch.object(self.live,'tp',105.09):
            self.assertFalse(self.modify(101.))
        self.sell()
        with patch.object(self.live,'sl',105.42):
            self.assertFalse(self.modify(105.40))
        with patch.object(self.live,'tp',105.24):
            self.assertFalse(self.modify(109.))
        self.assertFalse(self.requests)

    def test_freeze_distance_can_exceed_stop_distance(self):
        self.info.trade_freeze_level = 100
        self.assertFalse(self.modify(104.))
        self.assertTrue(self.modify(103.99))

    def test_exact_tick_grid_is_rechecked_after_rounding(self):
        self.info.trade_tick_size = .05
        self.assertTrue(self.modify(101.039))
        self.assertEqual(self.requests[-1]['sl'], 101.)
        self.assertFalse(self.modify(101.049))  # It quantizes to the existing stop.
        self.sell()
        self.assertTrue(self.modify(109.011))
        self.assertEqual(self.requests[-1]['sl'], 109.05)

    def test_missing_broker_limits_do_not_trigger_blind_submission(self):
        for info in (None, SimpleNamespace(point=.01),
                     SimpleNamespace(**{**vars(self.info),'trade_stops_level':float('nan')})):
            with self.subTest(info=info), patch.object(self,'info',info):
                self.assertFalse(self.modify(101.))
        self.assertFalse(self.requests)

    def test_existing_zero_stop_can_be_restored(self):
        self.live.sl = 0.
        self.assertTrue(self.modify(95.))
        self.assertEqual(self.live.sl, 95.)

    def test_broker_failure_is_not_recorded_as_success(self):
        self.reply = SimpleNamespace(retcode=10016,comment='Invalid stops')
        self.assertFalse(self.modify(101.))
        self.assertEqual(self.live.sl, 95.)

    def test_observe_mode_validates_but_never_sends_an_order(self):
        with patch.object(base,'ENABLE_TRADING',False):
            self.assertTrue(self.modify(101.))
        self.assertFalse(self.requests)

    def test_tighter_predicate_rejects_unknown_direction_and_nonfinite_values(self):
        for direction,current,new in (('UNKNOWN',0.,1.),('BUY',95.,float('nan')),
                                      ('BUY',95.,float('inf')),('SELL',float('nan'),100.),
                                      ('BUY',-1.,100.)):
            self.assertFalse(base.sl_change_is_tighter(direction,current,new))


if __name__ == '__main__':
    unittest.main()
