#!/usr/bin/env python3
"""T2B actual move admission, on the existing disposable published v5 corpus.

The first case covers the held-reader physical-move boundary. Retained tuple,
crash-prefix search and rename/adopt/state completion remain separate gates.
"""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('reader_move_lease_fixture',HERE/'laya-reader-lease-checks.py')
lease_fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(lease_fixture)
base=lease_fixture.base
spec=importlib.util.spec_from_file_location('reader_move_recovery_fixture',HERE/'laya-journal-reader-recovery-checks.py')
recovery=importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class MovedCheckpoint(BaseException):
    """Controlled unwinding after the real rename; not a process crash claim."""


OLD_STATE_CHILD=r'''
import json,pathlib,sys
from contextlib import nullcontext
core=pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0,str(core))
p=json.loads(pathlib.Path(sys.argv[2]).read_text())
import laya_state_move as mover,laya_install_journal as J,laya_product_bundle as bundle
from laya_index_host_route import select_route
assert pathlib.Path(mover.__file__).resolve().parent==core
assert pathlib.Path(J.__file__).resolve().parent==core
plugin=pathlib.Path(p['plugin'])
def inspect(root):
    assert pathlib.Path(root)==plugin
    return p['proof']
J._inspect_identity=lambda *a,**k:p['proof']
J._verified_executing_root=lambda *a,**k:str(plugin)
J._provider_file_identity=lambda *a,**k:[[str(plugin/'p.py'),1,2,3,4,5]]
J.inspect_full_plugin_candidate=bundle.inspect_full_plugin_candidate=inspect
mover._provider_lane=lambda:nullcontext()
project,state,home=map(pathlib.Path,(p['project'],p['state'],p['home']))
assert J.journal_version(state/'install-journal.sqlite')==4
proof=J.verify_selected_route_for_write(select_route(str(project),action='update'))
assert proof['productRouteAllowed'] is True
class Stop(BaseException):pass
reached=[]
def checkpoint(name):
    if name=='moved':reached.append(name);raise Stop()
try:mover.move(project,state,home=home,step=checkpoint)
except Stop:pass
target=mover.target_root(state,home)
assert reached==['moved'] and not state.exists() and target.is_dir(), 'real rename checkpoint not reached'
intent_path=target/mover.INTENT
intent=json.loads(intent_path.read_bytes())
intent['fromRoot'],intent['toRoot']=intent['toRoot'],intent['fromRoot']
intent_path.write_bytes(mover._canonical(intent))
pointer=mover.pointer_path(project);journal=target/'install-journal.sqlite'
before=(intent_path.read_bytes(),pointer.read_bytes(),journal.read_bytes())
error,result=None,None
try:
    chosen,result=mover.move(project,target,home=home)
except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
print(json.dumps(dict(case='released-v4-reversed-state-intent',commit=p['commit'],
    module=str(mover.__file__),journalVersion=4,baselineFullProof=proof['productRouteAllowed'],
    actualRenameReached=True,controlledUnwindNotProcessCrash=True,error=error,result=result,
    intentPreserved=intent_path.exists() and intent_path.read_bytes()==before[0],
    pointerPreserved=pointer.exists() and pointer.read_bytes()==before[1],
    journalPreserved=journal.read_bytes()==before[2],actualHostExecuted=False)),flush=True)
'''


