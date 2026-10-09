"""Offline frozen-plan latency and cost behavior; no broker calls."""
import copy
import unittest
from unittest.mock import patch
import test_session_retest_backtest as old
import xauusd_fib_pa_backtest as replay
from strategies.events import make_entry_event

class IntradayReplayTests(unittest.TestCase):
    def setUp(self):
        self.data,self.meta,self.ts=old.market()
    def test_override_isolated_and_unknown_rejected(self):
        original=replay.base.STRATEGIES.snapshot()
        test=replay.Backtest(self.data,self.meta,55,strategies='region_retest,region_rejection')
        self.assertEqual({s.id for s in test.strategy_snapshot.strategies if s.enabled},{'region_retest','region_rejection'})
        self.assertEqual(original,replay.base.STRATEGIES.snapshot())
        with self.assertRaises(ValueError): replay.Backtest(self.data,self.meta,55,strategies='unknown')
        with self.assertRaises(ValueError): replay.Backtest(self.data,self.meta,55,entry_delay_seconds=61)
        with self.assertRaises(ValueError): replay.Backtest(self.data,self.meta,55,spread_multiplier=float('nan'))
    def test_delayed_real_price_fixed_plan_and_cost(self):
        self.data['M1'].loc[1,'open']=102.
        test=replay.Backtest(self.data,self.meta,55,entry_delay_seconds=60,spread_multiplier=2)
        result=old.proposal(); frozen=copy.deepcopy(result)
        test.open_position(result,1,self.ts)
        self.assertIsNotNone(test.position,test.rejects)
        self.assertAlmostEqual(test.position['state'].entry_price,102.4)
        self.assertEqual(result,frozen)
        self.assertEqual(test.position['sl'],98.)
        self.assertEqual(test.position['signal_ts'],self.ts)
    def test_exact_missing_delayed_minute_and_new_bar(self):
        test=replay.Backtest(self.data,self.meta,55,entry_delay_seconds=120)
        self.assertIn('M1',test.research_signal_error(old.proposal(),1,self.ts))
        test=replay.Backtest(self.data,self.meta,55,entry_delay_seconds=10800)
        self.assertTrue(test.research_signal_error(old.proposal(),2,self.ts))
    def test_failed_quote_consumes_event_single_position(self):
        test=replay.Backtest(self.data,self.meta,55)
        result=old.proposal(sl=101.5)
        test.open_position(result,0,self.ts)
        self.assertIsNone(test.position)
        self.assertIn(result.details['strategy_event_id'],test.consumed_events)
        test.open_position(old.proposal(),0,self.ts)
        self.assertIsNone(test.position)
    def generic(self, side='buy'):
        result=old.proposal(side)
        evidence=result.details['strategy_assessments'][side]['evidence']
        raw=self.ts-300
        event=make_entry_event(event_id='source/episode',region_id='pivot/1',episode_start_bar_time=raw,
            action=side,trigger_timeframe='M5',trigger_bar_time=raw,
            trigger_close_utc=self.ts-10800,valid_until_utc=self.ts-10800+300,
            source_refs=[dict(timeframe='M5',bar_time=raw)]).to_dict()
        evidence.clear(); evidence.update(event_id=event['event_id'],action=side,server_offset_hours=3,
            closed_trigger=dict(confirmed=True,timeframe='M5',bar_time=raw,action=side),entry_event=event,
            host_verified_entry_event=dict(entry_event=copy.deepcopy(event),event_id=event['event_id'],action=side,server_offset_hours=3))
        result.details['strategy_event_id']=event['event_id']
        result.details['strategy_contract']['id']='generic_research'
        return result
    def test_generic_source_without_session_fields_and_binding(self):
        test=replay.Backtest(self.data,self.meta,55,entry_delay_seconds=60)
        result=self.generic()
        self.assertEqual(test.research_signal_error(result,1,self.ts),'')
        test.open_position(result,1,self.ts)
        self.assertIsNotNone(test.position)
        test.close(self.ts+120,102.,'test')
        self.assertIn('冷却',test.entry_gate('another',test.utc_timestamp(self.ts+120)))
        tampered=self.generic(); tampered.details['strategy_assessments']['buy']['evidence']['entry_event']['region_id']='forged'
        self.assertIn('绑定',test.research_signal_error(tampered,1,self.ts))
    def test_generic_source_missing_and_new_closed_context(self):
        result=self.generic()
        test=replay.Backtest(self.data,self.meta,55,entry_delay_seconds=60)
        test.windows['M5']=replay.Window(self.data['M5'].iloc[-1:].copy(),300)
        self.assertTrue(test.research_signal_error(result,1,self.ts))
        data=copy.deepcopy(self.data)
        data['M1'].loc[1,'time']=self.ts+300
        test=replay.Backtest(data,self.meta,55,entry_delay_seconds=300)
        self.assertTrue(test.research_signal_error(result,1,self.ts))
        data=copy.deepcopy(self.data)
        added=data['H1'].iloc[-1:].copy();added['time']=self.ts+60-3600
        # Synthetic fill occurs at the newly closed H1 boundary.
        test=replay.Backtest(data,self.meta,55,entry_delay_seconds=60)
        test.windows['H1']=replay.Window(replay.pd.concat([data['H1'],added]),3600)
        self.assertIn('新闭合H1',test.research_signal_error(result,1,self.ts))
    def test_sell_ask_exit_and_guardian_use_scaled_cost(self):
        test=replay.Backtest(self.data,self.meta,55,spread_multiplier=2)
        result=old.proposal('sell')
        test.open_position(result,0,self.ts)
        self.assertIsNotNone(test.position)
        self.assertEqual(test.position['state'].entry_price,101.6)
        test.l[0]=100.
        test.guardian_context=lambda ts:(None,2.)
        test.update_guardian(0)
        self.assertAlmostEqual(test.position['state'].lowest_price,100.4)
        test.position['max_hold_minutes']=1
        test.check_exit(1)
        self.assertAlmostEqual(test.trades[-1]['exit'],102.)
        self.assertEqual(test.trades[-1]['exit_reason'],'策略持仓时限')
    def test_pending_waits_exact_minute_and_does_not_rebuild_plan(self):
        data=copy.deepcopy(self.data)
        data['M1']=data['M1'].iloc[:2].copy()
        data['M1'].loc[1,'open']=102.
        test=replay.Backtest(data,self.meta,55,entry_delay_seconds=60)
        result=self.generic();test.pending=(copy.deepcopy(result),self.ts)
        with patch.object(test,'evaluate',side_effect=AssertionError('must not rebuild frozen pending')):
            trades=test.run()
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]['fill_server_ts'],self.ts+60)
        self.assertEqual(trades[0]['signal_server_ts'],self.ts)
        self.assertEqual(trades[0]['sl'],result.stop_loss)
    def test_single_position_cannot_be_overwritten(self):
        test=replay.Backtest(self.data,self.meta,55)
        test.open_position(old.proposal(),0,self.ts)
        original=test.position
        test.open_position(old.proposal('sell'),0,self.ts)
        self.assertIs(test.position,original)
        self.assertEqual(test.stats['单仓阻止成交'],1)

    def test_metrics_zero_days_and_split(self):
        trades=[dict(open_bj='2026-09-14 10:00',close_bj='2026-09-14 10:10',pnl=3,hold_min=10,session='london',setup='a',side='BUY'),dict(open_bj='2026-09-16 10:00',close_bj='2026-09-16 10:30',pnl=-1,hold_min=30,session='overlap',setup='b',side='SELL')]
        metric=replay.replay_metrics(trades,{'2026-09-14','2026-09-15','2026-09-16'},100)
        self.assertEqual(metric['zero_trade_days'],1)
        self.assertEqual(metric['best_day_removed_net'],-1)
        self.assertEqual(metric['split']['early']['net'],3)
        self.assertEqual(metric['split']['later']['net'],-1)
        self.assertEqual(metric['hold_minutes']['p90'],28)

if __name__=='__main__': unittest.main()

