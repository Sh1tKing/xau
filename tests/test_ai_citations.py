"""Request-bound citation contract. Every HTTP/MT5 call is mocked."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import xauusd_fib_volume_pa_ai3 as ai
from test_ai_strategy_opportunities import pool,payload
import test_ai_strategy_opportunities as opportunity_tests
from test_ai_response_contract import Response


def facts_pool():
    pack=pool()
    for c in pack['opportunities']:
        side=c['action']
        c['strategy_evidence'][side]['evidence'].update(
            position={'ema_distance_atr':.3},recent_extremes={'m5_protection_low':98.},
            bar_behavior={'m5_opposes_action':False},volume={'relative_tick_volume':.85})
        c['strategy_contract']['target_max_reference_r']=3.
    pack.update(m15_ema21_distance_atr=.3,rules={'max_stop_abs':8.,'price_finalization':{
        'generation_min_risk':1.2,'target_max_reference_r':3.}},quality_rubric=ai.quality_rubric(True))
    return pack


def bindings(pack):
    return {identifier:group['base']+suffix for group in pack['evidence_catalog']['groups']
            for identifier,suffix in group['refs'].items()}


def cite(pack,raw):
    result=copy.deepcopy(raw)
    by_path={path:identifier for identifier,path in bindings(pack).items()}
    def visit(value):
        if isinstance(value,dict):
            for k,v in value.items():
                if k=='evidence_refs': value[k]=[by_path[r] for r in v]
                else: visit(v)
        elif isinstance(value,list):
            for item in value: visit(item)
    visit(result)
    return result


class CitationTests(unittest.TestCase):
    def test_real_shared_risk_and_ema_facts_are_not_foreign_candidate_aliases(self):
        for ref in ('/m15_ema21_distance_atr','/rules/max_stop_abs','/rules/price_finalization/generation_min_risk'):
            raw=payload()
            raw['quality_assessment']['dimensions']['protection']['evidence_refs']=[ref]
            self.assertEqual(ai.normalize_ai1_quality(raw,facts_pool())[1],'',ref)

    def test_catalog_contains_exact_nested_volume_position_and_contract_not_guessed_aliases(self):
        source=facts_pool(); before=copy.deepcopy(source)
        request=ai.prepare_ai1_request_pack(source)
        paths=set(bindings(request).values())
        self.assertEqual(source,before)
        for path in ('/opportunities/0/strategy_evidence/buy/evidence/volume',
                     '/opportunities/0/strategy_evidence/buy/evidence/position',
                     '/opportunities/0/strategy_contract/target_max_reference_r'):
            self.assertIn(path,paths)
        self.assertNotIn('/local_plan_feasibility',paths)
        self.assertNotIn('reference_paths',request['quality_rubric'])
        self.assertTrue(all(ai._evidence_pointer_exists(request,p) for p in paths))

    def test_declared_ids_decode_to_original_paths_without_changing_action_score_or_input(self):
        pack=ai.prepare_ai1_request_pack(facts_pool()); raw=cite(pack,payload())
        original=copy.deepcopy(raw)
        result,error=ai.normalize_ai1_quality(raw,pack)
        self.assertEqual(error,'')
        self.assertEqual(raw,original)
        self.assertEqual((result['action'],result['confidence']),('buy',80))
        self.assertEqual(result['quality_assessment']['dimensions']['trigger']['evidence_refs'],
                         payload()['quality_assessment']['dimensions']['trigger']['evidence_refs'])

    def test_no_decision_wait_and_reservation_ids_keep_action_and_score(self):
        pack=ai.prepare_ai1_request_pack(facts_pool()); raw=payload()
        raw['action']='no'
        raw['wait_basis']=[{'code':'target_space','reason':'Wait for more room',
                           'evidence_refs':['/opportunities/1/strategy_contract/target_max_reference_r']}]
        raw['quality_assessment']['dimensions']['structure']['reservations']=[{
            'cause':'volume','event_group':'one-bar','reason':'Ordinary volume',
            'evidence_refs':['/opportunities/1/strategy_evidence/buy/evidence/volume']}]
        result,error=ai.normalize_ai1_quality(cite(pack,raw),pack)
        self.assertEqual(error,'')
        self.assertEqual((result['action'],result['confidence']),('no',80))
        self.assertEqual(result['wait_basis'],raw['wait_basis'])
        self.assertEqual(result['quality_assessment']['reservations'][0]['severity'],'minor')

    def test_foreign_candidate_ids_unknown_ids_and_ids_from_previous_snapshot_are_rejected(self):
        pack=ai.prepare_ai1_request_pack(facts_pool()); ids=bindings(pack)
        old=next(k for k,v in ids.items() if v=='/h1_trend')
        changed=facts_pool(); changed['live_quote']['ask']+=.01
        newer=ai.prepare_ai1_request_pack(changed)
        foreign=next(k for k,v in ids.items() if v=='/opportunities/0/strategy_evidence/buy/evidence/volume')
        for ref,source in ((foreign,pack),('invented',pack),(old,newer)):
            value=payload();value['quality_assessment']['dimensions']['trigger']['evidence_refs']=[ref]
            self.assertTrue(ai.normalize_ai1_quality(value,source)[1],ref)

    def test_bad_old_json_pointers_stay_invalid_and_are_never_repaired(self):
        pack=ai.prepare_ai1_request_pack(facts_pool())
        for ref in ('/opportunities/1/strategy_evidence/buy/position',
                    '/opportunities/1/strategy_evidence/buy/evidence/bar_behavior/volume',
                    '/opportunities/1/target_max_reference_r','/local_plan_feasibility',
                    '/rules/price_finalization/target_max_reference_r'):
            raw=payload();raw['quality_assessment']['dimensions']['structure']['evidence_refs']=[ref]
            self.assertTrue(ai.normalize_ai1_quality(raw,pack)[1],ref)

    def test_catalog_cannot_be_mutated_to_expose_hidden_or_removed_data(self):
        source=facts_pool()
        source['opportunities'][1]['strategy_evidence']['buy']['evidence']['host_verified_analysis_checkpoint']={'hidden':123}
        pack=ai.prepare_ai1_request_pack(source)
        self.assertFalse(any('host_verified' in p for p in bindings(pack).values()))
        identifier=next(iter(bindings(pack)))
        pack['evidence_catalog']['groups'][0]['refs'][identifier]='/local_plan_feasibility'
        raw=payload();raw['quality_assessment']['dimensions']['trigger']['evidence_refs']=[identifier]
        self.assertTrue(ai.normalize_ai1_quality(raw,pack)[1])

    def test_pointer_escaping_and_list_indices_are_strict(self):
        pack={'a/b':{'x~y':0},'a~2b':5,'list':[False]}
        self.assertTrue(ai._evidence_pointer_exists(pack,'/a~1b/x~0y'))
        self.assertTrue(ai._evidence_pointer_exists(pack,'/list/0'))
        for ref in ('/a~2b','/list/-1','/list/01','/list/００','/list/1'):
            self.assertFalse(ai._evidence_pointer_exists(pack,ref),ref)

    def test_schema_has_one_reference_enum_and_binds_candidate_ids_for_dimensions_reservations_waits(self):
        pack=ai.prepare_ai1_request_pack(facts_pool())
        schema=ai.response_json_schema('AI-1',pack)
        refs=schema['$defs']['evidence_ref']['enum']
        self.assertEqual(set(refs),set(bindings(pack)))
        self.assertEqual(schema['properties']['opportunity_id']['enum'],['A','B'])
        self.assertEqual(json.dumps(schema).count(json.dumps(refs)),1)
        def refs_at(value):
            if isinstance(value,dict):
                if 'evidence_refs' in value.get('properties',{}):
                    yield value['properties']['evidence_refs']['items']
                for child in value.values(): yield from refs_at(child)
            elif isinstance(value,list):
                for child in value: yield from refs_at(child)
        self.assertTrue(all(v=={'$ref':'#/$defs/evidence_ref'} for v in refs_at(schema)))

    def test_prompt_and_schema_refresh_together_after_http_schema_compatibility_recovery(self):
        packs=[ai.prepare_ai1_request_pack(facts_pool()),None]
        other=facts_pool();other['live_quote']['ask']+=.01
        packs[1]=ai.prepare_ai1_request_pack(other)
        snapshots=iter([{'prompt':json.dumps(p),'response_schema':ai.response_json_schema('AI-1',p)} for p in packs])
        with tempfile.TemporaryDirectory() as folder,patch.object(ai,'AI_USAGE_DIR',folder), \
                patch.dict(ai._CHAT_OPTIONS,{},clear=True),patch.object(ai.time,'sleep'), \
                patch.object(ai.requests,'post',side_effect=[Response(error='output_config.format json_schema unsupported'),Response()]) as post, \
                patch.object(ai,'backup_chat_config',return_value={'model':'model','effort':'low','url':'https://relay.test/v1/messages','key':'mock'}):
            ai.call_chat('AI-1','model','low','system','old',10,'https://relay.test/v1/messages','mock',refresh_request=lambda:next(snapshots))
        one,two=[call.kwargs['json'] for call in post.call_args_list]
        self.assertEqual(one['output_config']['format']['schema'],ai.response_json_schema('AI-1',packs[0]))
        self.assertNotIn('format',two.get('output_config',{}))
        self.assertIn(packs[1]['evidence_catalog']['scope'],two['messages'][0]['content'])

    def test_complete_messages_entry_and_review_use_successful_post_ids_even_after_failover(self):
        for failover in (False,True):
            with self.subTest(failover=failover):
                fixture=opportunity_tests.OpportunityAITests()
                fixture.setUp()
                try:
                    first=facts_pool(); second=copy.deepcopy(first)
                    second['live_quote']['ask']+=.02
                    requests=[]; captured=[]
                    def respond(*args,**kwargs):
                        request=kwargs['json']
                        body=json.loads(request['messages'][0]['content'].split('\n',1)[1])
                        requests.append(body)
                        if failover and len(requests)==1:
                            raise ai.requests.exceptions.ReadTimeout('mock timeout, no live network')
                        if 'evidence_catalog' in body:
                            self.assertEqual(request['output_config']['format']['schema'],
                                             ai.response_json_schema('AI-1',body))
                            value=payload(); value.update(stop_loss=97.,take_profit=109.)
                            return Response(text=json.dumps(cite(body,value)))
                        self.assertEqual(body['ai1_raw_proposal']['action'],'buy')
                        self.assertNotIn('evidence_catalog',body['market_evidence'])
                        self.assertEqual(body['market_evidence']['opportunities'][0],{})
                        self.assertEqual(body['market_evidence']['opportunities'][1]['opportunity_id'],'B')
                        return Response(text=json.dumps({'decision':'APPROVE','defects':[],
                                                       'reasoning':'safe','fact_conflicts':[]}))
                    def finalize(parsed,reference,*args,**kwargs):
                        self.assertEqual((parsed['action'],parsed['confidence']),('buy',80))
                        self.assertEqual(parsed['quality_assessment']['dimensions']['trigger']['evidence_refs'],
                                         payload()['quality_assessment']['dimensions']['trigger']['evidence_refs'])
                        self.assertEqual(reference,101.)
                        return ai.base.StrategyResult('buy',80,False,'valid',97.,109.,0,0,{'entry_price':reference}),'valid'
                    with patch.object(ai,'AI1_URL','https://primary.test/v1/messages'), \
                         patch.object(ai,'AI2_URL','https://primary.test/v1/messages'), \
                         patch.object(ai,'backup_chat_config',return_value={'model':'backup','effort':'low',
                                      'url':'https://backup.test/v1/messages','key':'mock'}), \
                         patch.object(fixture.trader,'ensure_review_bars_current'), \
                         patch.object(fixture.trader,'validate_strategy_entry',return_value=''), \
                         patch.object(fixture.trader,'get_live_quote',return_value=second['live_quote']), \
                         patch.object(fixture.trader,'place_order',side_effect=lambda result:captured.append(result) or True), \
                         patch.object(ai.base.logger,'error') as failures:
                        post,snapshots,alerts=fixture.run_entry(iter([first,first,second]),respond,finalize)
                    self.assertEqual(post.call_count,3 if failover else 2)
                    alerts.assert_not_called()
                    self.assertEqual(len(captured),1,failures.call_args_list)
                    self.assertEqual(captured[0].details['opportunity_id'],'B')
                    self.assertEqual(captured[0].details['strategy_fingerprint'],'frozen')
                    inputs=[c.args[2] for c in snapshots.call_args_list if c.args[1]=='ai1_input']
                    self.assertEqual(inputs[-1]['evidence_catalog'],requests[-2]['evidence_catalog'])
                    if failover:
                        self.assertNotEqual(requests[0]['evidence_catalog']['scope'],requests[1]['evidence_catalog']['scope'])
                    result=next(c.args[2] for c in snapshots.call_args_list if c.args[1]=='ai1_result')
                    self.assertEqual(result['status'],'review_pending')
                finally:
                    fixture.doCleanups()
