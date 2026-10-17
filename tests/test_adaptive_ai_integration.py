"""Real coarse host→HTTP→quality→finalization→AI2→broker send, offline only."""
from contextlib import ExitStack
import copy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import xauusd_fib_volume_pa_ai3 as ai
from strategies.framework import DEFAULT_REGISTRY, Registry
from strategies.lifecycle import EventLedger
from test_ai_entry_gate_v2 import quality_sample

base = ai.base
logging.disable(logging.CRITICAL)


def frames(profile='asia_adaptive', side='buy'):
    hour = {'asia_adaptive': 2, 'london_adaptive': 8, 'us_adaptive': 14}[profile]
    clock = datetime(2026, 10, 10, hour, 35, 5, tzinfo=timezone.utc)
    result = []
    for tf, step in (('M15', 900), ('H1', 3600), ('M5', 300)):
        end = int((clock.timestamp()+10800)//step)*step
        rows = []
        for i in range(80):
            close = 100. + (1 if side == 'buy' else -1)*i*.02 if tf == 'H1' else 100.
            opening = close + (.4 if side == 'buy' else -.4) if tf == 'M5' else close
            rows.append(dict(time=end-(80-i)*step, open=opening, high=close+1.,
                             low=close-1., close=close, tick_volume=100))
        result.append(base.pd.DataFrame(rows))
    # Explicit strong opposite final M5 candle: old mode's timing veto is true.
    result[2].loc[79, 'open'] = 100.7 if side == 'buy' else 99.3
    result[2].loc[79, 'close'] = 99.3 if side == 'buy' else 100.7
    return *result, clock


class Response:
    status_code, text, headers = 200, '', {}
    def __init__(self, value, messages=False):
        self.value, self.messages = value, messages
    def json(self):
        content = self.value if isinstance(self.value, str) else json.dumps(self.value)
        if self.messages:
            return {'type': 'message', 'content': [{'type': 'text', 'text': content}], 'stop_reason': 'end_turn'}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}


class AdaptiveAITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        # This historical synthetic-price fixture isolates the calendar. The real
        # enabled news gate is exercised with dated schedules in test_news_entry_pause.
        for scope in (patch.dict(ai.os.environ, {'FIB_PA_STRATEGY_STATE_FILE': str(self.directory/'events.json'),
                                               'FIB_PA_NEWS_PAUSE_ENABLED': 'false'}, clear=True),
                      patch.object(ai, '_PRIMARY_TIMEOUT_BACKOFF', ai.PrimaryTimeoutBackoff()),
                      patch.object(ai, 'AI3_STATE_FILE', str(self.directory/'ai3.json')),
                      patch.object(ai, 'AI_USAGE_DIR', self.temp.name), patch.dict(ai._CHAT_OPTIONS, {}, clear=True),
                      patch.object(ai, 'AI1_URL', 'https://unit.test/chat'), patch.object(ai, 'AI1_KEY', 'mock'),
                      patch.object(ai, 'AI2_URL', 'https://unit.test/chat'), patch.object(ai, 'AI2_KEY', 'mock'),
                      patch.object(ai, 'AI_ENABLED', True), patch.object(base, 'ENABLE_TRADING', True),
                      patch.object(base, 'STRATEGIES', DEFAULT_REGISTRY), patch.object(base, 'LOT_SIZE', .01),
                      patch.object(ai.time, 'sleep'), patch.object(base, 'send_feishu_notification')):
            scope.start(); self.addCleanup(scope.stop)
        self.trader = ai.ThreeAITrader()
        self.positions, self.sent, self.snapshots = [], [], []

    def configure(self, profile='asia_adaptive', side='buy'):
        self.profile, self.side = profile, side
        *self.data, self.clock = frames(profile, side)
        self.now = self.clock.timestamp()
        self.tick = SimpleNamespace(bid=100.1 if side == 'buy' else 99.7,
                                    ask=100.3 if side == 'buy' else 99.9, time=int(self.now+10800))
        for scope in (patch.object(base.time, 'time', side_effect=lambda: self.now),
                      patch.object(self.trader, 'get_context', side_effect=lambda: tuple(self.data)),
                      patch.object(base.mt5, 'symbol_info', return_value=SimpleNamespace(
                          trade_tick_size=.01, point=.01, digits=2, trade_stops_level=0, trade_freeze_level=0)),
                      patch.object(base.mt5, 'account_info', return_value=SimpleNamespace(
                          login=111, server='offline-demo', trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO)),
                      patch.object(base.mt5, 'symbol_info_tick', side_effect=lambda _: self.tick),
                      patch.object(base.mt5, 'positions_get', side_effect=lambda **_: list(self.positions)),
                      patch.object(base.mt5, 'order_send', side_effect=self.broker_send),
                      patch.object(ai, 'append_decision_snapshot', side_effect=lambda *a: self.snapshots.append(a))):
            scope.start(); self.addCleanup(scope.stop)

    def broker_send(self, request):
        self.sent.append(copy.deepcopy(request))
        if getattr(self, 'uncertain', False):
            return None
        self.positions[:] = [SimpleNamespace(ticket=123, identifier=123, symbol=base.SYMBOL, magic=base.MAGIC,
            type=base.mt5.POSITION_TYPE_BUY if self.side == 'buy' else base.mt5.POSITION_TYPE_SELL,
            comment=request['comment'], price_open=request['price'], sl=request['sl'], tp=request['tp'],
            time=int(self.now+10800), volume=.01, profit=0.)]
        return SimpleNamespace(retcode=base.mt5.TRADE_RETCODE_DONE, order=123, price=request['price'], comment='mock')

    def proposal(self, request):
        pack = json.loads(request['messages'][-1]['content'].split('\n', 1)[1])
        own = pack['opportunities'][0]
        self.assertEqual(own['action'], self.side)
        _, value = quality_sample(self.side)
        value['opportunity_id'] = own['opportunity_id']
        value['stop_loss'], value['take_profit'] = (97., 109.) if self.side == 'buy' else (103., 91.)
        value['trigger_evidence'] = '原始最近三根M5位置及收盘实体，自主顺势交易依据'
        for card in value['quality_assessment']['dimensions'].values():
            card['evidence_refs'] = ['/m5_bars/79/close']
        return value

    def run_entry(self, mutate=None, review='APPROVE', before_review=None, protocol='chat'):
        def post(url, **kwargs):
            request = kwargs['json']
            pack = json.loads(request['messages'][-1]['content'].split('\n', 1)[1])
            if 'proposal' not in pack:
                value = self.proposal(request)
                if mutate:
                    value = mutate(value)
                if before_review:
                    before_review()
            else:
                value = {'decision': review, 'defects': [], 'fact_conflicts': [], 'reasoning': '固定SLTP及实时风险通过'}
            return Response(value, protocol == 'messages')
        with patch.object(ai.requests, 'post', side_effect=post) as mock:
            stamp = int(self.data[0].time.iloc[-1])
            seq = self.trader.claim_task('entry', stamp, 360)
            self.trader.entry_task(stamp, seq)
        return mock

    def test_three_profiles_buy_sell_full_actual_chain_with_coarse_no_and_saved_contract(self):
        for profile, side in ((profile, side) for profile in ('asia_adaptive','london_adaptive','us_adaptive')
                              for side in ('buy','sell')):
            with self.subTest(profile=profile, side=side):
                # Separate fixture lifecycle per case.
                other = AdaptiveAITests(); other.setUp()
                try:
                    other.configure(profile, side)
                    local = other.trader.evaluate_context(*other.data)
                    self.assertEqual(local.action, 'no')
                    own = next(o for o in local.details['opportunities'] if base.opportunity_eligible(o))
                    self.assertLess(own['local_score'], 75)
                    self.assertTrue(base.m5_blocks_entry(other.data[2], side)[0])
                    self.assertTrue(own['local_plan']['details']['m5_advisory'])
                    self.assertFalse(own['strategy_evidence'][side]['evidence']['trade_trigger_confirmed'])
                    self.assertNotIn('entry_event', own['strategy_evidence'][side]['evidence'])
                    post = other.run_entry()
                    self.assertEqual(post.call_count, 2, other.snapshots)
                    self.assertEqual(len(other.sent), 1, other.snapshots)
                    saved = other.trader.entry_contexts['123']
                    self.assertEqual(saved['strategy_contract']['id'], profile)
                    self.assertEqual(saved['reference_entry_price'], other.tick.ask if side == 'buy' else other.tick.bid)
                    self.assertEqual(saved['entry_price_source'], 'ai1_request_live_ask' if side == 'buy' else 'ai1_request_live_bid')
                    self.assertTrue(Path(ai.AI3_STATE_FILE).exists())
                    self.assertEqual(other.sent[0]['volume'], .01)
                    self.assertEqual(other.trader.strategy_ledger.snapshot()['positions']['123']['max_hold_minutes'], 90)
                    for call in post.call_args_list:
                        self.assertIn(DEFAULT_REGISTRY.snapshot().get(profile).ai_rules['entry' if call == post.call_args_list[0] else 'review'], call.kwargs['json']['messages'][0]['content'])
                finally:
                    other.doCleanups()

    def test_no_reject_missing_quality_bad_json_and_invalid_plan_never_order(self):
        cases = [('no', lambda v: dict(v, action='no', wait_basis=[{'code':'missing_trigger','reason':'无触发','evidence_refs':['/m5_bars/79/close']}]), 'APPROVE'),
                 ('reject', None, 'REJECT'), ('quality', lambda v: {k:x for k,x in v.items() if k!='quality_assessment'}, 'APPROVE'),
                 ('json', lambda v: '{bad}', 'APPROVE'), ('sl8', lambda v: dict(v, stop_loss=90.), 'APPROVE'),
                 ('finite', lambda v: dict(v, stop_loss=float('inf')), 'APPROVE'),
                 ('rr', lambda v: dict(v, take_profit=101.), 'APPROVE')]
        for label, mutate, review in cases:
            with self.subTest(label=label):
                other = AdaptiveAITests(); other.setUp()
                try:
                    other.configure(); post=other.run_entry(mutate, review)
                    self.assertEqual(other.sent, [], other.snapshots)
                    self.assertEqual(post.call_count, 2 if label=='reject' else 1)
                    if label=='no':
                        self.assertEqual(next(s[2]['status'] for s in other.snapshots if s[1]=='ai1_result'),'watch')
                finally: other.doCleanups()

    def test_account_unknown_real_or_h1_neutral_blocks_before_http(self):
        self.configure()
        for account in (None, SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_REAL)):
            with patch.object(base.mt5,'account_info',return_value=account):
                self.assertEqual(self.run_entry().call_count,0)
        self.data[1]['close'] = 100.; self.data[1]['open'] = 100.
        self.assertEqual(self.run_entry().call_count,0)
        self.assertEqual(self.sent,[])

    def test_unknown_positions_or_open_position_prevent_each_paid_request(self):
        self.configure()
        for positions in (None, [SimpleNamespace(ticket=999)]):
            with patch.object(base.mt5, 'positions_get', return_value=positions):
                self.assertEqual(self.run_entry().call_count, 0)
        self.assertEqual(self.sent, [])

    def test_positions_become_unknown_or_occupied_after_ai1_stop_before_ai2_http(self):
        for positions in (None, [SimpleNamespace(ticket=999, magic=0)]):
            with self.subTest(positions=positions):
                other = AdaptiveAITests(); other.setUp()
                try:
                    other.configure()
                    state = [[]]
                    with patch.object(base.mt5, 'positions_get', side_effect=lambda **_: state[0]):
                        post = other.run_entry(before_review=lambda: state.__setitem__(0, positions))
                    self.assertEqual(post.call_count, 1, other.snapshots)
                    self.assertEqual(other.sent, [])
                    self.assertIn('持仓状态未知或已达到单仓上限', other.snapshots[-1][2]['reason'])
                finally: other.doCleanups()

    def test_account_becomes_unknown_or_real_after_ai1_stop_before_ai2_http(self):
        for account in (None, SimpleNamespace(trade_mode=base.mt5.ACCOUNT_TRADE_MODE_REAL)):
            with self.subTest(account=account):
                other = AdaptiveAITests(); other.setUp()
                try:
                    other.configure()
                    state = [SimpleNamespace(login=111, server='offline-demo', trade_mode=base.mt5.ACCOUNT_TRADE_MODE_DEMO)]
                    with patch.object(base.mt5, 'account_info', side_effect=lambda: state[0]):
                        post = other.run_entry(before_review=lambda: state.__setitem__(0, account))
                    self.assertEqual(post.call_count, 1, other.snapshots)
                    self.assertEqual(other.sent, [])
                    self.assertIn('仅允许模拟账户', other.snapshots[-1][2]['reason'])
                finally: other.doCleanups()

    def test_ai2_retry_and_failover_recheck_positions_before_each_http(self):
        for protocol in ('chat', 'messages'):
            for backup in (False, True):
                for positions in (None, [SimpleNamespace(ticket=999, magic=0)]):
                    with self.subTest(protocol=protocol, backup=backup, positions=positions):
                        other = AdaptiveAITests(); other.setUp()
                        try:
                            other.configure(); state = [[]]; requests = []
                            url = 'https://unit.test/' + protocol
                            def post(url, **kwargs):
                                request = kwargs['json']; requests.append(copy.deepcopy(request))
                                pack = json.loads(request['messages'][-1]['content'].split('\n', 1)[1])
                                if 'proposal' not in pack:
                                    return Response(other.proposal(request), protocol == 'messages')
                                state[0] = positions
                                raise ai.requests.ConnectionError('mock review transport failure')
                            options = {'AI2_BACKUP_URL': url.replace('unit.test', 'backup.test'),
                                       'AI2_BACKUP_KEY': 'mock'} if backup else {}
                            with patch.object(ai, 'AI1_URL', url), patch.object(ai, 'AI2_URL', url), \
                                 patch.dict(ai.os.environ, options), \
                                 patch.object(base.mt5, 'positions_get', side_effect=lambda **_: state[0]), \
                                 patch.object(ai.requests, 'post', side_effect=post) as http:
                                stamp = int(other.data[0].time.iloc[-1])
                                seq = other.trader.claim_task('entry', stamp, 360)
                                other.trader.entry_task(stamp, seq)
                            self.assertEqual(http.call_count, 2, other.snapshots)
                            self.assertEqual(other.sent, [])
                            self.assertIn('持仓状态未知或已达到单仓上限', other.snapshots[-1][2]['reason'])
                            system = requests[1]['system'] if protocol == 'messages' else requests[1]['messages'][0]['content']
                            self.assertIn(DEFAULT_REGISTRY.snapshot().get('asia_adaptive').ai_rules['review'], system)
                        finally: other.doCleanups()

    def test_clock_source_failure_does_not_claim_scope_then_valid_retries(self):
        self.configure(); self.tick.time = 0
        self.assertEqual(self.run_entry().call_count,0)
        self.assertFalse(getattr(self.trader,'_analysis_request_scopes',set()))
        self.tick.time=int(self.now+10800)
        self.assertEqual(self.run_entry().call_count,2)

    def test_first_request_clock_failure_after_valid_collection_allows_same_checkpoint_recovery(self):
        self.configure(); collections = []
        def context():
            collections.append(True)
            if len(collections) == 2:
                self.tick.time = 0
            return tuple(self.data)
        with patch.object(self.trader, 'get_context', side_effect=context):
            self.assertEqual(self.run_entry().call_count, 0)
        self.assertEqual(len(collections), 2)
        candidate = next(snapshot[2] for snapshot in self.snapshots if snapshot[1] == 'entry_candidate')
        self.assertTrue(candidate['market_evidence']['opportunities'])
        self.assertFalse(getattr(self.trader, '_analysis_request_scopes', set()))
        self.assertEqual(self.sent, [])
        self.tick.time = int(self.now + 10800)
        self.assertEqual(self.run_entry().call_count, 2, self.snapshots)
        self.assertEqual(len(self.sent), 1, self.snapshots)
        self.assertTrue(self.trader._analysis_request_scopes)

    def test_newbars_stale_checkpoint_and_too_near_margin_stop_ai2(self):
        for label in ('newbars','ttl','margin','chase','wick'):
            with self.subTest(label=label):
                other=AdaptiveAITests(); other.setUp()
                try:
                    other.configure()
                    def change():
                        if label=='newbars': other.data[1].loc[79,'time'] += 3600
                        if label=='ttl': other.now+=300; other.tick.time+=300
                        if label=='margin': other.tick.bid=97.5; other.tick.ask=97.7
                        if label=='chase': other.tick.bid=103.5; other.tick.ask=103.7
                    if label=='wick': other.data[2].loc[79,'low']=89.
                    post=other.run_entry(before_review=change)
                    self.assertEqual(post.call_count,1, other.snapshots)
                    self.assertEqual(other.sent,[])
                finally: other.doCleanups()

    def test_duplicate_m15_m5_ordinary_scope_no_second_paid_analysis(self):
        self.configure(); self.assertEqual(self.run_entry(lambda v: dict(v, take_profit=101.)).call_count,1)
        self.assertEqual(self.run_entry().call_count,0)
        with patch.object(self.trader,'dispatch_entry') as dispatch:
            self.trader.dispatch_m5_entry(int(self.data[2].time.iloc[-1]))
        dispatch.assert_called_once() # broad M5 eligibility; entry_task owns shared scope claim.

    def test_uncertain_order_consumes_checkpoint_and_pure_local_injection_rejected(self):
        self.configure(); self.uncertain=True; self.assertEqual(self.run_entry().call_count,2)
        self.assertEqual(len(self.sent),1)
        self.trader._analysis_request_scopes=set()
        self.assertEqual(self.run_entry().call_count,0)
        local=self.trader.evaluate_context(*self.data); local.action='buy'; local.details['source']='ai1'
        self.assertFalse(base.FibVolumePATrader.place_order(self.trader,local))
        self.assertEqual(len(self.sent),1)

    def test_ai_disabled_zero_orders_and_no_scope_claim(self):
        self.configure()
        with patch.object(ai,'AI_ENABLED',False): self.assertEqual(self.run_entry().call_count,0)
        self.assertEqual(self.sent,[])

    def test_guardian_uses_original_saved_profile_after_current_definition_changes(self):
        self.configure(); self.run_entry()
        saved=copy.deepcopy(self.trader.entry_contexts['123']['strategy_contract'])
        self.now+=600; self.tick.time+=600
        changed = SimpleNamespace(strategies=(), active=lambda session: [DEFAULT_REGISTRY.snapshot().get('london_adaptive')])
        with patch.object(base.STRATEGIES, 'snapshot', return_value=changed):
            snapshot=self.trader.collect_ai3_snapshot()
        self.assertIn(saved['ai_rules']['guardian'],snapshot['instructions'])
        self.assertIn('检查点过期或已消费本身不是交易失效',snapshot['instructions'])
        self.assertNotIn(DEFAULT_REGISTRY.snapshot().get('london_adaptive').ai_rules['guardian'],snapshot['instructions'])
        self.assertEqual(snapshot['profile_rules'][0]['profile'],'asia_adaptive')

    def test_guardian_and_ledger_keep_nondefault_saved_deadline_after_definition_reload(self):
        for profile in ('asia_adaptive', 'london_adaptive', 'us_adaptive'):
            with self.subTest(profile=profile):
                other = AdaptiveAITests(); other.setUp()
                try:
                    other.configure(profile)
                    definitions = other.directory / 'definitions'
                    definitions.mkdir()
                    for source in DEFAULT_REGISTRY.directory.glob('*_adaptive.py'):
                        text = source.read_text(encoding='utf-8')
                        self.assertEqual(text.count('max_hold_minutes=90'), 1)
                        (definitions / source.name).write_text(
                            text.replace('max_hold_minutes=90', 'max_hold_minutes=120'), encoding='utf-8')
                    registry = Registry(definitions)
                    original = registry.snapshot()
                    with patch.object(base, 'STRATEGIES', registry):
                        post = other.run_entry()
                        self.assertEqual(post.call_count, 2, other.snapshots)
                        self.assertEqual(len(other.sent), 1, other.snapshots)
                        saved = copy.deepcopy(other.trader.entry_contexts['123']['strategy_contract'])
                        self.assertEqual(saved['id'], profile)
                        self.assertEqual(saved['max_hold_minutes'], 120)
                        ledger_before = other.trader.strategy_ledger.snapshot()
                        position = ledger_before['positions']['123']
                        self.assertEqual(position['max_hold_minutes'], 120)
                        deadline = position['opened_at_utc'] + 120 * 60

                        # A real content-fingerprint reload must not rewrite an open position.
                        source = definitions / (profile + '.py')
                        source.write_text(source.read_text(encoding='utf-8').replace(
                            'max_hold_minutes=120', 'max_hold_minutes=180'), encoding='utf-8')
                        current = registry.snapshot()
                        self.assertNotEqual(current.fingerprint, original.fingerprint)
                        self.assertEqual(current.get(profile).max_hold_minutes, 180)
                        other.now += 600; other.tick.time += 600
                        snapshot = other.trader.collect_ai3_snapshot()
                        self.assertIsNotNone(snapshot)
                        self.assertIn(saved['ai_rules']['guardian'], snapshot['instructions'])
                        self.assertIn('120分钟', snapshot['instructions'])
                        self.assertNotIn('90分钟', snapshot['instructions'])
                        self.assertNotIn('180分钟', snapshot['instructions'])
                        self.assertNotIn(current.get(profile).ai_rules['guardian'], snapshot['instructions'])
                        self.assertIn('检查点过期或已消费本身不是交易失效', snapshot['instructions'])
                        self.assertEqual(snapshot['profile_rules'], [
                            {'profile': profile, 'rule': saved['ai_rules']['guardian']}])
                        payload = json.loads(snapshot['prompt'].split('\n', 1)[1])
                        self.assertEqual(payload['positions'][0]['entry_context']['strategy_contract'], saved)
                        self.assertEqual(other.trader.strategy_ledger.snapshot(), ledger_before)
                        restored = EventLedger(other.directory / 'events.json')
                        self.assertEqual(restored.snapshot(), ledger_before)
                        for ledger in (other.trader.strategy_ledger, restored):
                            self.assertEqual(ledger.due_tickets(deadline - .001), ())
                            self.assertEqual(ledger.due_tickets(deadline), (123,))
                finally:
                    other.doCleanups()

    def test_actual_retry_refresh_crosses_profile_with_live_reference_and_ai2_selected_rules(self):
        for protocol in ('chat','messages'):
            with self.subTest(protocol=protocol):
                other=AdaptiveAITests(); other.setUp()
                try:
                    other.configure()
                    url='https://unit.test/messages' if protocol=='messages' else 'https://unit.test/chat'
                    requests=[]
                    def post(url,**kwargs):
                        request=kwargs['json']; requests.append(copy.deepcopy(request))
                        if len(requests)==1:
                            *other.data, clock=frames('london_adaptive')
                            other.now=clock.timestamp(); other.tick.time=int(other.now+10800)
                            other.tick.bid=100.3; other.tick.ask=100.5
                            raise ai.requests.ConnectionError('mock switch')
                        pack=json.loads(request['messages'][-1]['content'].split('\n',1)[1])
                        if 'proposal' in pack:
                            value={'decision':'APPROVE','defects':[],'fact_conflicts':[],'reasoning':'固定保护通过'}
                        else: value=other.proposal(request)
                        return Response(value,protocol=='messages')
                    with patch.object(ai,'AI1_URL',url),patch.object(ai,'AI2_URL',url), \
                         patch.dict(ai.os.environ,{'AI1_BACKUP_URL':url.replace('unit.test','backup.test'),'AI1_BACKUP_KEY':'mock'}), \
                         patch.object(ai.requests,'post',side_effect=post):
                        stamp=int(other.data[0].time.iloc[-1]); seq=other.trader.claim_task('entry',stamp,360)
                        other.trader.entry_task(stamp,seq)
                    self.assertEqual(len(requests),3,other.snapshots)
                    self.assertEqual(len(other.sent),1,other.snapshots)
                    packs=[json.loads(r['messages'][-1]['content'].split('\n',1)[1]) for r in requests]
                    self.assertEqual([p['opportunities'][0]['strategy_contract']['id'] for p in packs[:2]],['asia_adaptive','london_adaptive'])
                    self.assertEqual([p['opportunities'][0]['reference_price']['price'] for p in packs[:2]],[100.3,100.5])
                    systems=[r['system'] if protocol=='messages' else r['messages'][0]['content'] for r in requests]
                    self.assertIn('asia_adaptive',systems[0]); self.assertIn('london_adaptive',systems[1])
                    self.assertIn(DEFAULT_REGISTRY.snapshot().get('london_adaptive').ai_rules['review'],systems[2])
                    self.assertEqual(other.trader.entry_contexts['123']['reference_entry_price'],100.5)
                    other.positions=[]
                    self.assertEqual(other.run_entry().call_count,0)
                finally: other.doCleanups()

    def test_cooldown_new_checkpoint_source_tamper_and_fingerprint_rejected(self):
        self.configure(); self.run_entry()
        original=next(s[2]['proposal'] for s in self.snapshots if s[1]=='ai2_result')
        result=base.StrategyResult(score_buy=0, score_sell=0, **original)
        self.positions=[]
        self.assertTrue(self.trader.strategy_ledger.closed_positions(set(),self.now))
        self.assertIn('cooldown',self.trader.strategy_ledger.entry_error('new-checkpoint',self.now))
        self.trader.strategy_ledger=EventLedger(self.directory/'fresh-events.json')
        self.assertEqual(self.trader.validate_strategy_entry(result),'')
        for mutation in ('binding','source','fingerprint','h1','profile'):
            with self.subTest(mutation=mutation):
                changed=copy.deepcopy(result)
                evidence=changed.details['strategy_evidence']['buy']['evidence']
                if mutation=='binding': changed.details.pop('ai1_source_binding')
                if mutation=='source': evidence['analysis_checkpoint']['m5_bar_time']-=300
                if mutation=='fingerprint': changed.details['strategy_fingerprint']='old'
                if mutation=='h1': evidence['host_verified_analysis_checkpoint']['h1_trend']='bear'
                if mutation=='profile': changed.details['strategy_contract']['id']='london_adaptive'
                self.assertTrue(self.trader.validate_strategy_entry(changed))
        local=self.trader.evaluate_context(*self.data); local.action='buy'; local.details['source']='ai1'
        self.assertFalse(base.FibVolumePATrader.place_order(self.trader,local))

    def test_actual_new_checkpoint_during_close_cooldown_does_not_call_ai(self):
        self.configure(); self.run_entry(); self.positions=[]
        self.now+=285; self.tick.time+=285
        self.assertEqual(self.trader.strategy_ledger.closed_positions(set(),self.now),[123])
        self.now+=15; self.tick.time+=15; self.data[2]['time']+=300
        self.assertEqual(self.run_entry().call_count,0)
        self.assertEqual(len(self.sent),1)

    def test_per_attempt_instructions_and_pack_switch_together_for_chat_and_messages(self):
        for protocol in ('chat','messages'):
            snapshots=iter([{'prompt':'亚盘报价100','instructions':ai.role_instructions('AI-1',[DEFAULT_REGISTRY.snapshot().get('asia_adaptive').manifest()])},
                            {'prompt':'欧盘报价101','instructions':ai.role_instructions('AI-1',[DEFAULT_REGISTRY.snapshot().get('london_adaptive').manifest()])}])
            url='https://unit.test/messages' if protocol=='messages' else 'https://unit.test/chat'
            with patch.dict(ai.os.environ,{'AI1_BACKUP_URL':url.replace('unit.test','backup.test'),'AI1_BACKUP_KEY':'mock'}), \
                 patch.object(ai.requests,'post',side_effect=[ai.requests.ConnectionError('mock'),Response({'action':'no'},protocol=='messages')]) as post:
                ai.call_chat('AI-1','mock','low','old','old',10,url,'mock',refresh_request=lambda:next(snapshots))
            requests=[c.kwargs['json'] for c in post.call_args_list]
            self.assertEqual([r['messages'][-1]['content'] for r in requests],['亚盘报价100','欧盘报价101'])
            systems=[r['system'] if protocol=='messages' else r['messages'][0]['content'] for r in requests]
            self.assertIn('asia_adaptive',systems[0]); self.assertIn('london_adaptive',systems[1])
            with patch.object(ai.requests,'post') as post:
                with self.assertRaises(ai.MarketSnapshotError):
                    ai.call_chat('AI-1','mock','low','old','old',10,url,'mock',refresh_request=lambda:{'prompt':'x','instructions':''})
                post.assert_not_called()


if __name__ == '__main__': unittest.main()