class ReaderMoveChecks(unittest.TestCase):
    setUp=lease_fixture.ReaderLeaseChecks.setUp
    api=lease_fixture.ReaderLeaseChecks.api

    def legacy_published(self,*,version):
        helper=base.JournalReaderChecks('runTest')
        helper.setUp();self.addCleanup(helper.doCleanups)
        class PublishedLegacyState(base.published.MoveCase):
            STATE_PARTS=('.local','state','laya','3'*32)
            PROVIDER_EXTRA={'serviceSocketPath':'{state}/runtime/laya-embed.sock'}
            # Short realpath fixture home; no runtime socket/model is needed.
            TEMP_DIR=str(Path('/tmp').resolve())
            TEMP_PREFIX=''
        case=helper.compose(PublishedLegacyState)
        self.assertEqual(len(case.head()[1]),3,'actual published three-card seed is required')
        import laya_initial_build as build
        record=build.read(case.state)
        self.assertEqual(record['identity']['stateRoot'],str(case.state))
        pointer=build.register_project(record['identity'])
        self.assertTrue(Path(pointer).is_file(),'real initial-build pointer is required')
        if version==5:
            reader,bridge=helper.reader_api(),helper.bridge_api()
            installer=helper.installer(case,reader,bridge)
            _challenge,_receipts,readiness=helper.ready(case,bridge)
            with helper.full_authority(case,reader,bridge):
                reader.convert_reader_v5(case.state,case.project,installer=installer,readiness=readiness)
        self.assertEqual(base.J.journal_version(case.journal),version)
        return helper,case

    def test_actual_project_rename_defers_before_any_physical_change_while_reader_is_held(self):
        api=self.api()
        mover=importlib.import_module('laya_project_dir_move')
        self.assertEqual(mover.needed(self.case.project),'rename')
        metadata=self.helper.metadata_snapshot(self.case)
        data=self.helper.data_snapshot(self.case)
        paths=(self.case.db,self.case.catalog,self.case.store,self.case.journal)
        nodes={str(path):(Path(path).stat().st_dev,Path(path).stat().st_ino) for path in paths}
        result,error=None,None
        with self.helper.full_authority(self.case,self.reader,api,mover):
            with api.reader_lifetime(self.install,exclusive=False):
                try:result=mover.move(self.case.project,self.case.state)
                except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
        reason=error or (result.get('reason') if isinstance(result,dict) else None)
        print(json.dumps(dict(case='actual-project-rename-held-reader',result=result,error=error,
            actualHostExecuted=False)),flush=True)
        self.assertEqual(reason,'reader_lifetime_busy')
        self.assertEqual(self.helper.metadata_snapshot(self.case),metadata)
        self.assertEqual(self.helper.data_snapshot(self.case),data)
        self.assertEqual({str(path):(Path(path).stat().st_dev,Path(path).stat().st_ino) for path in paths},nodes)
        self.assertTrue(self.case.auto.is_dir())
        self.assertFalse((self.case.project/'.memvane').exists())

    def test_current_state_noop_does_not_add_a_journal_requirement(self):
        mover=importlib.import_module('laya_state_move')
        self.assertFalse(mover.legacy_root(self.case.state,self.case.home))
        saved=self.case.state/'saved-journal.sqlite'
        self.case.journal.rename(saved)
        try:
            with self.helper.full_authority(self.case,self.reader,mover):
                state,result=mover.move(self.case.project,self.case.state,home=self.case.home)
            self.assertEqual(state,self.case.state)
            self.assertEqual(result,dict(status='state_move_current'))
            self.assertFalse(self.case.journal.exists())
        finally:saved.rename(self.case.journal)

    def test_v5_reversed_state_intent_refuses_before_abort_and_preserves_evidence(self):
        helper,case=self.legacy_published(version=5)
        mover=importlib.import_module('laya_state_move')
        reached=[]
        def checkpoint(name):
            if name=='moved':
                reached.append(name)
                raise MovedCheckpoint()
        with helper.full_authority(case,self.reader,mover):
            with self.assertRaises(MovedCheckpoint):
                mover.move(case.project,case.state,home=case.home,step=checkpoint)
        target=mover.target_root(case.state,case.home)
        self.assertEqual(reached,['moved'],'fixture must reach actual rename before tampering')
        self.assertFalse(case.state.exists())
        self.assertTrue(target.is_dir())
        intent_path=target/mover.INTENT
        intent=json.loads(intent_path.read_bytes())
        intent['fromRoot'],intent['toRoot']=intent['toRoot'],intent['fromRoot']
        intent_path.write_bytes(mover._canonical(intent))
        pointer=mover.pointer_path(case.project);journal=target/'install-journal.sqlite'
        before=(intent_path.read_bytes(),pointer.read_bytes(),journal.read_bytes())
        error,result=None,None
        try:
            with helper.full_authority(case,self.reader,mover):
                _state,result=mover.move(case.project,target,home=case.home)
        except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
        observed=dict(case='current-v5-reversed-state-intent',error=error,result=result,
            actualRenameReached=True,controlledUnwindNotProcessCrash=True,
            intentPreserved=intent_path.exists() and intent_path.read_bytes()==before[0],
            pointerPreserved=pointer.exists() and pointer.read_bytes()==before[1],
            journalPreserved=journal.read_bytes()==before[2],actualHostExecuted=False)
        print(json.dumps(observed),flush=True)
        self.assertIsNotNone(error,observed)
        self.assertTrue(observed['intentPreserved'],observed)
        self.assertTrue(observed['pointerPreserved'],observed)
        self.assertTrue(observed['journalPreserved'],observed)

    def test_actual_released_v4_reversed_intent_baseline_is_reported_separately(self):
        _helper,case=self.legacy_published(version=4)
        old_root=recovery.RecoveryChecks('runTest').old_tree(case)
        payload=case.home/'released-state-intent-payload.json'
        payload.write_text(json.dumps(dict(project=str(case.project),state=str(case.state),
            plugin=str(case.plugin),proof=case.proof,home=str(case.home),
            commit=recovery.READER_BRIDGE_FIXTURE_COMMIT)),encoding='utf8')
        payload.chmod(0o600)
        result=subprocess.run([sys.executable,'-B','-I','-c',OLD_STATE_CHILD,
            str(old_root/'core'),str(payload)],capture_output=True,encoding='utf8',timeout=35,
            cwd=str(case.home),env={**os.environ,'HOME':str(case.home),'PYTHONDONTWRITEBYTECODE':'1'})
        self.assertEqual(result.returncode,0,'historical setup/checkpoint failed: '+result.stderr)
        observed=json.loads(result.stdout.strip().splitlines()[-1])
        print(json.dumps(observed),flush=True)
        self.assertTrue(observed['baselineFullProof'],observed)
        self.assertTrue(observed['actualRenameReached'],observed)
        self.assertIsNone(observed['error'],observed)
        self.assertEqual(observed['result'],dict(status='state_move_current'),observed)
        self.assertFalse(observed['intentPreserved'],observed)
        self.assertFalse(observed['pointerPreserved'],observed)
        self.assertTrue(observed['journalPreserved'],observed)

    def test_v5_ordered_state_intent_still_resumes_after_the_actual_rename(self):
        helper,case=self.legacy_published(version=5)
        mover=importlib.import_module('laya_state_move')
        def checkpoint(name):
            if name=='moved':raise MovedCheckpoint()
        with helper.full_authority(case,self.reader,mover):
            with self.assertRaises(MovedCheckpoint):
                mover.move(case.project,case.state,home=case.home,step=checkpoint)
            target=mover.target_root(case.state,case.home)
            chosen,result=mover.move(case.project,target,home=case.home)
        self.assertEqual(chosen,target)
        self.assertEqual(result['status'],'state_move_complete',result)
        print(json.dumps(dict(case='current-v5-ordered-state-intent',status=result['status'],
            actualRenameReached=True,controlledUnwindNotProcessCrash=True,actualHostExecuted=False)),flush=True)


if __name__=='__main__':unittest.main(verbosity=2)
