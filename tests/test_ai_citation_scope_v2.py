"""Short evidence tokens remain bound to the entire successful POST snapshot."""
import copy
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
import xauusd_fib_volume_pa_ai3 as ai
from strategies import citations
from test_ai_citations import bindings, cite, facts_pool
from test_ai_quality_bound_v5 import sample, opinion
from test_ai_response_contract import Response
import test_ai_strategy_opportunities as opportunity_tests


def wire(pack, side='buy'):
    _, value=sample(side)
    value['opportunity_id']=pack['opportunities'][1]['opportunity_id']
    result=cite(pack,value)
    result['evidence_scope']=pack['evidence_catalog']['scope']
    return result


class ActualOctober19Failures(unittest.TestCase):
    def archive(self,suffix):
        return json.loads((Path(__file__).parent/'fixtures'/('ai1_contract_20261019_'+suffix+'.json')).read_text(encoding='utf-8'))

    def test_extra_closing_brace_is_not_trimmed_or_repaired(self):
        archive=self.archive('113520')
        value,error=ai.parse_json_object(archive['raw_text'])
        self.assertIsNone(value)
        self.assertIn('Extra data',error)
        self.assertEqual(archive['raw'],None)
        self.assertTrue(ai.catalog_bindings(archive['request_pack']))
        self.assertGreater(archive['successful_input_timestamp'],'2026-10-19T11:35:03')

    def test_actual_wrong_hash_stays_invalid_under_original_v1_protocol(self):
        archive=self.archive('135048')
        raw,pack=archive['raw'],archive['request_pack']
        self.assertEqual(pack['evidence_catalog']['version'],'ai1-citations-v1')
        self.assertTrue(ai.catalog_bindings(pack))
        self.assertEqual(ai.parse_json_object(archive['raw_text']),(raw,''))
        schema=ai.response_json_schema('AI-1',pack)
        self.assertFalse(Draft202012Validator(schema).is_valid(raw))
        before=copy.deepcopy(raw)
        normalized,error=ai.normalize_ai1_quality(raw,pack)
        self.assertIn('3ab99c892b.16.1',error)
        self.assertNotIn('quality_score',normalized)
        self.assertEqual(raw,before)


