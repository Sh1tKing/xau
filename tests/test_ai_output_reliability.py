"""Synced Oct 13 failure regressions; no terminal, network, or paid regeneration."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jsonschema import Draft202012Validator
import xauusd_fib_volume_pa_ai3 as ai
from test_ai_response_contract import Response
from test_ai_quality_contract_v3 import current_card


class OutputReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = json.loads((Path(__file__).parent / 'fixtures/ai_response_failures_20261013.json').read_text(encoding='utf-8'))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for scope in (patch.dict(ai.os.environ, {}, clear=True),
                      # Role limits are loaded at import; clearing environ alone
                      # must not let a private deployment .env change this fixture.
                      patch.dict(ai.AI_MAX_TOKENS, {'AI-1':4096, 'AI-2':4096, 'AI-3':2048}),
                      patch.dict(ai._CHAT_OPTIONS, {}, clear=True),
                      patch.dict(ai._CAP_BLOCKED_ROUTES, {}, clear=True),
                      patch.object(ai, 'AI_USAGE_DIR', folder.name),
                      patch.object(ai.time, 'sleep')):
            scope.start()
            self.addCleanup(scope.stop)

    def events(self):
        return [json.loads(line) for path in self.folder.glob('*.jsonl')
                for line in path.read_text(encoding='utf-8').splitlines()]

    def call(self, role='AI-1', **kwargs):
        return ai.call_chat(role, 'claude-unit', 'medium', ai.AI1_INSTRUCTIONS,
                            'fresh market', 120, 'https://relay.test/v1/messages', 'mock', **kwargs)

    def test_actual_native_thinking_breakdown_is_reported(self):
        row = self.archive['usage'][1]
        usage = ai.normalized_chat_usage({'type': 'message', 'usage': row['usage_reported']})
        self.assertEqual(usage['reasoning_tokens'], 3623)
        self.assertEqual(usage['non_reasoning_output_tokens'], 473)
        self.assertEqual(usage['output_tokens'], 4096)
        self.assertEqual(usage['reasoning_source'], 'output_tokens_details.thinking_tokens')
        self.assertEqual(usage['input_tokens'], 31445)

    def test_invalid_or_conflicting_reasoning_is_not_guessed(self):
        for counter in (True, -1, float('nan'), float('inf'), '12'):
            with self.subTest(counter=counter):
                usage = ai.normalized_chat_usage({'type': 'message', 'usage': {
                    'output_tokens': 100, 'output_tokens_details': {'thinking_tokens': counter}}})
                self.assertIsNone(usage['reasoning_tokens'])
                self.assertIsNone(usage['non_reasoning_output_tokens'])
        for details in ({'thinking_tokens': 101}, {'thinking_tokens': 20, 'reasoning_tokens': 30}):
            usage = ai.normalized_chat_usage({'type': 'message', 'usage': {
                'output_tokens': 100, 'output_tokens_details': details}})
            self.assertTrue(usage['reasoning_usage_inconsistent'])
            self.assertIsNone(usage['non_reasoning_output_tokens'])

    def test_reasoning_source_names_the_actual_provider_field(self):
        for native in (True, False):
            for field in ('completion_tokens_details', 'output_tokens_details'):
                with self.subTest(native=native, field=field):
                    body = {'usage': {'output_tokens': 100, field: {'thinking_tokens': 20}}}
                    if native:
                        body['type'] = 'message'
                    usage = ai.normalized_chat_usage(body)
                    self.assertEqual(usage['reasoning_source'], field + '.thinking_tokens')
                    self.assertEqual(usage['non_reasoning_output_tokens'], 80)

    def test_ai1_thinking_switch_is_independent_and_backup_inherits(self):
        with patch.dict(ai.os.environ, {'FIB_PA_AI1_THINKING': 'disabled',
                                       'AI1_BACKUP_URL': 'https://backup.test/v1/messages',
                                       'AI1_BACKUP_KEY': 'backup-mock'}), \
             patch.object(ai.requests, 'post', side_effect=[ai.requests.ReadTimeout('slow'), Response()]) as post:
            self.call()
        for request in post.call_args_list:
            data = request.kwargs['json']
            self.assertEqual(data['thinking'], {'type': 'disabled'})
            self.assertNotIn('effort', data['output_config'])
            self.assertEqual(data['max_tokens'], 4096)
            self.assertIn('format', data['output_config'])
        self.assertTrue(all(e['reasoning_effort_requested'] == 'medium' for e in self.events()))
        self.assertTrue(all(e['thinking_control_version'] == ai.AI_OUTPUT_RELIABILITY_VERSION for e in self.events()))
        self.assertIsNone(ai.request_thinking_mode('AI-2'))
        self.assertIsNone(ai.request_thinking_mode('AI-3'))

    def test_backup_only_ai1_switch_and_invalid_setting(self):
        with patch.dict(ai.os.environ, {'FIB_PA_AI1_BACKUP_THINKING': 'disabled'}):
            self.assertIsNone(ai.request_thinking_mode('AI-1'))
            self.assertEqual(ai.request_thinking_mode('AI-1', True), 'disabled')
        with patch.dict(ai.os.environ, {'FIB_PA_AI1_THINKING': 'low'}), patch.object(ai.requests, 'post') as post:
            with self.assertRaises(ai.AIRequestError):
                self.call()
        post.assert_not_called()

    def test_disabled_rejection_is_not_silently_removed(self):
        with patch.dict(ai.os.environ, {'FIB_PA_AI1_THINKING': 'disabled'}), \
             patch.object(ai.requests, 'post', return_value=Response(error='Unsupported thinking.type')) as post:
            with self.assertRaisesRegex(ai.AIRequestError, '思考开关'):
                self.call()
        self.assertEqual(post.call_count, 1)

    def test_explicit_between_tools_mode_keeps_medium_and_schema_without_tools(self):
        with patch.dict(ai.os.environ, {'FIB_PA_AI1_THINKING': 'between_tools'}), \
             patch.object(ai.requests, 'post', return_value=Response()) as post:
            self.call()
        data = post.call_args.kwargs['json']
        self.assertEqual(data['thinking'], {'type': 'between_tools'})
        self.assertEqual(data['output_config']['effort'], 'medium')
        self.assertIn('format', data['output_config'])
        self.assertEqual(data['max_tokens'], 4096)
        self.assertNotIn('tools', data)
        self.assertNotIn('tool_choice', data)
        self.assertEqual(self.events()[0]['thinking_mode_sent'], 'between_tools')

    def test_between_tools_invalid_protocol_or_effort_fails_before_post(self):
        with patch.dict(ai.os.environ, {'FIB_PA_AI1_THINKING': 'between_tools'}), \
             patch.object(ai.requests, 'post') as post:
            for url, effort in (('https://relay.test/v1/chat/completions', 'medium'),
                                ('https://relay.test/v1/messages', 'max')):
                with self.subTest(url=url, effort=effort), self.assertRaises(ai.AIRequestError):
                    ai.call_chat('AI-1', 'claude-unit', effort, 'rules', 'market', 120, url, 'mock')
        post.assert_not_called()
        with patch.dict(ai.os.environ, {'FIB_PA_AI2_THINKING': 'between_tools'}):
            with self.assertRaises(ai.AIRequestError): ai.request_thinking_mode('AI-2')

    def test_between_tools_rejected_by_provider_is_not_rewritten(self):
        with patch.dict(ai.os.environ, {'FIB_PA_AI1_THINKING': 'between_tools'}), \
             patch.object(ai.requests, 'post', return_value=Response(error='thinking.type between_tools unsupported')) as post:
            with self.assertRaises(ai.AIRequestError) as error:
                self.call()
        self.assertIn('thinking=between_tools', error.exception.detail)
        self.assertEqual(post.call_count, 1)

    def test_actual_bad_cards_remain_invalid_without_repairs(self):
        self.assertEqual(len(self.archive['cards']), 5)
        for card in self.archive['cards']:
            before = copy.deepcopy(card)
            parsed, error = ai.parse_json_object(card['raw'])
            if card['json_error']:
                self.assertIsNone(parsed)
                self.assertIn('JSON语法错误', error)
            else:
                shape_error = ai.response_schema_error(parsed, ai.response_json_schema('AI-1', quality_version=ai.DIMENSIONAL_QUALITY_VERSION))
                self.assertIn('/quality_assessment/dimensions/structure', shape_error)
                self.assertIn('observations', shape_error)
                self.assertEqual(parsed['action'], 'no')
            self.assertEqual(card, before)

    def test_local_schema_diagnostics_match_real_quality_schema(self):
        _, card = current_card()
        schema = ai.response_json_schema('AI-1', quality_version=ai.DIMENSIONAL_QUALITY_VERSION)
        samples = [card]
        for update in ('missing_array', 'extra_field', 'strong_extra', 'bad_level', 'empty_refs', 'boolean_price', 'wrong_wait_layer'):
            sample = copy.deepcopy(card)
            item = sample['quality_assessment']['dimensions']['structure']
            if update == 'missing_array': item.pop('observations')
            elif update == 'extra_field': item['invented'] = 'x'
            elif update == 'strong_extra': item['level'] = 'strong'
            elif update == 'bad_level': item['level'] = 'excellent'
            elif update == 'empty_refs': item['evidence_refs'] = []
            elif update == 'boolean_price': sample['stop_loss'] = True
            else: sample['quality_assessment']['wait_basis'] = sample.pop('wait_basis')
            samples.append(sample)
        for sample in samples:
            before = copy.deepcopy(sample)
            self.assertEqual(not ai.response_schema_error(sample, schema), Draft202012Validator(schema).is_valid(sample))
            self.assertEqual(sample, before)

    def test_success_http_does_not_claim_schema_enforced_for_invalid_card(self):
        card = self.archive['cards'][0]
        with patch.object(ai.requests, 'post', return_value=Response(text=card['raw'])) as post:
            self.assertEqual(self.call(refresh_request=lambda: {
                'prompt': 'historical v3 request',
                'response_schema': ai.response_json_schema('AI-1', quality_version=ai.DIMENSIONAL_QUALITY_VERSION)}), card['raw'])
        event = self.events()[0]
        self.assertEqual(event['response_shape_status'], 'invalid_shape')
        self.assertEqual(event['schema_enforcement'], 'violated')
        self.assertIn('/structure', event['response_shape_error'])
        self.assertEqual(post.call_count, 1)

    def test_actual_truncation_has_thinking_budget_diagnostic_and_no_retry(self):
        row = self.archive['usage'][1]
        with patch.object(ai.requests, 'post', return_value=Response(finish='max_tokens', usage=row['usage_reported'])) as post:
            with self.assertRaises(ai.AIRequestError) as error:
                self.call()
        self.assertIn('3623', error.exception.detail)
        self.assertIn('473', error.exception.detail)
        self.assertEqual(self.events()[0]['outcome'], 'truncated')
        self.assertEqual(post.call_count, 1)

    def test_actual_ai2_overrun_remains_quarantined_with_existing_backup(self):
        row = self.archive['usage'][0]
        with patch.dict(ai.os.environ, {'FIB_PA_AI2_THINKING': 'disabled',
                                       'AI2_BACKUP_URL': 'https://backup.test/v1/messages',
                                       'AI2_BACKUP_KEY': 'backup-mock'}), \
             patch.object(ai.requests, 'post', return_value=Response(text='{}', usage=row['usage_reported'])) as post:
            with self.assertRaises(ai.AIRequestError): self.call('AI-2')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(ai._CAP_BLOCKED_ROUTES), 1)
        self.assertEqual(self.events()[0]['outcome'], 'token_limit_exceeded')

    def test_schema_description_states_empty_arrays_explicitly(self):
        schema = ai.response_json_schema('AI-1')
        self.assertEqual(set(schema['$defs']['quality_dimension']['properties']),
                         {'support', 'reason', 'evidence_refs'})
        self.assertIn('issues', schema['properties']['quality_assessment']['required'])
        template = ai.ai1_json_template()
        self.assertLess(list(template).index('quality_assessment'), list(template).index('wait_basis'))
        self.assertEqual(list(template)[-1],'reasoning')
        self.assertNotIn('wait_basis', template['quality_assessment'])
        self.assertEqual(template['quality_assessment']['issues'], [])


if __name__ == '__main__':
    unittest.main()
