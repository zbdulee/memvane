"""Private fixtures and local fake CLI only: never an account/model request."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, 'core')
from context_learning import general_teacher as teacher
from context_learning import general_gold_ledger as ledger
from context_learning.egress_guard import inspect_payload
from context_learning.store import canonical
from context_learning.contracts import digest, request
from context_learning.shared import _db_at, _project_key


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='qmd-general-gold-')
        self.base=Path(self.tmp.name).resolve()
        self.root=self.base/'private';self.root.mkdir(mode=0o700)
        self.project=self.base/'project';self.project.mkdir()
        self.sample=request({'prompt':'What color is the garden?'},[
            {'candidate_id':'docs/garden.md','revision_sha256':digest('Cobalt garden.'),
             'excerpt':'Cobalt garden.','eligible':True}],host='codex',request_id='fixture')
        self.bodies={'docs/garden.md':'Cobalt garden.'}
        self.policy={'goldModel':'gpt-6-luna','maxGoldCalls':20,'maxRssMib':4096,
                     'safetySeconds':60}
        self.proposal={'schema':'qmd-project-teacher-consent-v1',
            'projectRoot':str(self.project),'recipient':'openai','transport':'codex-cli',
            'purpose':'selection-gold','fields':['query','candidate_body','candidate_id','revision_sha256'],
            'model':'gpt-6-luna','maxGoldCalls':20,'maxInputBytes':65536,
            'maxOutputBytes':65536,'timeoutSeconds':60,'maxRssMib':4096,'enabled':True}
        self.consent()

    def tearDown(self):self.tmp.cleanup()

    def consent(self):
        p=self.root/'project-teacher-consent.json'
        p.write_text(canonical({'proposal':self.proposal,'acceptedSha256':digest(canonical(self.proposal))}))
        p.chmod(0o600)

    def db(self):return _db_at(self.root/'shared-learning.sqlite3')

    def case(self):
        with self.db() as db:
            db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
               (_project_key(self.project),str(self.project),self.sample['request_id'],digest(canonical(self.sample)),canonical(self.sample),canonical(self.bodies),'corpus',1,'train','[]','active','queued'))

    def run_queue(self,invoke,quota=None):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(teacher,'_db',side_effect=lambda root:self.db()))
            stack.enter_context(patch.object(teacher,'_policy',return_value=self.policy))
            stack.enter_context(patch.object(teacher,'_case_active',return_value=True))
            stack.enter_context(patch.object(teacher,'resource_report',return_value={'ready':True}))
            stack.enter_context(patch.object(teacher,'_require_scope',return_value={
                'projectRoot':str(self.project),'cohort':{'id':'general-v1','notBeforeUnixMs':100}}))
            stack.enter_context(patch.object(teacher,'read_weekly_quota',
                **({'return_value':{'remainingPercent':80}} if quota is None else {'side_effect':quota})))
            return teacher.process_pending(self.root,self.project,self.policy,invoke=invoke)

    def test_secret_values_rejected_in_every_transmitted_field_without_echo(self):
        for secret in ['sk-proj-'+'a'*30,'Bearer abcdefghijklmnopqrstuvwxyz',
                       '-----BEGIN PRIVATE KEY-----','ghp_'+'a'*36,
                       'AKIA'+'A'*16,'password="mySensitivePassword"',
                       'GITHUB_TOKEN="mySensitiveToken"','token="mySensitiveToken"',
                       'OPENAI_API_KEY="notShapeSpecificButPrivate"',
                       'https://privatevalue@service.example',
                       'postgres://admin:privatepass@db.example/test',
                       'Cookie: session=privatevalue',
                       'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwcm9qZWN0In0.signature']:
            for field in ['query','candidate_body','candidate_id']:
                payload={'question':'ordinary','candidates':[{'id':'docs/a','body':'ordinary','revision_sha256':'a'*64}]}
                if field=='query':payload['question']=secret
                elif field=='candidate_body':payload['candidates'][0]['body']=secret
                else:payload['candidates'][0]['id']=secret
                result=inspect_payload(payload)
                self.assertFalse(result['allowed'],(secret,field))
                self.assertNotIn(secret,canonical(result))
        self.assertTrue(inspect_payload({'question':'How are tokens generated?','candidates':[]})['allowed'])

    def test_consent_tamper_and_project_mismatch_rejected(self):
        teacher.require_consent(self.root,self.project,self.policy,self.sample,self.bodies)
        self.proposal['projectRoot']=str(self.base)
        self.consent()
        with self.assertRaisesRegex(ValueError,'teacher_consent_scope'):
            teacher.require_consent(self.root,self.project,self.policy,self.sample,self.bodies)

    def test_ledger_reservation_and_crash_never_retry(self):
        with self.db() as db:
            ledger.reserve(db,'key','fixture','first',digest('input'),20)
            ledger.transition(db,'key','fixture','first',('reserved',),'dispatch_unknown')
        with self.db() as db:
            with self.assertRaisesRegex(ValueError,'gold_attempt_already_reserved'):
                ledger.reserve(db,'key','fixture','first',digest('input'),20)
            ledger.recover(db,'key')
            self.assertEqual(ledger.stage(db,'key','fixture','first'),'dispatched_uncertain')

    def test_agreement_two_calls_commits_gold_and_reentry_zero_calls(self):
        self.case();calls=[]
        def invoke(*args,**kwargs):
            calls.append(1);return {'selected_ids':['docs/garden.md'],'usage':{},'elapsed_seconds':0}
        result=self.run_queue(invoke)
        self.assertEqual(result['outcomes'],['gold'])
        self.assertEqual(len(calls),2)
        self.run_queue(invoke);self.assertEqual(len(calls),2)
        with self.db() as db:self.assertEqual(db.execute('SELECT status FROM cases').fetchone()[0],'gold')

    def test_secret_case_excluded_before_any_call(self):
        self.sample=request({'prompt':'api_key = "'+('x'*30)+'"'},[
            {'candidate_id':'docs/garden.md','revision_sha256':digest('Cobalt garden.'),
             'excerpt':'Cobalt garden.','eligible':True}],host='codex',request_id='fixture')
        self.case()
        result=self.run_queue(lambda *a,**kw:self.fail('secret must never be transmitted'))
        self.assertEqual(result['outcomes'],['excluded_sensitive'])
        with self.db() as db:
            self.assertNotIn('x'*30,db.execute('SELECT error FROM cases').fetchone()[0])

    def test_oversized_case_does_not_starve_the_queue_or_query_quota(self):
        self.bodies['docs/garden.md']='public garden '*6000;self.case()
        calls=[]
        self.run_queue(lambda *a,**kw:calls.append(1))
        self.assertEqual(calls,[])
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status,error FROM cases').fetchone(),('pending_review','teacher_input_budget'))

    def test_unknown_external_failure_no_automatic_retry(self):
        self.case();calls=[]
        def invoke(*a,**kw):calls.append(1);raise RuntimeError('private arbitrary provider text')
        self.run_queue(invoke);self.run_queue(invoke)
        self.assertEqual(len(calls),1)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status,error FROM cases').fetchone(),('pending_review','gold_attempt_failed'))

    def test_same_input_new_capture_after_completed_gold_is_not_sent_again(self):
        self.case();calls=[]
        def invoke(*a,**kw):calls.append(1);return {'selected_ids':['docs/garden.md'],'usage':{}}
        self.run_queue(invoke)
        self.sample['request_id']='duplicate-capture';self.case()
        result=self.run_queue(invoke)
        self.assertEqual(calls,[1,1])
        self.assertEqual(result['outcomes'],['duplicate_input'])
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status,error FROM cases WHERE request_id=?',('duplicate-capture',)).fetchone(),('pending_review','duplicate_input'))

    def test_same_input_new_capture_after_uncertain_send_is_not_sent_again(self):
        self.case();calls=[]
        def invoke(*a,**kw):
            calls.append(1);kw['before_launch']();kw['after_launch'](12345)
            raise RuntimeError('unknown external outcome')
        self.run_queue(invoke)
        self.sample['request_id']='duplicate-capture';self.case()
        result=self.run_queue(invoke)
        self.assertEqual(calls,[1])
        self.assertEqual(result['outcomes'],['duplicate_input'])

    def test_changed_question_or_body_is_a_new_teacher_task(self):
        self.case();calls=[]
        def invoke(*a,**kw):calls.append(1);return {'selected_ids':['docs/garden.md'],'usage':{}}
        self.run_queue(invoke)
        self.sample=request({'prompt':'What is planted in the garden?'},[
            {'candidate_id':'docs/garden.md','revision_sha256':digest('Cobalt garden.'),
             'excerpt':'Cobalt garden.','eligible':True}],host='codex',request_id='new-question')
        self.case();self.run_queue(invoke)
        self.sample=request({'prompt':'What is planted in the garden?'},[
            {'candidate_id':'docs/garden.md','revision_sha256':digest('Cobalt roses.'),
             'excerpt':'Cobalt roses.','eligible':True}],host='codex',request_id='new-body')
        self.bodies['docs/garden.md']='Cobalt roses.'
        self.case();self.run_queue(invoke)
        self.assertEqual(len(calls),6)

    def test_committed_first_answer_resumes_only_second_phase_for_same_capture(self):
        self.case();calls=[]
        def invoke(*a,**kw):calls.append(1);return {'selected_ids':['docs/garden.md'],'usage':{}}
        self.run_queue(invoke,quota=[{'remainingPercent':80},ValueError('weekly_quota_checkpoint')])
        with self.db() as db:self.assertEqual(db.execute('SELECT status FROM cases').fetchone()[0],'first_done')
        self.run_queue(invoke)
        self.assertEqual(len(calls),2)
        with self.db() as db:self.assertEqual(db.execute('SELECT status FROM cases').fetchone()[0],'gold')

    def test_disagreement_remains_pending_review(self):
        self.case();answers=iter([['docs/garden.md'],[]])
        self.run_queue(lambda *a,**kw:dict(selected_ids=next(answers),usage={},elapsed_seconds=0))
        with self.db() as db:self.assertEqual(db.execute('SELECT status FROM cases').fetchone()[0],'pending_review')

    def test_unproven_first_done_is_not_sent_or_promoted(self):
        self.case()
        with self.db() as db:db.execute("UPDATE cases SET status='first_done',first=?",(canonical({'selected_ids':['docs/garden.md'],'usage':{}}),))
        calls=[]
        def invoke(*a,**kw):calls.append(1);return {'selected_ids':['docs/garden.md'],'usage':{}}
        result=self.run_queue(invoke)
        self.assertEqual(calls,[])
        self.assertEqual(result['outcomes'],['pending_review'])

    def test_cases_before_approved_cohort_are_not_transmitted(self):
        self.case()
        with self.db() as db:db.execute('UPDATE cases SET captured_at=0')
        calls=[]
        self.run_queue(lambda *a,**kw:calls.append(1))
        self.assertEqual(calls,[])

    def test_case_changed_during_provider_cannot_commit_a_gold_receipt(self):
        self.case()
        def invoke(*a,**kw):
            with self.db() as db:db.execute("UPDATE cases SET status='failed'")
            return {'selected_ids':['docs/garden.md'],'usage':{}}
        result=self.run_queue(invoke)
        self.assertEqual(result['outcomes'],['pending_review'])
        with self.db() as db:
            self.assertNotEqual(db.execute('SELECT stage FROM project_gold_attempts').fetchone()[0],'committed')

    def test_real_local_cli_and_readonly_quota_ipc(self):
        """Runs real subprocess streaming through a local executable fixture."""
        cli=self.base/'codex'
        cli.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
assert os.environ.get('QMD_SANDBOX')=='1'
assert 'OPENAI_API_KEY' not in os.environ
def emit(value):print(json.dumps(value),flush=True)
if 'app-server' in sys.argv:
 first=json.loads(sys.stdin.readline());assert first['method']=='initialize'
 emit({'id':1,'result':{}})
 assert json.loads(sys.stdin.readline())['method']=='initialized'
 account=json.loads(sys.stdin.readline());assert account['method']=='account/read'
 assert account['params']=={'refreshToken':False}
 emit({'id':2,'result':{'account':{'type':'chatgpt'}}})
 request=json.loads(sys.stdin.readline());assert request['method']=='account/rateLimits/read'
 assert request['params']=={'excludeResetCreditDetails':True,'supportsLunaReserve':False}
 emit({'id':3,'result':{'rateLimitsByLimitId':{'codex':{'secondary':{'windowDurationMins':10080,'usedPercent':20}}}}})
else:
 assert '--ignore-user-config' in sys.argv and '--ignore-rules' in sys.argv
 assert '--ephemeral' in sys.argv and '--sandbox' in sys.argv
 assert 'features.shell_tool=false' in sys.argv and 'features.plugins=false' in sys.argv
 assert not any((p/'.git').exists() for p in (Path.cwd(),*Path.cwd().parents))
 request=json.loads(sys.stdin.read());assert request['question']=='What color is the garden?'
 emit({'type':'thread.started','thread_id':'fixture'})
 emit({'type':'turn.started'})
 emit({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'selected_ids':['docs/garden.md']})}})
 emit({'type':'turn.completed','usage':{'input_tokens':5,'output_tokens':2}})
''');cli.chmod(0o700)
        with patch.dict(os.environ,{'PATH':str(self.base)+os.pathsep+os.environ.get('PATH',''),'OPENAI_API_KEY':'private-fixture'}):
            self.assertEqual(teacher.read_weekly_quota()['remainingPercent'],80)
            stages=[];guard_calls=[]
            with patch.object(teacher,'_process_usage',return_value=(10,0)),patch.object(teacher,'resource_report',return_value={'ready':True}):
                result=teacher._invoke_codex(self.sample,self.bodies,self.proposal,self.root,self.policy,
                  guard=lambda **kw:guard_calls.append(kw.get('fresh_inventory',False)),before_launch=lambda:stages.append('before'),after_launch=lambda pid:stages.append('after'),launch_failed=lambda:self.fail('spawn'))
            self.assertEqual(result['selected_ids'],['docs/garden.md'])
            self.assertEqual(stages,['before','after'])
            self.assertEqual(guard_calls.count(True),1)
            cli.write_text(cli.read_text().replace("'type':'chatgpt'","'type':'apiKey'"))
            with self.assertRaisesRegex(ValueError,'weekly_quota_unavailable'):teacher.read_weekly_quota()

    def test_lock_excludes_second_worker(self):
        with teacher._worker_lock(self.root) as first:
            self.assertTrue(first)
            with teacher._worker_lock(self.root) as second:self.assertFalse(second)

    def test_weekly_threshold_and_unknown_fail_closed(self):
        for percent in [70,80,100]:
            with self.assertRaisesRegex(ValueError,'weekly_quota_checkpoint'):
                teacher.weekly_quota({'ordinaryUsageAllowed':True,'rateLimitsByLimitId':{'codex':{'secondary':{'windowDurationMins':10080,'usedPercent':percent}}}})
        for value in [{},{'rateLimits':{'primary':{'windowDurationMins':300,'usedPercent':2}}}]:
            with self.assertRaisesRegex(ValueError,'weekly_quota_unavailable'):teacher.weekly_quota(value)
        self.assertEqual(teacher.weekly_quota({'ordinaryUsageAllowed':True,'rateLimitsByLimitId':{'codex':{'secondary':{'windowDurationMins':10080,'usedPercent':20}}}})['remainingPercent'],80)
        for allowed in [False]:
            with self.assertRaises(ValueError):
                teacher.weekly_quota({'ordinaryUsageAllowed':allowed,'rateLimitsByLimitId':{'codex':{'secondary':{'windowDurationMins':10080,'usedPercent':20}}}})

    def test_quota_pause_preserves_queued_case_without_reserving(self):
        self.case()
        with patch.object(teacher,'read_weekly_quota',side_effect=ValueError('weekly_quota_checkpoint')):
            # run_queue normally replaces quota; test directly with minimal trusted local boundaries.
            with patch.object(teacher,'_db',side_effect=lambda root:self.db()),patch.object(teacher,'_policy',return_value=self.policy),patch.object(teacher,'_require_scope',return_value={'projectRoot':str(self.project),'cohort':{'id':'general-v1','notBeforeUnixMs':100}}),patch.object(teacher,'_case_active',return_value=True),patch.object(teacher,'resource_report',return_value={'ready':True}):
                result=teacher.process_pending(self.root,self.project,self.policy,invoke=lambda *a,**kw:self.fail('quota'))
        self.assertEqual(result['status'],'deferred_quota')
        with self.db() as db:self.assertEqual(db.execute('SELECT status FROM cases').fetchone()[0],'queued')


if __name__=='__main__':
    result=unittest.TextTestRunner(stream=sys.stderr).run(unittest.defaultTestLoader.loadTestsFromTestCase(Checks))
    raise SystemExit(not result.wasSuccessful())
