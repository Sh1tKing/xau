import copy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xauusd_fib_volume_pa_trader as base
from strategies.framework import Assessment, Strategy, Snapshot, DEFAULT_REGISTRY, StrategyError
from strategies.regions import region_facts
from strategies.definitions.region_rejection import P
from test_session_retest_integration import market
from test_intraday_regions import data, m5_after, m5_clock


def legacy_session_snapshot():
    original = DEFAULT_REGISTRY.snapshot()
    return Snapshot(original.fingerprint, (replace(original.get('session_retest'), enabled=True),))


class OpportunityTests(unittest.TestCase):
    def decision(self, definitions, threshold=55, targets=None):
        frames=market()
        snapshot=Snapshot('fixed', tuple(definitions))
        if targets is None:
            return base.build_fib_volume_pa_decision(*frames[:3],threshold,now=frames[3],strategy_snapshot=snapshot,server_utc_offset=3)
        with patch.object(base,'calculate_targets',side_effect=targets):
            return base.build_fib_volume_pa_decision(*frames[:3],threshold,now=frames[3],strategy_snapshot=snapshot,server_utc_offset=3)

    def strategy(self, name, score, blockers=(), side='buy', priority=0):
        return Strategy(name,name,'1',{'asia':priority,'london':priority,'overlap':priority,'late_us':priority},
            lambda c: Assessment(score if c.action==side else 0,{},blockers=blockers if c.action==side else ('wrong side',)),
            m5_boost=False)

    def test_blocked_top_does_not_swallow_executable_and_tie_priority(self):
        a=self.strategy('blocked',100,('source missing',))
        b=self.strategy('valid',80)
        result=self.decision([a,b])
        self.assertEqual((result.action,result.details['setup'],result.score_buy),('buy','valid',100))
        self.assertEqual(len(result.details['opportunities']),4)
        first=result.details['opportunities'][0]
        first['strategy_evidence']['buy']['blockers'].append('changed')
        self.assertNotIn('changed',result.details['opportunities'][2]['strategy_evidence']['buy']['blockers'])

    def test_hard_blocked_opposite_close_score_does_not_veto(self):
        result=self.decision([self.strategy('buy_plan',90),self.strategy('sell_plan',92,side='sell')])
        self.assertEqual(result.action,'buy')
        self.assertEqual(result.score_sell,92)

    def test_low_score_local_target_failure_retains_structural_candidate(self):
        result=self.decision([self.strategy('autonomous',20)],targets=lambda *a,**k:(0.,0.))
        opportunity=result.details['opportunities'][0]
        self.assertTrue(base.opportunity_eligible(opportunity))
        self.assertFalse(opportunity['local_plan_status']['valid'])
        self.assertEqual(opportunity['local_score'],20)
        self.assertEqual(result.action,'no')

    def test_session_baseline_prices_and_source_unchanged(self):
        result=self.decision([replace(DEFAULT_REGISTRY.snapshot().get('session_retest'), enabled=True)])
        self.assertEqual(result.action,'buy')
        self.assertEqual(result.details['reference_price_source'],'confirmed_closed_m5_close')
        expected=base.calculate_targets('buy',float(result.details['entry_price']),result.details['entry_guard_atr'],None,
                    base.recent_m5_extreme(market()[2],'buy'),base.recent_m5_extreme(market()[2],'buy'),target_r=3.)
        self.assertEqual((result.stop_loss,result.take_profit),expected)
        self.assertEqual(sum(o['local_plan_status']['valid'] for o in result.details['opportunities']),1)

    def region(self):
        m15=data(21);m5=m5_after(m15,1,{0:dict(low=98.6,close=100,open=100,high=101)})
        now=m5_clock(m5)
        evidence=region_facts('rejection',m15,m5,now,0,'buy',P)
        return m15,m5,now,evidence

    def test_region_reconstruction_rejects_forged_anchor_window_and_obstacles(self):
        m15,m5,now,evidence=self.region()
        verified=base.verified_region_evidence('buy',m15,m5,now,0,evidence,P,'rejection')
        self.assertIn('host_verified_region',verified)
        for key,value in [('risk_anchor',dict(price=99,source_refs=[])),('target_obstacles',[{'obstacle_price':110}]),
                          ('active_window',dict(start_utc=1,end_utc=now.timestamp()+99999))]:
            bad=copy.deepcopy(evidence);bad[key]=value
            with self.subTest(key=key),self.assertRaises(StrategyError):
                base.verified_region_evidence('buy',m15,m5,now,0,bad,P,'rejection')
        bad=copy.deepcopy(evidence);bad['region']['expiry_utc']+=900
        with self.assertRaises(StrategyError):
            base.verified_region_evidence('buy',m15,m5,now,0,bad,P,'rejection')

    def test_region_stop_outward_buy_sell_cap_and_nearest_first_edge(self):
        m15,m5,now,evidence=self.region()
        frozen=base.verified_region_evidence('buy',m15,m5,now,0,evidence,P,'rejection')
        stop=base.required_region_stop('buy',100,2,m5,frozen,P)
        self.assertLessEqual(stop,frozen['risk_anchor']['price']-base.stop_buffer(2))
        self.assertLessEqual(stop,98.6-base.stop_buffer(2))
        self.assertLessEqual(100-stop,8)
        with self.assertRaises(StrategyError):base.required_region_stop('buy',110,2,m5,frozen,P)
        mirrored=copy.deepcopy(frozen)
        f=mirrored['host_verified_region'];f['action']='sell';f['risk_anchor']['price']=200-f['risk_anchor']['price']
        f['protection_bars']=[dict(b,high=200-b['low'],low=200-b['high']) for b in f['protection_bars']]
        sell=base.required_region_stop('sell',100,2,None,mirrored,P)
        self.assertAlmostEqual(sell,200-stop)
        f=frozen['host_verified_region']
        f['target_obstacles']=[dict(lower=104,upper=106,obstacle_price=104,region_id='near'),
                               dict(lower=110,upper=112,obstacle_price=110,region_id='far')]
        self.assertEqual(base.region_target_limit('buy',100,2,frozen),103.4)
        self.assertTrue(base.region_plan_error('buy',100,stop,104,2,frozen))
        f['target_obstacles'][0]=dict(lower=99,upper=101,obstacle_price=99,region_id='overlap')
        self.assertLess(base.region_target_limit('buy',100,2,frozen),100)
        self.assertTrue(base.region_plan_error('buy',100,stop,100.1,2,frozen))

    def test_real_region_host_finishes_plan_and_fails_ema(self):
        m15,m5,now,evidence=self.region()
        m15=m15.assign(tick_volume=100);m5=m5.assign(tick_volume=100)
        from strategies.definitions.region_rejection import STRATEGY
        snapshot=Snapshot('region', (replace(STRATEGY,enabled=True),))
        with patch.object(base,'trend_from_ema',return_value='bull'):
            result=base.build_fib_volume_pa_decision(m15,m15,m5,55,now,strategy_snapshot=snapshot,server_utc_offset=0)
        self.assertEqual(result.action,'buy')
        self.assertEqual(result.details['setup'],'region_rejection')
        self.assertTrue(result.details['strategy_assessments']['buy']['evidence']['host_verified_region'])
        with patch.object(base,'trend_from_ema',return_value='bull'),patch.object(base,'chase_blocks_at',return_value=(True,'EMA gate')):
            result=base.build_fib_volume_pa_decision(m15,m15,m5,55,now,strategy_snapshot=snapshot,server_utc_offset=0)
        self.assertEqual(result.action,'no')
        self.assertIn('EMA gate',result.reasoning)

    def test_confirmed_nearest_obstacle_cannot_be_omitted(self):
        m15,m5,now,_=self.region();m15.loc[16,'high']=105.
        evidence=region_facts('rejection',m15,m5,now,0,'buy',P)
        self.assertEqual(len(evidence['target_obstacles']),1)
        self.assertEqual(evidence['target_obstacles'][0]['obstacle_price'],evidence['target_obstacles'][0]['lower'])
        for obstacles in ([], [dict(evidence['target_obstacles'][0],obstacle_price=120)]):
            bad=copy.deepcopy(evidence);bad['target_obstacles']=obstacles
            with self.assertRaises(StrategyError):
                base.verified_region_evidence('buy',m15,m5,now,0,bad,P,'rejection')

    def test_cloned_region_definition_declared_kind_and_unknown_kind(self):
        m15,m5,now,_=self.region();m15=m15.assign(tick_volume=100);m5=m5.assign(tick_volume=100)
        from strategies.definitions.region_rejection import STRATEGY
        clone=replace(STRATEGY,id='cloned_region',enabled=True)
        def run(strategy):
            with patch.object(base,'trend_from_ema',return_value='bull'):
                return base.build_fib_volume_pa_decision(m15,m15,m5,55,now,Snapshot('clone',(strategy,)),0)
        result=run(clone)
        self.assertEqual((result.action,result.details['setup']),('buy','cloned_region'))
        bad=replace(clone,parameters=dict(clone.parameters,region_event_kind='unknown'))
        result=run(bad)
        self.assertEqual(result.action,'no')
        self.assertFalse(any(base.opportunity_eligible(o) for o in result.details['opportunities']))

    def test_generic_event_uses_source_clock_side_and_frozen_identity(self):
        from strategies.events import make_entry_event
        frames=market();m15,h1,m5,now=frames
        raw=int(m5.time.iloc[-1]);close=raw+300-10800
        def definition(side_override=None):
            def assess(c):
                side=side_override or c.action
                event=make_entry_event(event_id='generic/'+side,region_id='source',episode_start_bar_time=raw,
                    action=side,trigger_timeframe='M5',trigger_bar_time=raw,trigger_close_utc=close,
                    valid_until_utc=close+300,source_refs=[dict(timeframe='M5',bar_time=raw)]).to_dict()
                return Assessment(80,{},evidence=dict(action=side,event_id=event['event_id'],entry_event=event,
                    server_offset_hours=3,closed_trigger=dict(confirmed=True,timeframe='M5',bar_time=raw,action=side)))
            return Strategy('generic','generic','1',dict(asia=0,london=0,overlap=0,late_us=0),assess,
                            m5_boost=False,one_entry_per_event=True)
        result=self.decision([definition()])
        self.assertEqual(result.action,'buy')
        evidence=result.details['strategy_assessments']['buy']['evidence']
        self.assertEqual(evidence['host_verified_entry_event']['entry_event'],evidence['entry_event'])
        trader=base.FibVolumePATrader.__new__(base.FibVolumePATrader)
        trader.strategy_ledger=SimpleNamespace(entry_error=lambda *a:'')
        with patch.object(base.time,'time',return_value=now.timestamp()+5):
            self.assertEqual(trader.validate_strategy_entry(result),'')
            changed=copy.deepcopy(result);changed.details['strategy_assessments']['buy']['evidence']['entry_event']['event_id']='forged'
            self.assertTrue(trader.validate_strategy_entry(changed))
        with patch.object(base.time,'time',return_value=close+300):
            self.assertIn('过期',trader.validate_strategy_entry(result))
        mismatch=self.decision([definition('sell')])
        self.assertEqual(mismatch.action,'no')
        self.assertTrue(mismatch.details['opportunities'][0]['structural_blockers'])

    def test_region_fixed_r_cap_does_not_change_with_live_tick(self):
        m15,m5,now,evidence=self.region()
        frozen=base.verified_region_evidence('buy',m15,m5,now,0,evidence,P,'rejection')
        sl=base.required_region_stop('buy',100,2,m5,frozen,P)
        tp=base.round_protective_stop('buy',100+P['target_r']*(100-sl))
        result=base.StrategyResult('buy',85,False,'',sl,tp,85,0,dict(strategy_contract=dict(demo_only=True,stop_reference='region'),
            entry_guard_atr=2.,entry_guard_ema21=100.,entry_price='100.00',reference_price=100.,
            strategy_evidence=dict(buy=dict(evidence=frozen))))
        self.assertEqual(base.research_execution_error(result,SimpleNamespace(bid=99.7,ask=99.8)),'')
        result.stop_loss=91.7995
        self.assertIn('上限',base.research_execution_error(result,SimpleNamespace(bid=99.7,ask=99.8)))

    def test_region_shared_helpers_reject_nonfinite_bool_text_and_bad_action(self):
        import numpy as np
        m15,m5,now,evidence=self.region()
        frozen=base.verified_region_evidence('buy',m15,m5,now,0,evidence,P,'rejection')
        sl=base.required_region_stop('buy',100.,2.,m5,frozen,P)
        self.assertEqual(base.region_plan_error('buy',np.float64(100.),np.float64(sl),np.float64(104.),np.float64(2.),frozen),'')
        invalid=(float('nan'),float('inf'),float('-inf'),True,False,'100',None,0.,-1.)
        for action in ('buy','sell'):
            for index in range(4):
                for value in invalid:
                    prices=[100.,sl,104.,2.];prices[index]=value
                    with self.subTest(action=action,index=index,value=value):
                        self.assertTrue(base.region_plan_error(action,*prices,frozen))
            for index in (0,1):
                for value in invalid:
                    args=[100.,2.];args[index]=value
                    with self.subTest(action=action,index=index,value=value):
                        with self.assertRaises(StrategyError):base.required_region_stop(action,*args,m5,frozen,P)
                        with self.assertRaises(StrategyError):base.region_target_limit(action,*args,frozen)
        for action in ('no','BUY','',None,True):
            self.assertTrue(base.region_plan_error(action,100.,sl,104.,2.,frozen))
            with self.assertRaises(StrategyError):base.required_region_stop(action,100.,2.,m5,frozen,P)
            with self.assertRaises(StrategyError):base.region_target_limit(action,100.,2.,frozen)
        self.assertEqual(base.region_target_limit('buy',100.,2.,frozen),None)

if __name__=='__main__':unittest.main()
