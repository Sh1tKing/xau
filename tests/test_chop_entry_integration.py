"""Exercise the actual entry/HTTP/reservation/broker boundaries offline."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_adaptive_ai_integration as fixtures
from test_repeated_chop import bars as chop_bars, mirror, directional_bars
from strategies import chop, citations

ai, base = fixtures.ai, fixtures.base


class ChopEntryIntegration(unittest.TestCase):
    def fixture(self, side='buy', kind='middle'):
        f=fixtures.AdaptiveAITests(); f.setUp(); f.configure(side=side)
        self.addCleanup(f.doCleanups)
        rows=directional_bars(clock=f.clock) if kind=='directional' else chop_bars(clock=f.clock)
        if kind=='breakout':
            rows[-2].update(open=102.,high=103.2,low=101.8,close=103.)
            rows[-1].update(open=103.,high=103.7,low=102.8,close=103.5)
        # Scale synthetic prices into this fixture's SL8/EMA budget.
        rows=[dict(r, **{k:100+(r[k]-100)*.4 for k in ('open','high','low','close')}) for r in rows]
        if side=='sell': rows=mirror(rows); rows=[dict(r,**{k:r[k]-100 for k in ('open','high','low','close')}) for r in rows]
        for index,row in zip(f.data[2].index[-26:],rows):
            for name,value in row.items(): f.data[2].loc[index,name]=value
        quote=101.4 if kind=='breakout' else 101.3 if kind=='directional' else 100.
        f.tick.bid,f.tick.ask=(quote,quote+.2) if side=='buy' else (200.-quote-.2,200.-quote)
        return f

    def test_chop_middle_blocks_both_sides_before_any_http_or_event_claim(self):
        for side in ('buy','sell'):
            f=self.fixture(side)
            post=f.run_entry()
            self.assertEqual(post.call_count,0,f.snapshots)
            self.assertEqual(f.sent,[])
            self.assertEqual(getattr(f.trader,'_analysis_request_scopes',set()),set())
            self.assertEqual(f.trader.strategy_ledger.snapshot()['events'],{})
            self.assertIn('反复震荡',json.dumps(f.snapshots,ensure_ascii=False))
            f.doCleanups()

    def test_breakout_reaches_both_ai_roles_and_broker_with_original_binding(self):
        for side in ('buy','sell'):
            f=self.fixture(side,'breakout')
            http=f.run_entry()
            self.assertEqual(http.call_count,2,f.snapshots)
            self.assertEqual(len(f.sent),1,f.snapshots)
            inp=next(row[2]['market_evidence'] for row in f.snapshots if row[1]=='ai1_input')
            self.assertEqual(inp['chop_context']['state'],'chop')
            selected=next(item for item in inp['opportunities'] if item['action']==side)
            self.assertEqual(selected['entry_chop']['route'],'breakout_confirmed')
            self.assertEqual(f.trader.entry_contexts['123']['strategy_contract']['version'],'4')
            wire=json.loads(http.call_args_list[0].kwargs['json']['messages'][-1]['content'].split('\n',1)[1])
            refs=citations.catalog_bindings(wire)
            self.assertIn('/chop_context/windows/24/efficiency',refs.values())
            self.assertFalse(citations.scope_allowed('/chop_context/private/candidate',0,side))
            self.assertFalse(citations.scope_allowed('/chop_context/windows/24/private',0,side))
            self.assertNotIn('host_verified_direction',json.dumps(wire))
            f.doCleanups()

    def test_return_into_box_after_ai1_prevents_paid_ai2_and_does_not_reserve(self):
        for side,kind in [(s,k) for s in ('buy','sell') for k in ('breakout','directional')]:
            f=self.fixture(side,kind)
            def move():
                mid=100. if kind=='breakout' else 99. if side=='buy' else 101.
                f.tick.bid,f.tick.ask=mid-.1,mid+.1
            http=f.run_entry(before_review=move)
            self.assertEqual(http.call_count,1,f.snapshots)
            self.assertEqual(f.sent,[])
            self.assertEqual(f.trader.strategy_ledger.snapshot()['events'],{})
            self.assertIn('反复震荡',json.dumps(f.snapshots,ensure_ascii=False))
            f.doCleanups()

    def test_recent_directional_opportunity_reaches_ai_and_broker_in_both_directions(self):
        for side in ('buy','sell'):
            f=self.fixture(side,'directional')
            http=f.run_entry()
            self.assertEqual(http.call_count,2,f.snapshots)
            self.assertEqual(len(f.sent),1,f.snapshots)
            inp=next(row[2]['market_evidence'] for row in f.snapshots if row[1]=='ai1_input')
            own=next(item for item in inp['opportunities'] if item['action']==side)
            self.assertEqual(own['entry_chop']['route'],'directional_leg_confirmed')
            f.doCleanups()

    def test_return_into_box_after_reserve_never_sends_and_keeps_consumed_event(self):
        for side,kind in [(s,k) for s in ('buy','sell') for k in ('breakout','directional')]:
            f=self.fixture(side,kind); ledger=f.trader.strategy_ledger
            original=ledger.reserve
            def reserve_then_move(*args,**kwargs):
                ok=original(*args,**kwargs)
                if ok:
                    mid=100. if kind=='breakout' else 99. if side=='buy' else 101.
                    f.tick.bid,f.tick.ask=mid-.1,mid+.1
                return ok
            with patch.object(ledger,'reserve',side_effect=reserve_then_move): http=f.run_entry()
            self.assertEqual(http.call_count,2,f.snapshots)
            self.assertEqual(f.sent,[],f.snapshots)
            self.assertEqual(len(ledger.snapshot()['events']),1)
            f.doCleanups()

    def test_missing_or_relaxed_policy_cannot_reuse_host_direction_proof(self):
        f=self.fixture('buy','breakout')
        pack=f.trader.collect_entry_snapshot('source')[4]
        own=next(item for item in pack['opportunities'] if item['action']=='buy')
        for kind in ('missing','relaxed','engine'):
            contract=deepcopy(own['strategy_contract']); evidence=deepcopy(own['strategy_evidence']['buy']['evidence'])
            if kind=='missing': contract['parameters'].pop('chop_policy')
            if kind=='relaxed': contract['parameters']['chop_policy']['efficiency_max']=.9
            if kind=='engine': evidence['host_verified_direction']['chop_engine_digest']='new-engine'
            with self.subTest(kind=kind),self.assertRaises(base.StrategyError):
                base.verified_direction_permission('buy',contract,evidence)

    def test_missing_warmup_or_gap_does_not_call_ai(self):
        for kind in ('short','gap'):
            f=self.fixture()
            if kind=='short': f.data[2]=f.data[2].tail(25)
            else: f.data[2]=f.data[2].drop(f.data[2].index[-15])
            self.assertEqual(f.run_entry().call_count,0,f.snapshots)
            self.assertEqual(f.sent,[])
            f.doCleanups()


if __name__=='__main__': unittest.main()
