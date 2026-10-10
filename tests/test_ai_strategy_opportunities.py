"""Frozen pool selection integration; all HTTP/terminal/orders are mocked."""
import copy
import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import xauusd_fib_volume_pa_ai3 as ai
from test_ai_entry_gate_v2 import quality_sample
from test_ai_request_freshness import frame
import test_strategy_opportunities as host_fixtures
from strategies.definitions.region_rejection import P
from test_session_retest_integration import market


def candidate(identifier='A', price=100., setup='pullback', score=20):
    evidence = {'buy': {'blockers': [], 'evidence': {'closed_trigger': {'confirmed': True},
                 'event_id': 'event-' + identifier, 'entry_event': {'event_id': 'event-' + identifier},
                 'candidates': [{'region_id': 'other', 'status': 'unused', 'selection_order': 1,
                                 'candidate': {'closed_trigger': {'confirmed': True}, 'secret': 'alternate'}}]}},
                'sell': {'blockers': ['wrong side'], 'evidence': {}}}
    return {'opportunity_id': identifier, 'action': 'buy', 'local_score': score,
            'strategy_contract': {'id': setup, 'regime_filter': 'none', 'stop_reference': 'm5_extreme',
                                  'entry_reference': 'closed_m5', 'target_max_reference_r': 3.},
            'strategy_fingerprint': 'frozen', 'strategy_evidence': evidence,
            'structural_blockers': [], 'local_plan_status': {'valid': False, 'reason': 'reference plan failed'},
            'reference_price': {'price': price, 'source': 'confirmed_closed_m5_rejection_close', 'bar_time': 1000},
            'event_id': 'event-' + identifier, 'entry_event': {'event_id': 'event-' + identifier},
            'local_plan': {'action': 'no', 'stop_loss': 0., 'take_profit': 0., 'reasoning': 'reference failed',
                           'details': {'setup': setup, 'entry_price': price, 'opportunities': ['DUPLICATE'],
                                       'strategy_contract': {}, 'strategy_assessments': evidence}}}


def pool(items=None):
    items = [candidate(), candidate('B', 101., 'breakout')] if items is None else items
    return {'opportunities': items, 'strategy_fingerprint': 'frozen', 'strategy_contract': items[0]['strategy_contract'] if items else {},
            'strategy_evidence': items[0]['strategy_evidence'] if items else {}, 'details': {'setup': 'pullback', 'opportunities': items},
            'h1_trend': 'bull', 'm15_trend': 'bull', 'atr': 2., 'm15_ema21': 101., 'regime': 'trend',
            'reference_price': {'price': 100., 'source': 'oldwinner', 'bar_time': 1000}, 'snapshot_id': 'frozen',
            'live_quote': {'bid': 100.9, 'ask': 101., 'time': 1001}, 'bar_closure': ai.closed_context_times(frame(), frame(), frame()),
            'local_action': 'no', 'local_stop_loss': 0., 'local_take_profit': 0., 'local_reasoning': 'local fail',
            'computed_evidence': {'price_action': {'M15': {'buy': {'score': 20}}}, 'breakout': {'closed_trigger': True}},
            'm5_bars': [{'time': 1000, 'open': 100., 'high': 102., 'low': 98., 'close': 100.}], 'm15_bars': [], 'h1_bars': []}


def payload(identifier='B', index=1):
    _, result = quality_sample()
    result['opportunity_id'] = identifier
    for item in result['quality_assessment']['dimensions'].values():
        item['evidence_refs'] = [f'/opportunities/{index}/strategy_evidence/buy/evidence/closed_trigger/confirmed']
    return result


class Response:
    status_code, text, headers = 200, '', {}
    def __init__(self, value):
        self.value = value
    def json(self):
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(self.value)}}]}


