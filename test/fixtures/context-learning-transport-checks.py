"""Actual subprocesses are local mock CLIs; no account/provider calls."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, 'core')
from context_learning.contracts import digest, request
from context_learning.store import capture, database, canonical
from context_learning.offline import save_provisional_batch, review_view, approve_review, delete_case, build_manifest
from context_learning.jobs import JobLease, label_job, job_status, cancel_job
from context_learning.transport import run_teacher, label_request, _SERIAL

SECRET = 'synthetic-secret-never-log'

class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qmd-transport-fixture-')
        self.root = Path(self.tmp.name)
        self.state = self.root / 'private'
        self.state.mkdir(mode=0o700)
        self.original_path = os.environ.get('PATH', '')
        self.env = patch.dict(os.environ, {'PATH':str(self.root)+os.pathsep+self.original_path,
            'ORCA_AGENT_HOOK_TOKEN':SECRET, 'OPENAI_API_KEY':SECRET})
        self.env.start()
        self.sample = request({'prompt':'Does approval precede access?'},
            [{'candidate_id':'synthetic','revision_sha256':digest('Approval precedes access.'),
              'excerpt':'Approval precedes access.','eligible':True}],host='codex',request_id='fixture')
        self.cli('codex')
        self.cli('claude')

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def cli(self, host, mode='success'):
        script = '''#!/usr/bin/env python3
import json,os,sys,time,subprocess
from pathlib import Path
mode=MODE
assert os.environ.get('QMD_SANDBOX')=='1'
assert 'ORCA_AGENT_HOOK_TOKEN' not in os.environ and 'OPENAI_API_KEY' not in os.environ
assert not any((p/'.git').exists() for p in (Path.cwd(),*Path.cwd().parents))
if mode=='hang-input':time.sleep(10)
if mode=='unsupported':
 sys.stderr.write('unknown option: '+SECRET);sys.exit(2)
sample=json.loads(sys.stdin.read())['sample']
if mode=='timeout':time.sleep(10)
if mode=='descendant':
 child=subprocess.Popen([sys.executable,'-c',"import time;from pathlib import Path;time.sleep(.7);Path("+repr(CHILD_MARKER)+").write_text('survived')"])
 Path(PID_FILE).write_text(str(child.pid));time.sleep(10)
if mode=='flood':sys.stderr.write(SECRET*20000);sys.stderr.flush();time.sleep(10)
if mode=='bad-json':print('not json '+SECRET,flush=True);time.sleep(10)
if mode=='duplicate-key':print('{"type":"thread.started","type":"error"}',flush=True);time.sleep(10)
def emit(e):print(json.dumps(e),flush=True)
labels=[]
for c in sample['candidates']:
 text=c['excerpt']['text']
 labels.append(dict(schema_version=2,request_id=sample['request_id'],candidate_id=c['candidate_id'],revision_sha256=c['revision_sha256'],abstain=False,relevance='necessary',evidence=text,rationale=dict(kind='support',text='Exact supplied evidence.',span={'start':0,'end':len(text)},scope='provided-excerpt')))
if mode=='bad-label':labels[-1]['evidence']=SECRET
if mode=='abstain':
 for l in labels:l.update(abstain=True,relevance=None,evidence='',rationale=dict(kind='uncertain',text='Uncertain synthetic excerpt.',span=None,scope='provided-excerpt'))
raw=json.dumps({'labels':labels})
if sample['host']=='claude':
 init=dict(type='system',subtype='init',model='claude-sonnet-synthetic',effort='low',tools=[],mcp_servers=[],skills=[],plugins=[{'name':'cc-plugin-sec-default','source':'cc-plugin-sec-default@builtin'}])
 if mode=='plugin':init['plugins'].append({'name':'unexpected','source':'unexpected@builtin'})
 if mode=='catalog':init['tools']=['Read']
 if mode=='wrong-model':init['model']='claude-opus-synthetic'
 if mode!='no-init':emit(init)
 if mode=='tool':emit({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Bash'}]}})
 if mode=='stream-tool':emit({'type':'stream_event','event':{'type':'content_block_start','content_block':{'type':'tool_use','name':'Bash'}}})
 if mode=='hook':emit({'type':'system','subtype':'hook_started','private':SECRET})
 emit(dict(type='result',subtype='success',num_turns=1,is_error=False,result=raw,usage={'input_tokens':100,'output_tokens':20,'private':SECRET}))
else:
 if mode!='no-init':emit({'type':'thread.started','thread_id':SECRET})
 emit({'type':'turn.started'})
 # Cross-process cancellation must win even when CI schedules other tests.
 if mode=='slow':time.sleep(10)
 if mode=='two-turns':emit({'type':'turn.started'})
 if mode=='tool':emit({'type':'item.started','item':{'type':'command_execution','command':SECRET}})
 if mode=='error':emit({'type':'error','message':SECRET})
 emit({'type':'item.completed','item':{'type':'agent_message','text':raw}})
 if mode!='incomplete':emit({'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':0,'output_tokens':20,'private':SECRET}})
if mode=='after-completion':emit({'type':'item.started','item':{'type':'command_execution','command':SECRET}})
if mode=='closed-pipes':
 os.close(1);os.close(2);time.sleep(10)
if mode=='nonzero':sys.exit(2)
'''.replace('MODE',repr(mode)).replace('PID_FILE',repr(str(self.root/'child.pid'))).replace('CHILD_MARKER',repr(str(self.root/'owned-alive'))).replace('SECRET',repr(SECRET))
        path=self.root/host
        path.write_text(script)
        path.chmod(0o700)

    def teacher(self, host='codex', **kwargs):
        sample=dict(self.sample,host=host)
        return run_teacher(sample,enabled=True,host=host,optional_hooks_audited=True,
                           accept_security_policy=True,timeout=kwargs.pop('timeout',2),**kwargs)

    def test_default_off_and_host_gates(self):
        self.assertEqual(run_teacher(None)['status'],'disabled')
        self.assertEqual(label_request('/missing','x')['status'],'disabled')
        for opts in ({'host':'claude'}, {'host':'agy'}, {'host':'codex'}, {'host':'codex','optional_hooks_audited':True,'timeout':46}):
            r=run_teacher(self.sample,enabled=True,**opts)
            self.assertEqual(r['cli_invocations'],0)
            self.assertEqual(r['status'],'failed')
        with patch('context_learning.transport.shutil.which',return_value=None):
            self.assertEqual(self.teacher()['reason'],'missing_cli')
        self.assertEqual(self.teacher('claude',disabled_plugin_ids=['*'])['cli_invocations'],0)
        self.assertEqual(self.teacher('claude',disabled_plugin_ids=['cc-plugin-sec-default@builtin'])['reason'],'security_policy_override_forbidden')

    def test_success_both_hosts_and_review_separation(self):
        for host in ('claude','codex'):
            sample=dict(self.sample,host=host,request_id=host)
            self.assertEqual(capture(sample,enabled=True,state_dir=self.state),'stored')
            result=label_request(self.state,host,enabled=True,host=host,accept_security_policy=True,optional_hooks_audited=True,timeout=2)
            self.assertEqual(result['status'],'completed',result)
            self.assertEqual(result['stored_labels'],1)
            self.assertNotIn('labels',result)
            self.assertNotIn(SECRET,json.dumps(result))
            view=review_view(self.state,host,'synthetic')
            self.assertEqual(view['status'],'provisional')
            self.assertIsNone(view['attestation'])
            self.assertEqual(approve_review(self.state,view['label'],input_sha256=view['input_sha256'],reviewer_id='fixture-reviewer',reference='fixture-review'),'reviewed')
            r=label_request(self.state,host,enabled=True,host=host,accept_security_policy=True,optional_hooks_audited=True,timeout=2)
            self.assertEqual(r['reason'],'provisional_storage_rejected')
            self.assertEqual(review_view(self.state,host,'synthetic')['status'],'reviewer-approved')

    def test_invalid_streams_labels_and_privacy(self):
        cases=[('codex',m) for m in ('bad-json','duplicate-key','tool','error','bad-label','no-init','two-turns','incomplete','nonzero','unsupported','after-completion')]+[('claude',m) for m in ('plugin','catalog','wrong-model','tool','stream-tool','hook','bad-label','no-init')]
        for host,mode in cases:
            with self.subTest(host=host,mode=mode):
                self.cli(host,mode)
                r=self.teacher(host)
                self.assertEqual(r['status'],'failed',r)
                self.assertEqual(r['cli_invocations'],1)
                self.assertNotIn('labels',r)
                self.assertNotIn(SECRET,json.dumps(r))
                self.assertNotIn(str(self.root),json.dumps(r))
        self.cli('claude')
        r=run_teacher(dict(self.sample,host='claude'),enabled=True,host='claude',timeout=2)
        self.assertEqual(r['reason'],'startup_rejected')

    def test_bounds_cancellation_busy_and_owned_descendant(self):
        self.cli('codex','flood')
        self.assertEqual(self.teacher()['reason'],'output_budget')
        for mode in ('timeout','hang-input','descendant'):
            self.cli('codex',mode)
            start=time.monotonic();r=self.teacher(timeout=.15)
            self.assertEqual(r['reason'],'timeout')
            self.assertLess(time.monotonic()-start,2)
        unrelated=subprocess.Popen([sys.executable,'-c',"import time;from pathlib import Path;time.sleep(.7);Path("+repr(str(self.root/'unrelated-alive'))+").write_text('survived')"],start_new_session=True)
        unrelated.wait(timeout=2)
        self.assertTrue((self.root/'unrelated-alive').exists())
        self.assertFalse((self.root/'owned-alive').exists())
        self.assertEqual(self.teacher(cancel=lambda:True)['cli_invocations'],0)
        start=time.monotonic()
        self.assertEqual(self.teacher(timeout=1,cancel=lambda:time.monotonic()-start>.1)['reason'],'cancelled')
        self.cli('codex','closed-pipes')
        start=time.monotonic()
        self.assertEqual(self.teacher(timeout=1,cancel=lambda:time.monotonic()-start>.15)['reason'],'cancelled')
        self.assertLess(time.monotonic()-start,.8)
        _SERIAL.acquire()
        try:self.assertEqual(self.teacher()['reason'],'transport_busy')
        finally:_SERIAL.release()
        self.assertEqual(self.teacher(cancel=lambda:(_ for _ in ()).throw(RuntimeError(SECRET)))['reason'],'transport_internal_error')

    def test_atomic_conflict_deletion_and_abstain(self):
        c=copy.deepcopy(self.sample['candidates'][0]);c['candidate_id']='other'
        sample=copy.deepcopy(self.sample);sample['candidates'].append(c)
        self.assertEqual(capture(sample,enabled=True,state_dir=self.state),'stored')
        labels=run_teacher(sample,enabled=True,host='codex',optional_hooks_audited=True,timeout=2)['labels']
        conflict=copy.deepcopy(labels[1]);conflict['relevance']='supporting'
        with database(self.state) as db:db.execute('INSERT INTO labels VALUES (?,?,?,NULL)',(sample['request_id'],'other',canonical(conflict)))
        with self.assertRaises(ValueError):save_provisional_batch(self.state,sample,labels)
        with database(self.state) as db:self.assertIsNone(db.execute('SELECT body FROM labels WHERE candidate_id=?',('synthetic',)).fetchone())
        delete_case(self.state,sample['request_id'])
        with self.assertRaises(ValueError):save_provisional_batch(self.state,sample,labels)
        self.assertEqual(label_request(self.state,sample['request_id'],enabled=True,host='codex')['cli_invocations'],0)
        new=dict(self.sample,request_id='abstain')
        self.assertEqual(capture(new,enabled=True,state_dir=self.state),'stored')
        self.cli('codex','abstain')
        r=label_request(self.state,'abstain',enabled=True,host='codex',optional_hooks_audited=True,timeout=2)
        self.assertEqual(r['status'],'completed')
        self.assertTrue(review_view(self.state,'abstain','synthetic')['label']['abstain'])
        self.assertEqual(build_manifest(self.state,'no-promotion',{}, {})['cases'],[])

    def command(self, *args):
        p=subprocess.run([sys.executable,'core/context_learning_cli.py','--state-dir',str(self.state),*args],
            capture_output=True,text=True,timeout=5)
        self.assertNotIn(SECRET,p.stdout+p.stderr)
        return json.loads(p.stdout)

    def test_cli_walkthrough(self):
        self.assertEqual(self.command('label-request','fixture','--host','codex')['status'],'disabled')
        self.assertEqual(capture(self.sample,enabled=True,state_dir=self.state),'stored')
        self.assertEqual(self.command('list-requests')['requests'][0]['request_id'],'fixture')
        self.assertEqual(self.command('inspect-request','fixture')['sample'],self.sample)
        options=('label-request','fixture','--host','codex','--enable-teacher','--optional-hooks-audited','--timeout','2')
        self.assertEqual(self.command(*options)['stored_labels'],1)
        self.assertEqual(self.command(*options)['cli_invocations'],0)
        self.assertEqual(label_job(self.state,'fixture',enabled=True,host='codex',cancel=lambda:True)['reason'],'cancelled')
        self.assertEqual(label_job(self.state,'fixture',enabled=True,host='claude')['reason'],'host_mismatch')
        view=self.command('review-label','fixture','synthetic')
        self.assertEqual(view['status'],'provisional')
        self.assertEqual(self.command('inspect-labels','fixture')['labels'][0]['status'],'provisional')
        review=('review-label','fixture','synthetic','--approve','--reviewer-id','synthetic-reviewer','--reference','offline-fixture',
            '--input-sha256',view['input_sha256'],'--label-sha256',view['label_sha256'])
        self.assertEqual(self.command(*review[:-1],'bad-hash')['reason'],'review_snapshot_mismatch')
        self.assertEqual(self.command(*review)['status'],'reviewed')
        self.assertEqual(self.command('review-label','fixture','synthetic')['status'],'reviewer-approved')
        self.assertEqual(self.command(*options)['reason'],'reviewed_label_exists')
        self.assertEqual(self.command('job-status','fixture')['status'],'failed')
        self.assertEqual(self.command('cancel-job','fixture')['status'],'not_running')

    def test_job_lease_ownership_expiry_and_paths(self):
        capture(self.sample,enabled=True,state_dir=self.state)
        with JobLease(self.state,'fixture',lease_seconds=.05) as lease:
            time.sleep(.07)
            with self.assertRaisesRegex(ValueError,'job_busy'):
                with JobLease(self.state,'fixture'):pass
            self.assertTrue(lease.cancelled())
            with database(self.state) as db:
                db.execute("UPDATE teacher_jobs SET owner='replacement',status='running' WHERE request_id='fixture'")
        self.assertEqual(job_status(self.state,'fixture')['status'],'running')
        with JobLease(self.state,'fixture'):pass
        with database(self.state) as db:
            db.execute("UPDATE teacher_jobs SET status='running',expires=? WHERE request_id='fixture'",(time.time()+.05,))
        with self.assertRaisesRegex(ValueError,'job_busy'):
            with JobLease(self.state,'fixture'):pass
        time.sleep(.07)
        with JobLease(self.state,'fixture'):pass
        original_exit=JobLease.__exit__
        def broken_finalize(lease,*args):
            original_exit(lease,*args)
            raise ValueError('synthetic-finalization-error')
        with patch.object(JobLease,'__exit__',broken_finalize):
            result=label_job(self.state,'fixture',enabled=True,host='codex',optional_hooks_audited=True,timeout=2)
        self.assertEqual(result['cli_invocations'],1)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['job_metadata_status'],'unavailable')
        path=self.state/('teacher-job-'+digest('fixture')+'.lock')
        path.unlink()
        target=self.root/'external-lock';target.write_text('');target.chmod(0o600)
        path.symlink_to(target)
        self.assertEqual(label_job(self.state,'fixture',enabled=True,host='codex')['cli_invocations'],0)
        path.unlink();os.link(target,path)
        self.assertEqual(label_job(self.state,'fixture',enabled=True,host='codex')['reason'],'unsafe_job_lock')

    def test_cross_process_cancel_and_deletion(self):
        capture(self.sample,enabled=True,state_dir=self.state)
        self.cli('codex','slow')
        argv=[sys.executable,'core/context_learning_cli.py','--state-dir',str(self.state),'label-request','fixture',
            '--host','codex','--enable-teacher','--optional-hooks-audited','--timeout','8']
        for deleted in (False,True):
            p=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
            try:
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    if job_status(self.state,'fixture')['status']=='running':break
                    time.sleep(.02)
                else:self.fail('mock job did not start')
                self.assertEqual(self.command(*argv[4:])['reason'],'job_busy')
                if deleted:delete_case(self.state,'fixture')
                else:self.assertEqual(self.command('cancel-job','fixture')['status'],'cancel_requested')
                out,err=p.communicate(timeout=6)
                self.assertNotIn(SECRET,out+err)
                self.assertEqual(json.loads(out)['reason'],'cancelled')
                with database(self.state) as db:self.assertEqual(db.execute('SELECT count(*) FROM labels').fetchone()[0],0)
            finally:
                if p.poll() is None:
                    os.killpg(p.pid,15);p.communicate(timeout=2)

if __name__=='__main__':unittest.main()
