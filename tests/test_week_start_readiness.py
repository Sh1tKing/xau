"""Transport recovery, contextual facts and future-calendar checks; no live I/O."""
import copy
from datetime import datetime
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

logging.disable(logging.CRITICAL)
from strategies.ai_transport import PrimaryTimeoutBackoff
from strategies import entry_context as ec, news_calendar as news
from strategies.citations import build_catalog, scope_allowed
import xauusd_fib_volume_pa_ai3 as ai


class BackoffTests(unittest.TestCase):
    def setUp(self):
        self.now=100.
        self.health=PrimaryTimeoutBackoff(lambda:self.now)

    def fail(self,key='route'):
        lease=self.health.enter(key,2,900,True)
        self.health.read_timeout(lease,2,900)
        self.health.release(lease)

    def test_two_timeouts_bypass_and_one_scheduled_recovery_probe(self):
        self.fail();self.fail()
        self.assertTrue(self.health.enter('route',2,900,True).bypass)
        self.now+=901
        probe=self.health.enter('route',2,900,True)
        self.assertFalse(probe.bypass)
        self.assertTrue(self.health.enter('route',2,900,True).bypass)
        self.health.response_received(probe)
        self.assertFalse(self.health.enter('route',2,900,True).bypass)

    def test_aborted_probe_releases_without_consuming_http_or_holding_route(self):
        self.fail();self.fail();self.now+=901
        probe=self.health.enter('route',2,900,True)
        self.health.release(probe)
        self.assertFalse(self.health.enter('route',2,900,True).bypass)

    def test_without_backup_disabled_or_changed_configuration_never_bypasses(self):
        self.fail();self.fail()
        for threshold,cooldown,backup in [(2,900,False),(0,900,True),(2,0,True)]:
            self.assertFalse(self.health.enter('route',threshold,cooldown,backup).bypass)
        self.assertFalse(self.health.enter('other-config',2,900,True).bypass)

    def test_late_success_cannot_clear_a_newly_opened_timeout_window(self):
        old=self.health.enter('route',2,900,True)
        self.fail();self.fail()
        self.health.response_received(old)
        self.assertTrue(self.health.enter('route',2,900,True).bypass)


class Response:
    status_code=200
    text=''
    headers={}
    def json(self):
        return {"choices":[{"finish_reason":"stop","message":{"content":'{"action":"no"}'}}]}


class TransportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now=100.
        self.health=PrimaryTimeoutBackoff(lambda:self.now)
        for scope in (patch.dict(ai.os.environ,{'AI1_BACKUP_URL':'https://backup.example.test/chat',
                                              'AI1_BACKUP_KEY':'fake-backup'},clear=True),
                      patch.object(ai,'_PRIMARY_TIMEOUT_BACKOFF',self.health),
                      patch.object(ai,'AI1_PRIMARY_TIMEOUT_THRESHOLD',2),
                      patch.object(ai,'AI1_PRIMARY_COOLDOWN_SEC',900),
                      patch.object(ai,'AI_HTTP_RETRIES',5),patch.object(ai,'AI_READ_TIMEOUT_RETRIES',0),
                      patch.object(ai,'AI_HTTP_RETRY_PAUSE',0),patch.object(ai,'AI_USAGE_DIR',self.tmp.name),
                      patch.object(ai.base,'SYMBOL','XAUUSD'),
                      patch.dict(ai._CHAT_OPTIONS,{},clear=True),patch.dict(ai._CAP_BLOCKED_ROUTES,{},clear=True)):
            scope.start();self.addCleanup(scope.stop)

    def call(self,**kwargs):
        return ai.call_chat('AI-1','test-model','medium','rules','unused',120,
                            'https://primary.example.test/chat','fake-primary',**kwargs)

    def test_third_call_uses_backup_with_fresh_input_and_no_extra_paid_probe(self):
        count=[0]
        def refresh():
            count[0]+=1
            return {'prompt':str(count[0]),'instructions':'fresh rules','snapshot_id':str(count[0]),
                    'quote':{'bid':100+count[0],'ask':101+count[0]}}
        failure=lambda:ai.requests.ReadTimeout('read timed out')
        with patch.object(ai.requests,'post',side_effect=[failure(),Response(),failure(),Response(),Response()]) as post:
            for _ in range(3):self.call(refresh_request=refresh)
        self.assertEqual(post.call_count,5)
        self.assertEqual(count[0],5)
        self.assertEqual([c.args[0] for c in post.call_args_list],
                         ['https://primary.example.test/chat','https://backup.example.test/chat',
                          'https://primary.example.test/chat','https://backup.example.test/chat',
                          'https://backup.example.test/chat'])
        self.assertEqual([c.kwargs['json']['messages'][1]['content'] for c in post.call_args_list],['1','2','3','4','5'])
        records=[json.loads(line) for p in Path(self.tmp.name).glob('*.jsonl') for line in p.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(records),5)
        self.assertTrue(records[-1]['primary_temporarily_bypassed'])
        self.assertEqual(records[0]['read_timeout_stage'],'awaiting_headers')
        self.now+=901
        with patch.object(ai.requests,'post',return_value=Response()) as post:
            self.call();self.call()
        self.assertEqual([c.args[0] for c in post.call_args_list],['https://primary.example.test/chat']*2)

    def test_unusable_backup_never_causes_a_healthy_primary_to_be_skipped(self):
        with patch.object(ai.requests,'post',side_effect=[ai.requests.ReadTimeout('read timed out'),Response()]*2):
            self.call();self.call()
        with patch.dict(ai.os.environ,{'FIB_PA_AI1_BACKUP_MODEL':'grok-4.7','FIB_PA_AI1_BACKUP_THINKING':'disabled'}), \
             patch.object(ai.requests,'post',return_value=Response()) as post:
            self.call()
        self.assertEqual(post.call_args.args[0],'https://primary.example.test/chat')

    def test_ordinary_watch_response_resets_timeout_streak(self):
        with patch.object(ai.requests,'post',side_effect=[ai.requests.ReadTimeout('read timed out'),Response(),Response(),
                                                        ai.requests.ReadTimeout('read timed out'),Response(),Response()]) as post:
            for _ in range(4):self.call()
        self.assertEqual(post.call_args.args[0],'https://primary.example.test/chat')

    def test_expired_task_releases_recovery_lease_and_has_no_http(self):
        with patch.object(ai.requests,'post',side_effect=[ai.requests.ReadTimeout('read timed out'),Response()]*2):
            self.call();self.call()
        self.now+=901
        with patch.object(ai.requests,'post',return_value=Response()) as post:
            self.assertEqual(self.call(still_current=lambda:False),'')
            post.assert_not_called()
            self.call()
        self.assertEqual(post.call_args.args[0],'https://primary.example.test/chat')

    def test_ai2_and_ai3_keep_their_original_primary_first_behavior(self):
        for role,number in [('AI-2',2),('AI-3',3)]:
            with patch.dict(ai.os.environ,{f'AI{number}_BACKUP_URL':'https://backup.example.test/chat',
                                          f'AI{number}_BACKUP_KEY':'fake-backup'}), \
                 patch.object(ai.requests,'post',side_effect=[ai.requests.ReadTimeout('read timed out'),Response()]*3) as post:
                for _ in range(3):
                    ai.call_chat(role,'test-model','low','rules','fresh',120,'https://primary.example.test/chat','fake-primary')
            self.assertEqual(post.call_count,6)
            self.assertEqual(sum(c.args[0]=='https://primary.example.test/chat' for c in post.call_args_list),3)


def bars(closes):
    return [{'time':1000+i*300,'open':c-.25,'high':c+1,'low':c-1,'close':c} for i,c in enumerate(closes)]