class ScopedShortReferences(unittest.TestCase):
    def setUp(self):
        self.pack=ai.prepare_ai1_request_pack(facts_pool())

    def test_new_directory_uses_short_flat_tokens_and_scope_once_in_wire(self):
        catalog=self.pack['evidence_catalog']
        self.assertEqual(catalog['version'],'ai1-citations-v2')
        ids=bindings(self.pack)
        self.assertTrue(ids)
        self.assertTrue(all(re.fullmatch(r'r[0-9]+',key) for key in ids))
        self.assertEqual(list(ids),['r'+str(i) for i in range(len(ids))])
        raw=wire(self.pack)
        self.assertEqual(json.dumps(raw).count(catalog['scope']),1)
        self.assertFalse(any(catalog['scope'] in key for key in ids))

    def test_scope_is_required_and_singleton_in_actual_request_schema(self):
        schema=ai.response_json_schema('AI-1',self.pack)
        Draft202012Validator.check_schema(schema)
        self.assertEqual(schema['properties']['evidence_scope']['enum'],[self.pack['evidence_catalog']['scope']])
        self.assertIn('evidence_scope',schema['required'])
        raw=wire(self.pack)
        self.assertTrue(Draft202012Validator(schema).is_valid(raw))
        before=copy.deepcopy(raw)
        value,error=ai.normalize_ai1_quality(raw,self.pack)
        self.assertEqual(error,'')
        self.assertEqual((value['action'],value['quality_score']),('buy',80))
        self.assertEqual(raw,before)

    def test_missing_wrong_type_or_old_scope_is_rejected_without_a_score(self):
        raw=wire(self.pack)
        for mode in ('missing',None,123,'wrong-snapshot'):
            value=copy.deepcopy(raw)
            if mode=='missing': value.pop('evidence_scope')
            else: value['evidence_scope']=mode
            normalized,error=ai.normalize_ai1_quality(value,self.pack)
            self.assertIn('evidence_scope',error)
            self.assertNotIn('quality_score',normalized)

    def test_same_short_tokens_do_not_allow_replaying_previous_post(self):
        raw=wire(self.pack)
        changed=facts_pool()
        changed['live_quote']['ask']+=.02
        changed['snapshot_id']='new-successful-post'
        newer=ai.prepare_ai1_request_pack(changed)
        self.assertEqual(bindings(self.pack).keys(),bindings(newer).keys())
        self.assertNotEqual(self.pack['evidence_catalog']['scope'],newer['evidence_catalog']['scope'])
        value,error=ai.normalize_ai1_quality(raw,newer)
        self.assertIn('evidence_scope',error)
        self.assertNotIn('quality_score',value)

    def test_unknown_id_pointer_or_prefixed_id_cannot_bypass_v2_directory(self):
        for ref in ('r999999','/h1_trend',self.pack['evidence_catalog']['scope']+'.0.0'):
            raw=wire(self.pack)
            raw['quality_assessment']['dimensions']['structure']['evidence_refs']=[ref]
            value,error=ai.normalize_ai1_quality(raw,self.pack)
            self.assertTrue(error,ref)
            self.assertNotIn('quality_score',value)

    def test_other_candidate_still_fails_even_with_correct_snapshot_scope(self):
        raw=wire(self.pack)
        foreign=next(key for key,path in bindings(self.pack).items()
                     if path=='/opportunities/0/strategy_evidence/buy/evidence/volume')
        raw['quality_assessment']['dimensions']['structure']['evidence_refs']=[foreign]
        value,error=ai.normalize_ai1_quality(raw,self.pack)
        self.assertIn('隔离',error)
        self.assertNotIn('quality_score',value)

    def test_catalog_scope_content_or_version_cannot_be_tampered_with(self):
        for mode in ('facts','scope','version','refs'):
            changed=copy.deepcopy(self.pack)
            if mode=='facts': changed['live_quote']['ask']+=.01
            if mode=='scope': changed['evidence_catalog']['scope']='tampered'
            if mode=='version': changed['evidence_catalog']['version']='unknown-version'
            if mode=='refs':
                first=changed['evidence_catalog']['groups'][0]
                first['refs'][next(iter(first['refs']))]='/foreign'
            value,error=ai.normalize_ai1_quality(wire(self.pack),changed)
            self.assertTrue(error,mode)
            self.assertNotIn('quality_score',value)

    def test_legacy_catalog_stays_strict_and_is_not_upgraded_while_decoding(self):
        source=facts_pool()
        legacy=ai.compact_market_pack(source)
        legacy['evidence_catalog']=citations.build_catalog(legacy,version='ai1-citations-v1')
        _,raw=sample()
        raw['opportunity_id']=legacy['opportunities'][1]['opportunity_id']
        raw=cite(legacy,raw)
        self.assertNotIn('evidence_scope',raw)
        value,error=ai.normalize_ai1_quality(raw,legacy)
        self.assertEqual(error,'')
        self.assertEqual(value['quality_score'],80)
        self.assertTrue(all(key.startswith(legacy['evidence_catalog']['scope']+'.') for key in bindings(legacy)))
        self.assertNotIn('evidence_scope',ai.response_json_schema('AI-1',legacy)['properties'])

    def test_legal_blocked_watch_remains_watch_and_cannot_be_promoted_to_trade(self):
        raw=wire(self.pack)
        ref=raw['quality_assessment']['dimensions']['structure']['evidence_refs'][0]
        issue=opinion('protection','blocking',cause='hard_limit')
        issue['evidence_refs']=[ref]
        raw.update(action='no',stop_loss=None,take_profit=None,
                   wait_basis=[dict(code='hard_limit',reason='原硬限额阻断',evidence_refs=[ref])])
        raw['quality_assessment']['issues']=[issue]
        value,error=ai.normalize_ai1_quality(raw,self.pack)
        self.assertEqual(error,'')
        self.assertEqual((value['action'],value['quality_score']),('no',64))
        raw.update(action='buy',stop_loss=97.,take_profit=109.,wait_basis=[])
        value,error=ai.normalize_ai1_quality(raw,self.pack)
        self.assertIn('阻断',error)
        self.assertNotIn('quality_score',value)

    def test_schema_template_and_instructions_share_one_field_order_and_simple_tail(self):
        schema=ai.response_json_schema('AI-1',self.pack)
        template=ai.ai1_json_template()
        self.assertEqual(list(schema['properties']),list(template))
        self.assertEqual(list(template)[0],'quality_assessment')
        self.assertEqual(list(template)[-1],'reasoning')
        self.assertTrue(json.dumps(template,ensure_ascii=False,separators=(',',':')).endswith('"<本次结论>"}'))
        self.assertNotIn('末尾依次闭合最后维度',ai.AI1_INSTRUCTIONS)
        self.assertIn('evidence_scope',ai.AI1_INSTRUCTIONS)
        self.assertIn('evidence_scope',self.pack['quality_rubric']['reference_path_policy'])

    def test_buy_and_sell_keep_identical_scope_and_quality_rules(self):
        for side in ('buy','sell'):
            source=facts_pool()
            if side=='sell':
                source['h1_trend']=source['m15_trend']='bear'
                for candidate in source['opportunities']:
                    candidate['action']='sell'
                    candidate['strategy_evidence']={'sell':candidate['strategy_evidence']['buy']}
            request=ai.prepare_ai1_request_pack(source)
            raw=wire(request,side)
            value,error=ai.normalize_ai1_quality(raw,request)
            self.assertEqual(error,'')
            self.assertEqual((value['action'],value['quality_score']),(side,80))


class ScopedWireIntegration(unittest.TestCase):
    def test_bad_json_and_wrong_scope_stop_without_review_order_or_paid_repair(self):
        for mode in ('extra_brace','old_scope'):
            with self.subTest(mode=mode):
                fixture=opportunity_tests.OpportunityAITests()
                fixture.setUp()
                try:
                    sent=[]
                    def respond(*args,**kwargs):
                        request=kwargs['json']
                        body=json.loads(request['messages'][-1]['content'].split('\n',1)[1])
                        sent.append(body)
                        raw=wire(body)
                        if mode=='old_scope': raw['evidence_scope']='previous-post'
                        text=json.dumps(raw,ensure_ascii=False)
                        if mode=='extra_brace': text+='}'
                        return Response(text=text)
                    with patch.object(ai,'AI1_URL','https://unit.test/v1/messages'), \
                         patch.object(fixture.trader,'place_order') as order, \
                         patch.object(ai,'finalize_ai_proposal') as finalize:
                        post,snapshots,_=fixture.run_entry(iter([facts_pool(),facts_pool()]),respond)
                    self.assertEqual(post.call_count,1)
                    order.assert_not_called()
                    finalize.assert_not_called()
                    stages=[call.args[1] for call in snapshots.call_args_list]
                    self.assertNotIn('ai2_input',stages)
                    result=next(call.args[2] for call in snapshots.call_args_list if call.args[1]=='ai1_result')
                    self.assertEqual(result['status'],'contract_invalid')
                    self.assertNotIn('quality_score',result.get('parsed') or {})
                    self.assertEqual(sent[0]['evidence_catalog']['version'],'ai1-citations-v2')
                finally:
                    fixture.doCleanups()