class OpportunityAITests(unittest.TestCase):
    def setUp(self):
        previous_disable = logging.root.manager.disable
        self.addCleanup(logging.disable, previous_disable)
        logging.disable(logging.CRITICAL)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for scope in (patch.dict(ai.os.environ, {}, clear=True), patch.dict(ai._CHAT_OPTIONS, {}, clear=True),
                      patch.object(ai, 'AI3_STATE_FILE', str(Path(self.temp.name)/'state.json')),
                      patch.object(ai, 'AI_USAGE_DIR', self.temp.name), patch.object(ai, 'AI1_URL', 'https://unit.test/chat'),
                      patch.object(ai, 'AI1_KEY', 'mock'), patch.object(ai, 'AI2_URL', 'https://unit.test/chat'),
                      patch.object(ai, 'AI2_KEY', 'mock'), patch.object(ai, 'AI_ENABLED', True),
                      patch.object(ai.time, 'sleep'), patch.object(ai.base, 'send_feishu_notification')):
            scope.start()
            self.addCleanup(scope.stop)
        self.trader = ai.ThreeAITrader()

    def test_id_direction_unknown_absent_duplicate_and_no(self):
        pack = ai.compact_market_pack(pool())
        for change in ({'opportunity_id': None}, {'opportunity_id': 'foreign'}, {'action': 'sell'}, {'quality_assessment': []}):
            value = payload(); value.update(change)
            self.assertTrue(ai.normalize_ai1_quality(value, pack)[1])
        value = payload(); value['quality_assessment']['direction'] = 'sell'
        self.assertTrue(ai.normalize_ai1_quality(value, pack)[1])
        duplicate = copy.deepcopy(pack); duplicate['opportunities'].append(copy.deepcopy(duplicate['opportunities'][1]))
        self.assertIn('重复', ai.normalize_ai1_quality(payload(), duplicate)[1])
        value = payload(); value['action'] = 'no'
        value['wait_basis'] = [{'code': 'target_space', 'reason': 'No forward room', 'evidence_refs': ['/opportunities/1/local_plan_status']}]
        self.assertEqual(ai.normalize_ai1_quality(value, pack)[1], '')
        self.assertEqual(ai.ai1_result_status(value, '', None), 'watch')

    def test_all_foreign_alias_and_nested_alternate_refs_fail_closed(self):
        pack = pool()
        for ref in ('/opportunities/0/strategy_evidence/buy/evidence/closed_trigger', '/details/setup',
                    '/strategy_contract/id', '/strategy_evidence/buy/evidence/closed_trigger', '/reference_price/price',
                    '/computed_evidence/breakout/closed_trigger', '/computed_evidence/setup',
                    '/computed_evidence/m5_timing/risk_from_reference', '/computed_evidence/obstacles',
                    '/local_plan_feasibility', '/opportunities/1/strategy_evidence/buy/evidence/candidates/0/candidate',
                    '/opportunities/1/strategy_evidence/sell/blockers/0',
                    '/opportunities/01/action', '/opportunities/1/strategy_evidence/buy/evidence/host_verified_region',
                    '/opportunities~1/0/action'):
            value = payload(); value['quality_assessment']['dimensions']['trigger']['evidence_refs'] = [ref]
            self.assertTrue(ai.normalize_ai1_quality(value, pack)[1], ref)
        value = payload(); value['quality_assessment']['dimensions']['trigger']['reservations'] = [
            {'cause': 'target_uncertainty', 'event_group': 'x', 'reason': 'foreign', 'evidence_refs': ['/opportunities/0/action']}]
        self.assertIn('隔离', ai.normalize_ai1_quality(value, pack)[1])
        value = payload(); value.update(action='no', wait_basis=[{'code': 'target_space', 'reason': 'foreign', 'evidence_refs': ['/details/setup']}])
        self.assertIn('隔离', ai.normalize_ai1_quality(value, pack)[1])

    def test_compact_shared_once_original_unchanged_and_own_refs_resolve(self):
        pack = pool(); original = copy.deepcopy(pack)
        pack['opportunities'][1]['strategy_evidence']['buy']['evidence']['host_verified_region'] = {'region': {'large': 'duplicate'}}
        original = copy.deepcopy(pack)
        request = ai.compact_market_pack(pack)
        self.assertEqual(pack, original)
        self.assertNotIn('strategy_evidence', request)
        text = json.dumps(request)
        self.assertNotIn('DUPLICATE', text); self.assertNotIn('secret', text); self.assertNotIn('host_verified_region', text)
        self.assertEqual(ai.normalize_ai1_quality(payload(), request)[1], '')
        selected = ai.selected_opportunity_pack(pack, ai.opportunity_selection(payload(), pack)[0])
        self.assertEqual(selected['reference_price']['price'], 101.)
        self.assertEqual(selected['setup'], 'breakout')
        self.assertEqual(selected['opportunities'][0], {})
        self.assertNotIn('breakout', selected['computed_evidence'])
        self.assertEqual(pack, original)

    def test_real_host_blocked_top_and_low_score_failed_plan_pool_remain_visible(self):
        fixtures = host_fixtures.OpportunityTests()
        definitions = [fixtures.strategy('blocked', 100, ('missing source',)), fixtures.strategy('autonomous', 20)]
        local = fixtures.decision(definitions, targets=lambda *args, **kwargs: (0., 0.))
        original = copy.deepcopy(local.details)
        m15, h1, m5, _ = market()
        pack, *_ = ai.build_feature_pack(m15, h1, m5, local, quote={'bid': 101.6, 'ask': 101.93})
        self.assertEqual(local.details, original)
        self.assertEqual(len(pack['opportunities']), 1)
        chosen = pack['opportunities'][0]
        self.assertEqual(chosen['strategy_contract']['id'], 'autonomous')
        self.assertEqual(chosen['local_score'], 20)
        self.assertFalse(chosen['local_plan_status']['valid'])
        self.assertEqual(len(pack['opportunity_blockers']), 4)
        self.assertIn('patterns', pack)
        self.assertEqual(len(pack['m5_bars']), len(m5))

    def run_entry(self, snapshots, responses, finalize=None, frames=None):
        local = ai.base.StrategyResult('no', 20, False, 'reference fail', 0., 0., 20, 0, {})
        def collect(_):
            value = copy.deepcopy(next(snapshots))
            market_frames = frames if frames is not None else (frame(), frame(), frame())
            return *market_frames, local, value, None, 2., 100.
        scopes = [patch.object(self.trader, 'collect_entry_snapshot', side_effect=collect),
                  patch.object(self.trader, 'entry_is_current', return_value=True),
                  patch.object(self.trader, 'ensure_strategy_current'), patch.object(self.trader, 'release_task'),
                  patch.object(self.trader, 'notify_lines'), patch.object(self.trader, 'alert_api_failure'),
                  patch.object(ai, 'append_decision_snapshot'), patch.object(ai.requests, 'post', side_effect=responses)]
        if finalize is not None:
            scopes.append(patch.object(ai, 'finalize_ai_proposal', side_effect=finalize))
        from contextlib import ExitStack
        with ExitStack() as stack:
            mocks = [stack.enter_context(scope) for scope in scopes]
            self.trader.entry_task(1000, 1)
        return mocks[7], mocks[6], mocks[5]

    def test_empty_pool_no_actual_post_normal_local_snapshot(self):
        post, snapshots, alerts = self.run_entry(iter([pool([])]), [])
        post.assert_not_called(); alerts.assert_not_called()
        self.assertEqual([c.args[1] for c in snapshots.call_args_list], ['entry_candidate', 'local_prefilter'])

    def test_new_m15_still_posts_low_score_failed_plan_pool_and_selects_nonwinner(self):
        calls = []
        def finalize(value, reference, *args, **kwargs):
            calls.append((reference, kwargs))
            return None, '观望止于测试'
        post, snapshots, _ = self.run_entry(iter([pool(), pool()]), [Response(payload())], finalize)
        self.assertEqual(post.call_count, 1)
        initial = next(call.args[2] for call in snapshots.call_args_list if call.args[1] == 'entry_candidate')
        self.assertEqual(initial['trigger'], 'M15')
        self.assertEqual(initial['local_result']['action'], 'no')
        self.assertEqual(initial['local_result']['confidence'], 20)
        request = json.loads(post.call_args.kwargs['json']['messages'][1]['content'].split('\n', 1)[1])
        self.assertEqual(len(request['opportunities']), 2)
        self.assertEqual(calls[0][0], 101.)
        self.assertEqual(calls[0][1]['setup'], 'breakout')
        self.assertEqual(calls[0][1]['strategy_evidence']['buy']['evidence']['event_id'], 'event-B')

    def test_m5_low_score_or_failed_plan_records_pool_without_dispatch(self):
        for action, score, reason in (('no', 20, 'low score'), ('no', 95, 'reference plan failed'),
                                     ('buy', 20, 'explicit threshold guard')):
            with self.subTest(action=action, score=score):
                item = candidate('B', score=score)
                local = ai.base.StrategyResult(action, score, False, reason, 97., 109., score, 0,
                                                {'opportunities': [item]})
                with patch.object(self.trader, 'get_context', return_value=(frame(), frame(), frame())), \
                     patch.object(self.trader, 'evaluate_context', return_value=local), \
                     patch.object(ai.base, 'CONFIDENCE_THRESHOLD', 55), \
                     patch.object(ai, 'append_decision_snapshot') as snapshots, \
                     patch.object(self.trader, 'dispatch_entry') as dispatch:
                    self.trader.dispatch_m5_entry(1000)
                dispatch.assert_not_called()
                snapshots.assert_called_once()
                self.assertEqual(snapshots.call_args.args[1], 'local_evaluation')
                self.assertEqual(snapshots.call_args.args[2]['local_result']['details']['opportunities'][0]['opportunity_id'], 'B')

    def test_m5_executable_above_threshold_dispatches_once_with_pool_snapshot(self):
        local = ai.base.StrategyResult('buy', 80, False, 'valid', 97., 109., 80, 0,
                      {'opportunities': [candidate('B', score=80)]})
        with patch.object(self.trader, 'get_context', return_value=(frame(), frame(), frame())), \
             patch.object(self.trader, 'evaluate_context', return_value=local), \
             patch.object(ai.base, 'CONFIDENCE_THRESHOLD', 55), \
             patch.object(ai, 'append_decision_snapshot') as snapshots, \
             patch.object(self.trader, 'dispatch_entry') as dispatch:
            self.trader.dispatch_m5_entry(1000)
        dispatch.assert_called_once_with(1000, trigger='M5')
        snapshots.assert_called_once()

    def run_assembled_region_execution_guard(self, action):
        m15, m5, now, evidence = host_fixtures.OpportunityTests().region()
        if action == 'sell':
            from strategies.regions import region_facts
            def mirror(df):
                result = df.copy()
                result['open'], result['close'] = 200-df['open'], 200-df['close']
                result['high'], result['low'] = 200-df['low'], 200-df['high']
                return result
            m15, m5 = mirror(m15), mirror(m5)
            evidence = region_facts('rejection', m15, m5, now, 0, action, P)
        frozen = ai.base.verified_region_evidence(action, m15, m5, now, 0, evidence, P, 'rejection')
        contract = {'id': 'region_rejection', 'demo_only': True, 'stop_reference': 'region',
                    'entry_reference': 'closed_m5', 'regime_filter': 'none', 'target_max_reference_r': 2.5,
                    'parameters': P}
        selected = candidate('B', 100., 'region_rejection')
        selected['action'] = action
        selected['strategy_contract'] = contract
        selected['strategy_evidence'] = {action: {'blockers': [], 'evidence': frozen}}
        selected['reference_price'] = {'price': 100., 'source': 'confirmed_closed_m5_close', 'bar_time': int(m5.time.iloc[-1])}
        request = pool([selected]); request['m15_ema21'] = 100.
        request['h1_trend'] = request['m15_trend'] = 'bull' if action == 'buy' else 'bear'
        request['live_quote'] = {'bid': 100. if action == 'buy' else 99.9, 'ask': 100.1 if action == 'buy' else 100., 'time': 1001}
        value = payload('B', 0); value.update(action=action, stop_loss=97.54 if action == 'buy' else 102.46,
                                             take_profit=104.92 if action == 'buy' else 95.08)
        value['quality_assessment']['direction'] = action
        for item in value['quality_assessment']['dimensions'].values():
            item['evidence_refs'] = [ref.replace('/buy/', '/' + action + '/') for ref in item['evidence_refs']]
        captured = []
        # This intercept is after actual entry_task assembly. Execution guard and
        # the region host helper run unchanged; broker send is not performed.
        def place(result):
            captured.append(copy.deepcopy(result))
            self.assertEqual(self.trader.validate_execution_prices(result, SimpleNamespace(**request['live_quote'])), '')
            return True
        with patch.object(self.trader, 'ensure_review_bars_current'), \
             patch.object(self.trader, 'validate_strategy_entry', return_value=''), \
             patch.object(self.trader, 'get_live_quote', return_value=request['live_quote']), \
             patch.object(self.trader, 'place_order', side_effect=place), \
             patch.object(ai.base, 'm5_blocks_entry', return_value=(False, '')):
            post, snapshots, _ = self.run_entry(iter([request, request]),
                    [Response(value), Response({'decision': 'APPROVE', 'defects': [], 'reasoning': 'safe', 'fact_conflicts': []})],
                    frames=(m15, frame(), m5))
        self.assertEqual(post.call_count, 2, snapshots.call_args_list)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].details['reference_price'], 100.)
        self.assertEqual(captured[0].details['reference_price_metadata'], selected['reference_price'])
        self.assertEqual(captured[0].details['reference_price_source'], 'confirmed_closed_m5_close')

    def test_ai_buy_assembled_numeric_region_reference_runs_real_execution_guard(self):
        self.run_assembled_region_execution_guard('buy')

    def test_ai_sell_assembled_numeric_region_reference_runs_real_execution_guard(self):
        self.run_assembled_region_execution_guard('sell')

    def test_retry_success_pool_replaces_prior_source_and_rejects_stale_id(self):
        first, second = pool([candidate('A')]), pool([candidate('B', 101.)])
        with patch.dict(ai.os.environ, {'AI1_BACKUP_URL': 'https://backup.test/chat'}):
            post, snapshots, _ = self.run_entry(iter([first, first, second]),
                                              [ai.requests.ReadTimeout('slow'), Response(payload('A', 0))])
        self.assertEqual(post.call_count, 2)
        result = next(c.args[2] for c in snapshots.call_args_list if c.args[1] == 'ai1_result')
        self.assertEqual(result['status'], 'contract_invalid')
        self.assertIn('/opportunity_id', result['quality_error'])
        requests = [json.loads(c.kwargs['json']['messages'][1]['content'].split('\n', 1)[1]) for c in post.call_args_list]
        self.assertEqual([r['opportunities'][0]['opportunity_id'] for r in requests], ['A', 'B'])

    def test_retry_becomes_empty_no_more_post_and_no_api_alert(self):
        with patch.dict(ai.os.environ, {'AI1_BACKUP_URL': 'https://backup.test/chat'}):
            post, snapshots, alerts = self.run_entry(iter([pool(), pool(), pool([])]), [ai.requests.ReadTimeout('slow')])
        self.assertEqual(post.call_count, 1)
        alerts.assert_not_called()
        stages = [call.args[1] for call in snapshots.call_args_list]
        self.assertIn('local_prefilter', stages)
        self.assertIn('entry_stopped', stages)
        self.assertNotIn('ai_request_failed', stages)

    def test_schema_template_mandatory_id(self):
        self.assertIn('opportunity_id', ai.response_json_schema('AI-1')['required'])
        self.assertIn('opportunity_id', ai.ai1_json_template())

    def test_retry_same_id_binds_success_source_reference_and_original_markers(self):
        first = pool([candidate('B', 100.)]); second = pool([candidate('B', 102., 'breakout')])
        first['opportunities'][0]['strategy_evidence']['buy']['evidence']['source_version'] = 'old'
        second['opportunities'][0]['strategy_evidence']['buy']['evidence']['source_version'] = 'new'
        second['opportunities'][0]['strategy_evidence']['buy']['evidence']['host_verified_entry_event'] = {'action': 'buy', 'source': 'new'}
        value = payload('B', 0); value['quality_assessment']['dimensions']['trigger']['evidence_refs'] = ['/opportunities/0/strategy_evidence/buy/evidence/source_version']
        calls = []
        def finalize(value, reference, *args, **kwargs):
            calls.append((reference, kwargs['strategy_evidence']))
            return None, '观望止于测试'
        with patch.dict(ai.os.environ, {'AI1_BACKUP_URL': 'https://backup.test/chat'}):
            post, _, _ = self.run_entry(iter([first, first, second]), [ai.requests.ReadTimeout('slow'), Response(value)], finalize)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(calls[0][0], 102.)
        self.assertEqual(calls[0][1]['buy']['evidence']['host_verified_entry_event']['source'], 'new')

    def test_ai2_gate_expiration_or_newbars_no_transport(self):
        selected = ai.selected_opportunity_pack(pool(), ai.opportunity_selection(payload(), pool())[0])
        proposal = ai.base.StrategyResult('buy', 80, False, 'selected', 97., 109., 20, 0,
                    {'entry_price': 101., 'execution_guard': {'atr': 2.}})
        for gate in ('source expired', 'event already consumed'):
            with patch.object(self.trader, 'ensure_strategy_current'), patch.object(self.trader, 'ensure_review_bars_current'), \
                 patch.object(self.trader, 'validate_strategy_entry', return_value=gate), patch.object(ai.requests, 'post') as post:
                with self.assertRaises(ai.MarketSnapshotError):
                    ai.call_chat('AI-2', 'mock', 'low', 'rules', '', 10, ai.AI2_URL, 'mock',
                            refresh_request=lambda: self.trader.prepare_review_request(proposal, selected, payload(), 'fixed', 'd', 'M5'))
                post.assert_not_called()
        with patch.object(self.trader, 'ensure_strategy_current'), \
             patch.object(self.trader, 'get_context', return_value=(frame(), frame(), frame(1300))), \
             patch.object(ai.requests, 'post') as post:
            with self.assertRaises(ai.MarketSnapshotError):
                ai.call_chat('AI-2', 'mock', 'low', 'rules', '', 10, ai.AI2_URL, 'mock',
                        refresh_request=lambda: self.trader.prepare_review_request(proposal, selected, payload(), 'fixed', 'd', 'M5'))
            post.assert_not_called()

    def test_actual_ai1_ai2_pipeline_records_same_selected_event_and_contract(self):
        raw = pool()
        raw['opportunities'][1]['strategy_evidence']['buy']['evidence']['host_verified_entry_event'] = {'action': 'buy', 'event_id': 'event-B'}
        made = []
        def finalize(value, reference, *args, **kwargs):
            result = ai.base.StrategyResult('buy', 80, False, 'selected', 97., 109., 20, 0,
                       {'entry_price': reference, 'quality_assessment': value['quality_assessment'], 'quality_score': 80})
            made.append(result)
            return result, 'fixed safe plan'
        token = ai.base.execution_token('event-B', ai.base.SYMBOL, ai.base.MAGIC, 'buy')
        position = SimpleNamespace(ticket=123, identifier=123, symbol=ai.base.SYMBOL, magic=ai.base.MAGIC,
                       type=ai.base.mt5.POSITION_TYPE_BUY, comment=token, price_open=101., sl=97., tp=109.)
        def place(result):
            self.trader.record_entry_context(result, position)
            return True
        with patch.object(self.trader, 'ensure_review_bars_current'), \
             patch.object(self.trader, 'validate_strategy_entry', return_value=''), \
             patch.object(self.trader, 'get_live_quote', return_value=raw['live_quote']), \
             patch.object(self.trader, 'place_order', side_effect=place) as placed, \
             patch.object(self.trader, 'save_entry_contexts'):
            post, snapshots, alerts = self.run_entry(iter([raw, raw]),
                    [Response(payload()), Response({'decision': 'APPROVE', 'defects': [], 'reasoning': 'safe', 'fact_conflicts': []})], finalize)
        self.assertEqual(post.call_count, 2)
        placed.assert_called_once()
        result = made[0]
        self.assertEqual(result.details['opportunity_id'], 'B')
        self.assertEqual(result.details['event_id'], 'event-B')
        self.assertEqual(result.details['reference_entry_price'], 101.)
        self.assertEqual(result.details['reference_price_source'], 'confirmed_closed_m5_rejection_close')
        self.assertEqual(result.details['strategy_evidence']['buy']['evidence']['host_verified_entry_event']['event_id'], 'event-B')
        review = json.loads(post.call_args_list[1].kwargs['json']['messages'][1]['content'].split('\n', 1)[1])
        self.assertEqual(review['market_evidence']['opportunities'][0], {})
        context = self.trader.entry_contexts['123']
        self.assertEqual(context['opportunity_id'], 'B')
        self.assertEqual(context['strategy_contract']['id'], 'breakout')
        self.assertEqual(context['strategy_evidence']['buy']['evidence']['event_id'], 'event-B')
        self.assertEqual(context['event_id'], 'event-B')
        self.assertEqual(context['reference_price_metadata'], raw['opportunities'][1]['reference_price'])

    def test_selected_trigger_only_never_borrows_other_or_shared(self):
        pack = pool(); pack['opportunities'][1]['strategy_evidence']['buy']['evidence']['closed_trigger']['confirmed'] = False
        selected = {**pack, '_quality_opportunity_id': 'B'}
        self.assertFalse(ai.has_confirmed_trigger(selected, 'buy'))
        selected['_quality_opportunity_id'] = 'A'
        self.assertTrue(ai.has_confirmed_trigger(selected, 'buy'))

    def test_ai2_keeps_sparse_refs_frozen_contract_and_refreshes_every_post(self):
        pack = pool(); selected = ai.selected_opportunity_pack(pack, ai.opportunity_selection(payload(), pack)[0])
        proposal = ai.base.StrategyResult('buy', 80, False, 'selected', 97., 109., 20, 0,
                 {'entry_price': 101., 'reference_entry_price': 101., 'execution_guard': {'atr': 2.},
                  'strategy_contract': selected['strategy_contract'], 'strategy_evidence': selected['strategy_evidence'], 'opportunity_id': 'B'})
        quotes = iter([{'bid': 100.9, 'ask': 101., 'time': 1}, {'bid': 101., 'ask': 101.1, 'time': 2}])
        with patch.object(self.trader, 'ensure_strategy_current'), patch.object(self.trader, 'ensure_review_bars_current'), \
             patch.object(self.trader, 'validate_strategy_entry', return_value='') as gate, \
             patch.object(self.trader, 'get_live_quote', side_effect=lambda: next(quotes)), \
             patch.object(ai, 'append_decision_snapshot'), patch.dict(ai.os.environ, {'AI2_BACKUP_URL': 'https://backup.test/chat'}), \
             patch.object(ai.requests, 'post', side_effect=[ai.requests.ReadTimeout('slow'), Response({'decision': 'REJECT'})]) as post:
            ai.call_chat('AI-2', 'mock', 'low', 'rules', '', 10, ai.AI2_URL, 'mock',
                         refresh_request=lambda: self.trader.prepare_review_request(proposal, selected, payload(), 'fixed', 'd', 'M5'))
        self.assertEqual(gate.call_count, 2)
        for call in post.call_args_list:
            value = json.loads(call.kwargs['json']['messages'][1]['content'].split('\n', 1)[1])
            market = value['market_evidence']
            self.assertEqual(market['opportunities'][0], {})
            self.assertTrue(ai._evidence_pointer_exists(market, payload()['quality_assessment']['dimensions']['trigger']['evidence_refs'][0]))
            self.assertEqual(market['opportunity_id'], 'B')
            self.assertEqual(market['strategy_evidence']['buy']['evidence']['event_id'], 'event-B')
        self.assertEqual(proposal.stop_loss, 97.)
        self.assertEqual(selected['live_quote']['time'], 1001)

    def test_original_ai3_context_selection_and_no_unrelated_binding(self):
        result = ai.base.StrategyResult('buy', 80, False, 'selected', 97., 109., 20, 0,
                    {'opportunity_id': 'B', 'event_id': 'event-B', 'strategy_contract': candidate('B')['strategy_contract'],
                     'strategy_evidence': candidate('B')['strategy_evidence']})
        token = ai.base.execution_token('event-B', ai.base.SYMBOL, ai.base.MAGIC, 'buy')
        position = SimpleNamespace(ticket=123, identifier=123, symbol=ai.base.SYMBOL, magic=ai.base.MAGIC,
                       type=ai.base.mt5.POSITION_TYPE_BUY, comment=token, price_open=101., sl=97., tp=109.)
        with patch.object(self.trader, 'save_entry_contexts'):
            self.trader.record_entry_context(result, position)
        result.details['strategy_evidence']['buy']['evidence']['event_id'] = 'later'
        self.assertEqual(self.trader.entry_contexts['123']['opportunity_id'], 'B')
        self.assertEqual(self.trader.entry_contexts['123']['strategy_evidence']['buy']['evidence']['event_id'], 'event-B')
        result.details['strategy_evidence']['buy']['evidence']['event_id'] = 'event-B'
        unrelated = copy.copy(position); unrelated.ticket = 456; unrelated.comment = 'unrelated'
        self.trader.last_order_ticket = 0
        with patch.object(ai.base.FibVolumePATrader, 'place_order', return_value=True), \
             patch.object(self.trader, 'get_positions', return_value=[unrelated]), \
             patch.object(self.trader, 'record_entry_context') as record:
            self.assertTrue(self.trader.place_order(result))
        record.assert_not_called()

    def execute_real_order_matching(self, receipt_ticket, duplicate=False):
        """Run actual host reserve/send/receipt and AI matching; mock broker only."""
        from strategies.lifecycle import EventLedger
        m15, h1, m5, clock = market()
        now = clock.timestamp() + 5
        result = ai.base.build_fib_volume_pa_decision(m15, h1, m5, 55, now=clock, server_utc_offset=3,
                                                   strategy_snapshot=host_fixtures.legacy_session_snapshot())
        self.assertEqual(result.action, 'buy', result.reasoning)
        own = next(item for item in result.details['opportunities'] if item['action'] == 'buy')
        result.details.update(opportunity_id=own['opportunity_id'], event_id=own['event_id'],
                              strategy_evidence=copy.deepcopy(own['strategy_evidence']))
        original_contract = copy.deepcopy(own['strategy_contract'])
        token = ai.base.execution_token(own['event_id'], ai.base.SYMBOL, ai.base.MAGIC, 'buy')
        position = SimpleNamespace(ticket=123, identifier=123, symbol=ai.base.SYMBOL, magic=ai.base.MAGIC,
                       type=ai.base.mt5.POSITION_TYPE_BUY, comment='_SR_' + token,
                       price_open=float(result.details['entry_price']), sl=result.stop_loss, tp=result.take_profit,
                       time=int(now+10800), volume=.01, profit=0.)
        positions = [position]
        if duplicate:
            other = copy.copy(position); other.ticket = 456; other.identifier = 456
            positions.append(other)
        sent = False
        def broker_positions(**kwargs):
            return positions if sent else []
        def broker_send(request):
            nonlocal sent
            self.assertEqual(request['comment'], '_SR_' + token)
            sent = True
            return SimpleNamespace(retcode=ai.base.mt5.TRADE_RETCODE_DONE, order=receipt_ticket,
                                   price=position.price_open, comment='mock receipt')
        self.trader.strategy_ledger = EventLedger(str(Path(self.temp.name)/'matching-events.json'))
        tick = SimpleNamespace(bid=position.price_open, ask=position.price_open+.33, time=int(now+10800))
        with patch.object(ai.base.time, 'time', return_value=now), patch.object(ai.base, 'ENABLE_TRADING', True), \
             patch.object(ai.base.mt5, 'account_info', return_value=SimpleNamespace(trade_mode=ai.base.mt5.ACCOUNT_TRADE_MODE_DEMO)), \
             patch.object(ai.base.mt5, 'symbol_info_tick', return_value=tick), \
             patch.object(ai.base.mt5, 'positions_get', side_effect=broker_positions), \
             patch.object(ai.base.mt5, 'order_send', side_effect=broker_send) as send:
            self.assertTrue(self.trader.place_order(result))
            send.assert_called_once()
            state = self.trader.strategy_ledger.snapshot()
            recovered = ai.base.recovery_event(position, state, ai.base.SYMBOL, ai.base.MAGIC)
        self.assertEqual(self.trader.last_order_ticket, receipt_ticket)
        if duplicate:
            self.assertEqual(self.trader.entry_contexts, {})
            return
        context = self.trader.entry_contexts['123']
        self.assertEqual(context['opportunity_id'], own['opportunity_id'])
        self.assertEqual(context['event_id'], own['event_id'])
        self.assertEqual(context['strategy_contract'], original_contract)
        self.assertEqual(context['strategy_evidence'], own['strategy_evidence'])
        if receipt_ticket:
            self.assertEqual(state['positions']['123']['event_id'], own['event_id'])
            self.assertIsNone(recovered)  # Already bound; receipt branch must not rely on recovery.
        else:
            self.assertEqual(state['positions'], {})
            self.assertEqual(recovered[0], own['event_id'])

    def test_bound_exact_receipt_binds_original_context_via_real_order_path(self):
        self.execute_real_order_matching(123)

    def test_unbound_exact_token_without_receipt_recovers_original_context(self):
        self.execute_real_order_matching(0)

    def test_duplicate_exact_token_without_receipt_never_binds_context(self):
        self.execute_real_order_matching(0, duplicate=True)

    def test_region_finalize_preserves_verified_anchor_and_caps_obstacle(self):
        for action in ('buy', 'sell'):
            m15, m5, now, evidence = host_fixtures.OpportunityTests().region()
            frozen = ai.base.verified_region_evidence('buy', m15, m5, now, 0, evidence, P, 'rejection')
            if action == 'sell':
                verified = frozen['host_verified_region']; verified['action'] = 'sell'
                verified['risk_anchor']['price'] = 200 - verified['risk_anchor']['price']
                verified['protection_bars'] = [dict(b, high=200-b['low'], low=200-b['high']) for b in verified['protection_bars']]
                verified['target_obstacles'] = []
                m5 = None
            entry = 100.; atr = 2.
            required = ai.base.required_region_stop(action, entry, atr, m5, frozen, P)
            _, value = quality_sample(action); value['confidence'] = 80
            value['stop_loss'] = required - .1 if action == 'buy' else required + .1
            value['take_profit'] = 120. if action == 'buy' else 80.
            contract = {'stop_reference': 'region', 'entry_reference': 'closed_m5', 'regime_filter': 'none',
                        'target_max_reference_r': 2.5, 'parameters': P}
            with patch.object(ai.base, 'm5_blocks_entry', return_value=(False, '')), \
                 patch.object(ai.base, 'chase_blocks_at', return_value=(False, '')):
                result, reason = ai.finalize_ai_proposal(value, entry, atr, 'bull' if action == 'buy' else 'bear', 'bull' if action == 'buy' else 'bear', None,
                    df_m5=m5, strategy_contract=contract, strategy_evidence={action: {'evidence': frozen}})
            self.assertIsNotNone(result, reason)
            self.assertAlmostEqual(result.stop_loss, value['stop_loss'])
            self.assertEqual(ai.base.region_plan_error(action, entry, result.stop_loss, result.take_profit, atr, frozen), '')


if __name__ == '__main__':
    unittest.main()