class EntryContextTests(unittest.TestCase):
    def pack(self):
        rows=bars([120,115,112,109,106,107])
        return {'h1_trend':'bull','m15_trend':'bear','h1_bars':rows,'m15_bars':copy.deepcopy(rows),
                'm5_bars':copy.deepcopy(rows),'live_quote':{'bid':107.,'ask':107.33}}

    def test_bull_background_can_coexist_with_falling_window_and_last_bar_rebound(self):
        result=ec.build(self.pack(),100.,113.,120,150)
        self.assertEqual(result['backgrounds']['H1']['label'],'bull')
        self.assertEqual(result['backgrounds']['H1']['ema_value'],100.)
        self.assertEqual(result['backgrounds']['H1']['source_count'],120)
        facts=result['timeframes']['H1']
        self.assertEqual((facts['close_change'],facts['last_close_change']),(-13,1))
        self.assertEqual((facts['recent3_close_change'],facts['latest_vs_peak_close'],facts['latest_vs_trough_close']),(-2,-13,1))
        self.assertEqual((facts['higher_closes'],facts['lower_closes']),(1,4))

    def test_buy_sell_mirror_and_outside_envelope_location_is_not_clamped(self):
        original=self.pack(); mirrored=copy.deepcopy(original)
        for key in ec.TIMEFRAMES.values():
            mirrored[key]=[{**r,'open':240-r['open'],'close':240-r['close'],
                            'high':240-r['low'],'low':240-r['high']} for r in original[key]]
        mirrored['live_quote']={'bid':240-original['live_quote']['ask'],'ask':240-original['live_quote']['bid']}
        a,b=ec.build(original),ec.build(mirrored)
        for tf in ec.TIMEFRAMES:
            self.assertEqual(a['timeframes'][tf]['close_change'],-b['timeframes'][tf]['close_change'])
            self.assertAlmostEqual(a['timeframes'][tf]['buy_ask_location'],1-b['timeframes'][tf]['sell_bid_location'])
        original['live_quote']['ask']=140
        self.assertGreater(ec.build(original)['timeframes']['H1']['buy_ask_location'],1)

    def test_incomplete_or_bad_context_is_unknown_without_creating_a_trade_veto(self):
        for mutate in (lambda p:p.update(h1_bars=[]),lambda p:p['h1_bars'][-1].update(close=float('nan')),
                       lambda p:p['h1_bars'][-1].update(low=200)):
            pack=self.pack();mutate(pack)
            result=ec.build(pack)
            self.assertFalse(result['timeframes']['H1']['available'])
            self.assertNotIn('allowed',result)
            self.assertNotIn('blockers',result)

    def test_explicit_shared_citations_survive_both_roles_and_change_with_snapshot(self):
        pack=self.pack();pack['entry_context']=ec.build(pack,100,113,120,150)
        before=copy.deepcopy(pack)
        request=ai.compact_market_pack(pack)
        self.assertEqual(request['entry_context'],before['entry_context'])
        self.assertTrue(scope_allowed('/entry_context/timeframes/H1/close_change',0,'buy'))
        self.assertFalse(scope_allowed('/entry_context/timeframes/H1',0,'buy'))
        self.assertFalse(scope_allowed('/entry_context/timeframes/H1/other_candidate',0,'buy'))
        old=build_catalog(request)
        changed=copy.deepcopy(request);changed['live_quote']['ask']+=1
        changed['entry_context']=ec.build(changed,100,113,120,150)
        new=build_catalog(changed)
        refs=lambda c:{key for group in c['groups'] for key in group['refs']}
        self.assertTrue(refs(old).isdisjoint(refs(new)))
        review=ai.compact_review_pack({'market_evidence':pack,'proposal':{},'ai1_raw_proposal':{}})
        self.assertEqual(review['market_evidence']['entry_context'],before['entry_context'])
        self.assertEqual(pack,before)


class CalendarReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now=news.instant('2026-10-17T06:45:00Z')
        self.end=news.instant('2026-10-24T07:00:00+08:00')
        self.frames=[news.frame(p,[],self.now-86400,self.now+30*86400,self.now) for p in news.FAMILIES]

    def test_bootstrap_is_included_and_its_verification_age_limits_coverage(self):
        old=copy.deepcopy(self.frames)
        for f in old:f['verified_at']=news.instant('2026-10-14T13:36:06Z')
        short=copy.deepcopy(self.frames[0]);short['coverage_until']=news.instant('2026-10-18T12:00:00+08:00')
        cache=[short]+self.frames[1:]
        monday=news.coverage_report(cache,self.now,news.instant('2026-10-19T07:00:00+08:00'),old)
        self.assertTrue(monday['covers_through'])
        self.assertEqual(monday['providers']['bls']['segments'][0]['kind'],'bootstrap')
        self.assertFalse(news.coverage_report(cache,self.now,self.end,old)['covers_through'])
        self.assertTrue(news.coverage_report(cache,self.now,self.end,self.frames)['covers_through'])

    def test_long_calendar_never_extends_the_seven_day_verified_lifetime(self):
        result=news.coverage_report(self.frames,self.now,self.now+7*86400+1)
        self.assertFalse(result['covers_through'])
        for p in result['providers'].values():
            self.assertEqual(news.instant(p['usable_until_utc']),self.now+7*86400)

    def test_future_unobserved_verification_and_gapped_coverage_are_not_accepted(self):
        invalid=copy.deepcopy(self.frames);invalid[0]['verified_at']=self.now+6
        self.assertFalse(news.coverage_report(invalid,self.now,self.end)['available_now'])
        first=copy.deepcopy(self.frames[0]);first['coverage_until']=self.now+100
        later=copy.deepcopy(first);later.update(coverage_from=self.now+101,coverage_until=self.now+1000)
        self.assertFalse(news.coverage_report([first,later]+self.frames[1:],self.now,self.now+200)['covers_through'])

    def test_adjacent_verified_frames_cover_a_rollover_without_missing_event_windows(self):
        first=copy.deepcopy(self.frames[0]);first['coverage_until']=self.now+100
        later=copy.deepcopy(first);later.update(coverage_from=self.now+100,coverage_until=self.now+1000)
        data=[first,later]+self.frames[1:]
        self.assertTrue(news.coverage_report(data,self.now,self.now+200)['covers_through'])
        later['events']=[news.event('cpi',self.now+150,news.BLS_URL)]
        self.assertFalse(news.evaluate(data,self.now+150).allowed)

    def test_refresh_anticipates_expiry_without_blocking_status_or_rewriting_seed(self):
        with tempfile.TemporaryDirectory() as d:
            short=copy.deepcopy(self.frames)
            for f in short:f['coverage_until']=self.now+1800
            manager=news.NewsCalendar(Path(d)/'cache.json',fetcher=lambda p,t:next(f for f in short if f['provider']==p))
            with patch.object(news.time,'monotonic',return_value=100):
                self.assertTrue(manager.refresh(self.now))
            self.assertEqual(manager._next_refresh,1600)
            manager.request_refresh=lambda:None
            self.assertTrue(manager.status(self.now).allowed)
            self.assertFalse(manager.coverage(self.now,self.now+2000)['covers_through'])

    def test_cli_future_check_is_read_only_and_reports_coverage_separately(self):
        with tempfile.TemporaryDirectory() as d:
            manager=news.NewsCalendar(Path(d)/'missing.json')
            manager._frames=self.frames
            output=io.StringIO()
            with patch.object(news.NewsCalendar,'from_environment',return_value=manager), \
                 patch('sys.argv',['calendar','--through','2026-10-24T07:00:00+08:00']), \
                 patch.object(news.time,'time',return_value=self.now),patch.object(news,'_get') as get, \
                 patch('sys.stdout',output):
                self.assertEqual(news.main(),0)
            get.assert_not_called()
            self.assertTrue(json.loads(output.getvalue())['coverage']['covers_through'])
            self.assertFalse(manager.cache_path.exists())

    def test_invalid_cli_horizon_does_not_refresh_or_construct_runtime_cache(self):
        with patch('sys.argv',['calendar','--refresh','--through','2026-10-24T07:00:00']), \
             patch.object(news.NewsCalendar,'from_environment') as construct,patch('sys.stderr',io.StringIO()):
            with self.assertRaises(SystemExit) as caught:news.main()
        self.assertEqual(caught.exception.code,2)
        construct.assert_not_called()


if __name__=='__main__':
    unittest.main()
