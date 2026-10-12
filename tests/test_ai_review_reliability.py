"""AI-2 transport/cost boundaries. No network or broker calls are made."""
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai
from test_ai_response_contract import Response


class ReviewReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for scope in (patch.dict(ai.os.environ, {}, clear=True),
                      patch.dict(ai._CHAT_OPTIONS, {}, clear=True),
                      patch.object(ai, '_CAP_BLOCKED_ROUTES', {}, create=True),
                      patch.object(ai, 'AI_USAGE_DIR', self.folder.name),
                      patch.object(ai, 'AI_HTTP_RETRIES', 5),
                      patch.object(ai, 'AI_READ_TIMEOUT_RETRIES', 0),
                      patch.object(ai.time, 'sleep')):
            scope.start()
            self.addCleanup(scope.stop)

    def call(self, role='AI-2', key='mock', model='review-model', effort='low', **kwargs):
        return ai.call_chat(role, model, effort, 'independent review', 'fresh evidence', 120,
                            'https://primary.test/v1/messages', key, **kwargs)

    def backup(self):
        ai.os.environ.update(AI2_BACKUP_URL='https://api.deepseek.com/anthropic/v1/messages',
                             AI2_BACKUP_KEY='mock-backup', FIB_PA_AI2_BACKUP_MODEL='backup-model')

    def events(self):
        return [json.loads(line) for path in Path(self.folder.name).glob('*.jsonl')
                for line in path.read_text(encoding='utf-8').splitlines()]

    def excessive(self, output=6045, finish='end_turn'):
        return Response(text='{"decision":"APPROVE","defects":[],"fact_conflicts":[],"reasoning":"safe"}',
                        usage={'output_tokens': output}, finish=finish)

    def test_ai2_disabled_thinking_preserves_cap_schema_and_requested_low(self):
        with patch.dict(ai.os.environ, {'FIB_PA_AI2_THINKING':'disabled'}), \
             patch.object(ai.requests, 'post', return_value=Response()) as post:
            self.call()
        data = post.call_args.kwargs['json']
        self.assertEqual(data['thinking'], {'type':'disabled'})
        self.assertNotIn('effort', data['output_config'])
        self.assertIn('format', data['output_config'])
        self.assertEqual(data['max_tokens'], 4096)
        event = self.events()[0]
        self.assertEqual(event['reasoning_effort_requested'], 'low')
        self.assertIsNone(event['reasoning_effort_sent'])
        self.assertEqual(event['thinking_mode_sent'], 'disabled')

    def test_ai2_backup_only_disabled_and_blank_timeout_inherits_primary(self):
        self.backup()
        with patch.dict(ai.os.environ, {'FIB_PA_AI2_BACKUP_THINKING':'disabled',
                                       'FIB_PA_AI2_BACKUP_TIMEOUT':''}), \
             patch.object(ai.requests, 'post', side_effect=[ai.requests.ReadTimeout('read timed out'),Response()]) as post:
            self.call()
        first, second = post.call_args_list
        self.assertNotIn('thinking', first.kwargs['json'])
        self.assertEqual(second.kwargs['json']['thinking'], {'type':'disabled'})
        self.assertNotIn('output_config', second.kwargs['json'])
        self.assertEqual([first.kwargs['timeout'], second.kwargs['timeout']], [120,120])

    def test_ai2_invalid_switch_fails_before_post(self):
        with patch.dict(ai.os.environ, {'FIB_PA_AI2_THINKING':'low'}), \
             patch.object(ai.requests, 'post') as post:
            with self.assertRaises(ai.AIRequestError): self.call()
        post.assert_not_called()

    def test_ai2_switch_does_not_change_ai1_or_override_explicit_ai3(self):
        with patch.dict(ai.os.environ, {'FIB_PA_AI2_THINKING':'disabled', 'FIB_PA_AI3_THINKING':''}), \
             patch.object(ai.requests, 'post', return_value=Response()) as post:
            self.call(role='AI-1'); self.call(role='AI-3')
        self.assertTrue(all('thinking' not in c.kwargs['json'] for c in post.call_args_list))

    def test_ai2_unsupported_switch_is_not_removed_and_backup_inherits(self):
        self.backup()
        with patch.dict(ai.os.environ, {'FIB_PA_AI2_THINKING':'disabled'}), \
             patch.object(ai.requests, 'post', side_effect=[Response(error='Unsupported thinking.type'),Response()]) as post:
            self.call()
        self.assertEqual(post.call_count,2)
        self.assertTrue(all(c.kwargs['json']['thinking']=={'type':'disabled'} for c in post.call_args_list))

    def test_ai2_format_recovery_keeps_disabled_and_refreshes_evidence(self):
        snapshots=iter([{'prompt':'fresh-1'},{'prompt':'fresh-2'}])
        with patch.dict(ai.os.environ, {'FIB_PA_AI2_THINKING':'disabled'}), \
             patch.object(ai.requests, 'post', side_effect=[Response(error='output_config.format unsupported'),Response()]) as post:
            self.call(refresh_request=lambda:next(snapshots))
        first,second=[c.kwargs['json'] for c in post.call_args_list]
        self.assertEqual(second['thinking'],first['thinking'])
        self.assertEqual(second['max_tokens'],4096)
        self.assertEqual(second['messages'][0]['content'],'fresh-2')

    def test_cap_violation_rejects_this_round_and_prevents_repeated_paid_posts(self):
        with patch.object(ai.requests,'post',return_value=self.excessive()) as post:
            with self.assertRaises(ai.AIRequestError) as error: self.call()
            with self.assertRaises(ai.AIRequestError): self.call()
        self.assertEqual(post.call_count,1)
        for value in ('6045','4096','primary.test','review-model'):
            self.assertIn(value,error.exception.detail)
        self.assertEqual(len(self.events()),1)

    def test_quarantined_primary_next_round_uses_backup_with_fresh_request_and_full_http_budget(self):
        self.backup()
        with patch.object(ai.requests,'post',return_value=self.excessive()) as post:
            with self.assertRaises(ai.AIRequestError): self.call()
        self.assertEqual(post.call_count,1)  # No paid rejudgement in the bad round.
        refresh=[]
        def snapshot():
            refresh.append('new-round')
            return {'prompt':'new-round-live-price'}
        with patch.object(ai.requests,'post',side_effect=[ai.requests.ConnectTimeout('connect timed out')]*4+[Response()]) as post:
            self.call(refresh_request=snapshot)
        self.assertEqual(post.call_count,5)
        self.assertEqual(len(refresh),5)
        self.assertTrue(all(c.args[0].startswith('https://api.deepseek.com/') for c in post.call_args_list))
        self.assertEqual(self.events()[-1]['attempt'],5)

    def test_both_cap_violating_routes_stop_future_post_without_manufacturing_approval(self):
        self.backup()
        with patch.object(ai.requests,'post',return_value=self.excessive()) as post:
            for _ in range(3):
                with self.assertRaises(ai.AIRequestError): self.call()
        self.assertEqual(post.call_count,2)
        self.assertEqual([e['channel'] for e in self.events()],['primary','backup'])

    def test_cap_guard_is_scoped_to_role_credential_model_and_sent_parameters(self):
        with patch.object(ai.requests,'post',return_value=self.excessive()):
            with self.assertRaises(ai.AIRequestError): self.call()
        with patch.object(ai.requests,'post',return_value=Response()) as post:
            self.call(role='AI-1')
            self.call(key='other-mock-key')
            self.call(model='other-review-model')
            self.call(effort='medium')
            with patch.dict(ai.os.environ,{'FIB_PA_AI2_THINKING':'disabled'}): self.call()
        self.assertEqual(post.call_count,5)

    def test_cap_guard_also_remembers_overrun_on_truncated_response_without_rejudgement(self):
        self.backup()
        with patch.object(ai.requests,'post',return_value=self.excessive(finish='max_tokens')) as post:
            with self.assertRaises(ai.AIRequestError): self.call()
        self.assertEqual(post.call_count,1)
        self.assertTrue(self.events()[0]['token_limit_violation'])
        with patch.object(ai.requests,'post',return_value=Response()) as post: self.call()
        self.assertTrue(post.call_args.args[0].startswith('https://api.deepseek.com/'))

    def test_stale_overrun_is_discarded_but_cost_guard_still_remembers(self):
        alive=[True]
        def reply(*args,**kwargs):
            alive[0]=False
            return self.excessive()
        with patch.object(ai.requests,'post',side_effect=reply):
            self.assertEqual(self.call(still_current=lambda:alive[0]),'')
        self.assertEqual(self.events()[0]['outcome'],'stale_response')
        self.assertTrue(self.events()[0]['token_limit_violation'])
        with patch.object(ai.requests,'post') as post:
            with self.assertRaises(ai.AIRequestError): self.call()
        post.assert_not_called()

    def test_read_timeout_summary_identifies_each_route_and_its_actual_wait(self):
        self.backup()
        with patch.dict(ai.os.environ,{'FIB_PA_AI2_BACKUP_TIMEOUT':'40'}), \
             patch.object(ai.requests,'post',side_effect=ai.requests.ConnectionError('Read timed out')) as post:
            with self.assertRaises(ai.AIRequestError) as error: self.call()
        self.assertEqual(post.call_count,2)
        for value in ('primary','backup','120','40','primary.test','api.deepseek.com'):
            self.assertIn(value,error.exception.detail)

    def test_usage_includes_loaded_code_identity_and_raw_reported_token_fields(self):
        with patch.object(ai.requests,'post',return_value=Response(usage={'output_tokens':90})):
            self.call()
        event=self.events()[0]
        self.assertEqual(event['usage_reported'],{'output_tokens':90})
        self.assertEqual(event['citation_version'],'ai1-citations-v1')
        self.assertTrue(event['client_code_signature'])
        self.assertFalse(event['token_limit_violation'])

    def test_skipping_blocked_route_does_not_refresh_or_consume_analysis_scope(self):
        with patch.object(ai.requests,'post',return_value=self.excessive()):
            with self.assertRaises(ai.AIRequestError): self.call()
        refresh=Mock()
        with patch.object(ai.requests,'post') as post:
            with self.assertRaises(ai.AIRequestError): self.call(refresh_request=refresh)
        post.assert_not_called()
        refresh.assert_not_called()

    def test_error_response_with_excessive_usage_does_not_trigger_paid_backup(self):
        self.backup()
        reply=Response(error='output_config.format unsupported')
        reply.body['usage']={'output_tokens':6045}
        with patch.object(ai.requests,'post',return_value=reply) as post:
            with self.assertRaises(ai.AIRequestError): self.call()
        self.assertEqual(post.call_count,1)
        self.assertTrue(self.events()[0]['token_limit_violation'])

    def test_cap_guard_tracks_explicit_cap_change(self):
        with patch.object(ai.requests,'post',return_value=self.excessive()):
            with self.assertRaises(ai.AIRequestError): self.call()
        with patch.dict(ai.AI_MAX_TOKENS,{'AI-2':2048}), \
             patch.object(ai.requests,'post',return_value=Response()) as post:
            self.call()
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'],2048)

    def test_raw_usage_filters_private_text_and_nonfinite_counters(self):
        usage={'output_tokens':90,'prompt_tokens_details':{'cached_tokens':7,'thinking_tokens':'PRIVATE'},
               'thinking_tokens':'PRIVATE','secret':'MOCK-KEY','invalid_tokens':float('nan')}
        with patch.object(ai.requests,'post',return_value=Response(usage=usage)):
            self.call()
        report=self.events()[0]['usage_reported']
        self.assertEqual(report,{'output_tokens':90,'prompt_tokens_details':{'cached_tokens':7}})
        self.assertNotIn('PRIVATE',json.dumps(report))

    def test_api_failure_notification_identifies_loaded_code(self):
        trader=ai.ThreeAITrader.__new__(ai.ThreeAITrader)
        trader._last_api_alert=0
        with patch.object(trader,'notify_lines') as notify:
            trader.alert_api_failure('AI-2','mock failure')
        body=' '.join(notify.call_args.args[1])
        self.assertIn(ai.CLIENT_CODE_SIGNATURE,body)
        self.assertIn(ai.AI1_CITATION_VERSION,body)

    def test_shared_guard_protects_ai3_without_changing_its_thinking_setting(self):
        with patch.object(ai.requests,'post',return_value=self.excessive()) as post:
            for _ in range(2):
                with self.assertRaises(ai.AIRequestError): self.call(role='AI-3')
        self.assertEqual(post.call_count,1)
        self.assertNotIn('thinking',post.call_args.kwargs['json'])
