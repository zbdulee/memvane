"""Actual shell/core QA boundaries; transport spy is distinct from UDS tests."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('qa_hook_base', HERE/'laya-journal-reader-checks.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
import laya_bridge_readiness as bridge
import laya_bridge_observer as observer
import laya_host_response as response


class QaHookChecks(unittest.TestCase):
    def test_v4_preflight_accepts_new_host_bundle_against_old_selected_route(self):
        new=self.case.home/'new-host';new.mkdir(mode=0o700)
        target={**self.case.proof,'pluginRoot':str(new),'sourceCommit':'a'*40,
                'bundleManifestSha256':'f'*64,'ownershipManifestSha256':'e'*64}
        challenge=bridge.begin_bridge_preflight(self.case.state,release_commit='a'*40,
                                                expected_hosts=frozenset(('claude','codex')))
        payload={'cwd':str(self.case.project),'prompt':bridge.challenge_prompt(challenge)}
        decision=self.case.decision()
        self.assertNotEqual(decision['bundleManifestSha256'],target['bundleManifestSha256'])
        with mock.patch.dict(os.environ,{'MEMVANE_BRIDGE_PREFLIGHT':challenge.challenge_id}), \
             mock.patch.object(observer,'inspect_full_plugin_candidate',return_value=target):
            envelope=observer._qa_envelope(payload,'claude',decision,new)
            self.assertEqual(envelope['hook_stage'],'registered-v4')
            self.assertEqual(envelope['manifest_sha256'],target['bundleManifestSha256'])
            with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                observer._qa_envelope(payload,'claude',{**decision,'scopeId':'0'*32},new)

    def setUp(self):
        self.helper = base.JournalReaderChecks('runTest')
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.case = self.helper.compose()

    def challenge(self):
        challenge = bridge.begin_bridge_preflight(self.case.state, release_commit='c'*40,
                                                  expected_hosts=frozenset(('claude','codex')))
        return challenge

    def shell(self, host, ident, sandbox=False):
        legacy = self.case.home/'legacy-cwd'
        legacy.mkdir(exist_ok=True)
        env = dict(os.environ)
        for name in ('QMD_SANDBOX','CLAUDE_SANDBOX','CLAUDE_HEADLESS','CODEX_SANDBOX','GEMINI_SANDBOX'):
            env.pop(name, None)
        env.update(MEMVANE_BRIDGE_PREFLIGHT=ident, QMD_SETUP_GUARD_FIXTURE='1')
        if sandbox:
            env['QMD_SANDBOX']='1'
        done = subprocess.run(['bash',str(ROOT/'hooks/run-hook'),'recall',host],
            input=json.dumps(dict(cwd=str(legacy),prompt='not a challenge',session_id='fixture-session')),
            env=env, capture_output=True, encoding='utf8', timeout=15)
        print(json.dumps(dict(boundary='actual-shell',host=host,qa_id=ident,sandbox=sandbox,
                              returncode=done.returncode,stdout=done.stdout,stderr=done.stderr)),flush=True)
        return done

    def test_actual_shell_invalid_qa_blocks_before_legacy_classification_for_both_hosts(self):
        for host in ('claude','codex'):
            for ident in ('not-an-id','0'*32,''):
                with self.subTest(host=host,ident=ident):
                    done=self.shell(host,ident)
                    self.assertEqual(done.returncode,0,done.stderr)
                    self.assertTrue(done.stdout.strip(),'dedicated QA must emit direct block even for legacy cwd')
                    self.assertEqual(json.loads(done.stdout)['decision'],'block')

    def test_actual_shell_sandbox_still_has_no_output_even_for_dedicated_qa(self):
        for host in ('claude','codex'):
            with self.subTest(host=host):
                done=self.shell(host,'0'*32,sandbox=True)
                self.assertEqual((done.returncode,done.stdout,done.stderr),(0,'',''))

    def core(self, action, host, payload, challenge):
        path=self.case.home/'qa-hook-payload.json'
        path.write_text(json.dumps(payload),encoding='utf8')
        path.chmod(0o600)
        fakefile=self.case.plugin/'core/laya_host_response.py'
        fakefile.parent.mkdir(mode=0o700,exist_ok=True)
        fakefile.write_text('# synthetic executing-root identity seam\n',encoding='utf8')
        fakefile.chmod(0o600)
        calls={}
        output,error=io.StringIO(),io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ,dict(
                MEMVANE_BRIDGE_PREFLIGHT=challenge.challenge_id,
                MEMVANE_BRIDGE_HOOK_ENTRY='hooks/run-hook:recall:'+host)))
            stack.enter_context(mock.patch.object(response,'__file__',str(fakefile)))
            stack.enter_context(self.helper.full_authority(self.case,response,observer,bridge))
            emitter=stack.enter_context(mock.patch.object(observer,'emit_registered_hook'))
            for name in ('serve_selected','serve_preview','learning_capture','_capture_learning','_recall_sync_wake',
                         '_reselect','select_hook_payload','select_hook_route'):
                calls[name]=stack.enter_context(mock.patch.object(response,name,
                    side_effect=AssertionError('QA entered normal downstream: '+name)))
            with redirect_stdout(output),redirect_stderr(error):
                with mock.patch.object(response.sys,'stdin',io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode('utf8')))):
                    status=response.main(['serve-qa-stdin',action,host,str(self.case.project)])
        counts={name:spy.call_count for name,spy in calls.items()}
        print(json.dumps(dict(boundary='actual-core-main',action=action,host=host,status=status,
                              stdout=output.getvalue(),stderr=error.getvalue(),downstream=counts,
                              emit_calls=emitter.call_count,transport='spy; no OS handshake claim')),flush=True)
        return status,output.getvalue(),error.getvalue(),counts,emitter

    def test_actual_core_recall_emits_verified_envelope_then_direct_block_without_context_wrapper(self):
        challenge=self.challenge()
        for host in ('claude','codex'):
            with self.subTest(host=host):
                payload=dict(prompt=bridge.challenge_prompt(challenge),cwd=str(self.case.project),session_id='fixture-session')
                status,out,err,counts,emitter=self.core('recall',host,payload,challenge)
                self.assertEqual(status,0,err)
                self.assertEqual(emitter.call_count,1)
                args=emitter.call_args.args
                self.assertEqual((args[0],args[1],args[3]),(challenge.challenge_id,host,'fixture-session'))
                self.assertEqual(args[2]['observed_nonce_sha256'],challenge.nonce_sha256)
                result=json.loads(out)
                self.assertEqual(result['decision'],'block')
                self.assertNotIn('hookSpecificOutput',result)
                self.assertTrue(all(value==0 for value in counts.values()),counts)

    def test_promptless_session_start_validates_without_inventing_a_nonce_prompt(self):
        challenge=self.challenge()
        payload=dict(cwd=str(self.case.project),session_id='fixture-session')
        self.assertNotIn('prompt',payload)
        with mock.patch.dict(os.environ,{'MEMVANE_BRIDGE_PREFLIGHT':challenge.challenge_id}), \
                self.helper.full_authority(self.case,observer,bridge):
            allowed=observer.qa_session_start(payload,'claude',self.case.decision(),self.case.plugin)
        print(json.dumps(dict(boundary='promptless-session-helper',allowed=allowed,payload_keys=sorted(payload))),flush=True)
        self.assertTrue(allowed)

    def test_actual_core_promptless_session_start_skips_serve_learning_and_wake(self):
        challenge=self.challenge()
        payload=dict(cwd=str(self.case.project),session_id='fixture-session')
        status,out,err,counts,emitter=self.core('update-queued','claude',payload,challenge)
        self.assertEqual(status,0,err)
        self.assertEqual(json.loads(out),{})
        self.assertEqual(emitter.call_count,0)
        self.assertTrue(all(value==0 for value in counts.values()),counts)
        bad=dict(cwd=str(self.case.home),session_id='fixture-session')
        status,out,err,counts,emitter=self.core('update-queued','claude',bad,challenge)
        self.assertNotEqual(status,0,'invalid dedicated SessionStart cannot enter normal flow')
        self.assertIn('reader_hosts_pending',err)
        self.assertEqual(emitter.call_count,0)
        self.assertTrue(all(value==0 for value in counts.values()),counts)


if __name__=='__main__':
    unittest.main(verbosity=2)
