import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from strategies.lifecycle import EventLedger, execution_token, recovery_event


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ledger=EventLedger(Path(self.temp.name)/'state.json')
        self.now=1800000000.

    def reserve(self,event='event',side='buy'):
        token=execution_token(event,'XAUUSD',9,side)
        meta=dict(execution_token=token,symbol='XAUUSD',magic=9,action=side,server_utc_offset=3,
                  max_hold_minutes=120,cooldown_seconds=600,contract={'id':'region_retest'},evidence={'event_id':event})
        self.assertTrue(self.ledger.reserve(event,self.now,meta))
        return token

    def position(self,token,**kwargs):
        values=dict(ticket=5,comment=token,symbol='XAUUSD',magic=9,type=0,time=self.now+10800)
        values.update(kwargs);return SimpleNamespace(**values)

    def test_exact_restore_frozen_metadata_not_nearest_event(self):
        token=self.reserve('original');self.now+=1;self.reserve('neighbor')
        recovered=recovery_event(self.position(token),self.ledger.snapshot(),'XAUUSD',9)
        self.assertEqual(recovered[0],'original')
        self.assertTrue(self.ledger.record_position(5,*recovered[:2],120,600))
        self.assertIsNone(recovery_event(self.position(token),self.ledger.snapshot(),'XAUUSD',9))
        reopened=EventLedger(self.ledger.path)
        self.assertEqual(reopened.snapshot()['positions']['5']['max_hold_minutes'],120)
        self.assertFalse(reopened.record_position(6,'original',self.now,180,600))

    def test_side_symbol_magic_comment_mismatch_stay_unknown(self):
        token=self.reserve()
        for changes in [dict(type=1),dict(symbol='OTHER'),dict(magic=8),dict(comment=token+'x'),dict(comment='FIB_SR_BUY')]:
            with self.subTest(changes=changes):
                self.assertIsNone(recovery_event(self.position(token,**changes),self.ledger.snapshot(),'XAUUSD',9))

    def test_legacy_ambiguous_never_chooses_nearest_missing_side_stays_unknown(self):
        meta=dict(server_utc_offset=3,max_hold_minutes=180,cooldown_seconds=600)
        self.assertTrue(self.ledger.reserve('unknown',self.now,meta))
        p=self.position('FIB_SR_BUY')
        self.assertIsNone(recovery_event(p,self.ledger.snapshot(),'XAUUSD',9))
        meta['action']='buy'
        self.assertTrue(self.ledger.reserve('legacy1',self.now,meta))
        self.assertEqual(recovery_event(p,self.ledger.snapshot(),'XAUUSD',9)[0],'legacy1')
        self.assertTrue(self.ledger.reserve('legacy2',self.now+1,meta))
        self.assertIsNone(recovery_event(p,self.ledger.snapshot(),'XAUUSD',9))

    def test_collision_and_failed_persist_consumption(self):
        token=self.reserve()
        meta=self.ledger.snapshot()['events']['event']['metadata']
        self.assertFalse(self.ledger.reserve('different',self.now,meta))
        self.assertIn('consumed',self.ledger.entry_error('event',self.now))
        self.assertEqual(len(token),23)
        with patch.object(self.ledger,'_persist',return_value=False):
            self.assertFalse(self.ledger.reserve('write_failed',self.now,{}))
        self.assertNotIn('write_failed',self.ledger.snapshot()['events'])

    def test_host_failed_reservation_sends_no_order_and_uncertain_send_consumes(self):
        import xauusd_fib_volume_pa_trader as base
        from test_session_retest_integration import market
        m15,h1,m5,clock=market();now=clock.timestamp()+5
        local=base.build_fib_volume_pa_decision(m15,h1,m5,55,clock,server_utc_offset=3)
        ref=float(local.details['entry_price']);quote=SimpleNamespace(bid=ref,ask=ref+.33,time=int(now+10800))
        with patch.dict(os.environ,{'FIB_PA_STRATEGY_STATE_FILE':str(self.ledger.path)}), \
             patch.object(base.time,'time',return_value=now),patch.object(base,'ENABLE_TRADING',True), \
             patch.object(base,'send_feishu_notification'), \
             patch.object(base.mt5,'account_info',return_value=SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO)), \
             patch.object(base.mt5,'positions_get',return_value=()), \
             patch.object(base.mt5,'symbol_info_tick',return_value=quote), \
             patch.object(base.mt5,'order_send',return_value=None) as send:
            trader=base.FibVolumePATrader()
            with patch.object(trader.strategy_ledger,'reserve',return_value=False):
                self.assertFalse(trader.place_order(local));send.assert_not_called()
            self.assertFalse(trader.place_order(local));self.assertEqual(send.call_count,1)
            self.assertFalse(trader.place_order(local));self.assertEqual(send.call_count,1)
            meta=trader.strategy_ledger.snapshot()['events'][local.details['strategy_event_id']]['metadata']
            self.assertEqual(meta['action'],'buy')
            self.assertEqual(meta['evidence']['entry_event']['event_id'],local.details['strategy_event_id'])
            self.assertEqual(send.call_args[0][0]['comment'],'_SR_'+meta['execution_token'])
            self.assertLessEqual(len(send.call_args[0][0]['comment']),31)

    def test_corrupted_duplicate_token_state_fails_closed(self):
        import copy,json
        token=self.reserve()
        state=self.ledger.snapshot()
        state['events']['forged']=copy.deepcopy(state['events']['event'])
        self.ledger.path.write_text(json.dumps(state),encoding='utf-8')
        reopened=EventLedger(self.ledger.path)
        self.assertTrue(reopened.error)
        self.assertFalse(reopened.reserve('next',self.now,{}))

if __name__=='__main__':unittest.main()
