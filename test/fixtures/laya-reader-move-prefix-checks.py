#!/usr/bin/env python3
"""Actual project move prefixes, before resume, over published retained v5.

Only existing bundle/provider/executing-copy and provider-lane seams are
synthetic. Physical rename, fsync, fence, journal and fresh FTS are actual.
"""
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import unittest

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
spec=importlib.util.spec_from_file_location('move_prefix_journal_fixture',HERE/'laya-reader-move-journal-checks.py')
journal_fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(journal_fixture)
seed_module=journal_fixture.seed_module

NEGATIVE_READER=r'''
from pathlib import Path
from types import SimpleNamespace
import importlib.util,json,os,signal,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime']
signal.alarm(25)
spec=importlib.util.spec_from_file_location('negative_prefix_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as R,laya_reader_lease as L
assert len(R._issued)==0
def forbidden(*a,**k):raise AssertionError('negative reader must not issue an installer')
R.seal_current_reader=forbidden
helper=base.JournalReaderChecks('runTest');case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
try:
    with helper.full_authority(case,R,L):
        with L.resolve_endpoint(Path(p['project']),action='recall',host_root=case.plugin) as endpoint:
            print(json.dumps(dict(event='capability-issued',typed=type(endpoint) is R.VerifiedEndpoint)),flush=True)
except (ValueError,OSError,RuntimeError) as exc:
    print(json.dumps(dict(event='refused',reason=str(exc))),flush=True)
    sys.exit(3)
'''

WRITER=r'''
from contextlib import ExitStack,nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import importlib.util,json,os,signal,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime']
signal.alarm(30)
spec=importlib.util.spec_from_file_location('physical_prefix_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_reader_lease as L,laya_journal_reader as R,laya_project_dir_move as mover,laya_state_move as state_move
state,project=Path(p['state']),Path(p['project'])
case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
helper=base.JournalReaderChecks('runTest');phase='owner'
def checkpoint(name):
    folder=mover.current_folder(project) if name=='anchor-entry' else mover.legacy_folder(project)
    assert R._node(folder)==p['folderNode'] and R._node(folder/'wiki')==p['wikiNode']
    print(json.dumps(dict(event='physical-prefix-checkpoint',phase=name,pid=os.getpid(),
        actualOwner=True,actualLifetimeEX=True,actualPrefenceFullProof=True,
        folder=str(folder),folderNode=R._node(folder),wikiNode=R._node(folder/'wiki'),
        actualHostExecuted=False)),flush=True)
    os.kill(os.getpid(),signal.SIGKILL)
try:
    with helper.full_authority(case,R,L,mover),mock.patch.object(state_move,'_provider_lane',lambda:nullcontext()):
        intent=mover._load_intent(state,project);assert intent is not None
        with ExitStack() as locks:
            mover._locks(locks,project,state,mover._queue_path(project,intent))
            with L.move_lifetime(state,project,intent=intent):
                phase='prefence-full-proof'
                record,raw,proof=R._evidence(state,project,case.plugin)
                assert proof['productRouteAllowed'] is True
                assert R.read_selection(state/'install-journal.sqlite').preferred_endpoint==p['retained']
                phase='physical-switch'
                original_anchor=mover._anchor_wiki
                def anchor_entry(chosen):
                    assert Path(chosen)==project
                    checkpoint('anchor-entry')
                    return original_anchor(chosen)
                mover._anchor_wiki=anchor_entry
                def observed_step(name):
                    if name=='fenced' and p['phase']=='fenced-before-rename':checkpoint('fenced-before-rename')
                mover._switch(intent,step=observed_step)
                raise RuntimeError('actual physical checkpoint was not reached')
except Exception as exc:
    print(json.dumps(dict(event='error',phase=phase,errorType=type(exc).__name__,reason=str(exc))),flush=True)
    sys.exit(2)
'''


class MovePrefixChecks(unittest.TestCase):
    @staticmethod
    def require(value,reason):
        if not value:raise RuntimeError('physical-prefix prerequisite failed: '+reason)

    def check_prefix(self,phase,*,mutation=None):
        seed=seed_module.MoveSealChecks('runTest')
        self.addCleanup(seed.doCleanups)
        try:
            seed.setUp()
            with seed.prepared() as intent:
                stage,seal_type=seed.producer()
                seal=stage(intent,installer=seed.installer)
                self.require(type(seal) is seal_type,'actual typed stage seal')
                selected=seed.lease.publish_move_reader(seed.case.journal,seal=seal)
                seed.reader._recheck(seal.retained_endpoint)
        except Exception as exc:
            raise RuntimeError('published-v5/owner/stage prerequisite failed before physical reader gate') from exc
        case=seed.case;mover=seed.mover
        old=mover.legacy_folder(case.project);new=mover.current_folder(case.project)
        folder_node=seed.reader._node(old);wiki_node=seed.reader._node(old/'wiki')
        original_project={str(path.relative_to(old)):path.read_bytes() for path in old.rglob('*')
                          if path.is_file() and not path.is_symlink()}
        self.require(not os.path.lexists(new),'rename destination must not be pre-created')
        bundle=seed_module.base.J.inspect_journal(case.journal)
        payload=case.home/'physical-prefix.json'
        payload.write_text(json.dumps(dict(home=str(case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
            state=str(case.state),project=str(case.project),plugin=str(case.plugin),proof=case.proof,
            retained=selected.preferred_endpoint,generation=seal.old_generation,exclusion=seal.exclusion_epoch,
            phase=phase,folderNode=folder_node,wikiNode=wiki_node)),encoding='utf8');payload.chmod(0o600)
        child=subprocess.Popen([sys.executable,'-B','-I','-c',WRITER,str(ROOT),str(payload)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
        try:
            if not select.select([child.stdout],[],[],30)[0]:raise RuntimeError('physical writer checkpoint deadline')
            line=child.stdout.readline()
            if not line:raise RuntimeError('physical writer exited before any checkpoint')
            checkpoint=json.loads(line);print(json.dumps(checkpoint),flush=True)
            if checkpoint.get('event')!='physical-prefix-checkpoint':
                raise RuntimeError('writer factory/proof failure, not retained-reader feature RED: '+json.dumps(checkpoint))
            self.require(checkpoint['phase']==phase,'exact actual checkpoint')
            self.require(child.wait(timeout=5)==-signal.SIGKILL,'actual writer SIGKILL')
        finally:
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)
        folder=new if phase=='anchor-entry' else old
        self.require((not os.path.lexists(old) and new.is_dir()) if phase=='anchor-entry'
                     else (old.is_dir() and not os.path.lexists(new)), 'exact rename prefix paths')
        self.require(seed.reader._node(folder)==folder_node,'original project-folder inode')
        self.require(seed.reader._node(folder/'wiki')==wiki_node,'original wiki inode')
        self.require({str(path.relative_to(folder)):path.read_bytes() for path in folder.rglob('*')
                      if path.is_file() and not path.is_symlink()}==original_project,
                     'settings/old route/marker/wiki bytes before anchor')
        import laya_fresh_wiki_origin as fresh
        self.require((case.state/fresh.LEDGER).read_bytes()==mover._canonical(intent['fencedLedger']),
                     'exact durable fenced ledger')
        self.require((folder/fresh.ROUTE).read_bytes()==intent['oldRoute'].encode('utf8'),'old route remains exact')
        self.require((folder/fresh.MARKER).read_bytes()==intent['oldMarker'].encode('utf8'),'old marker remains exact')
        self.require(seed_module.base.J.inspect_journal(case.journal)==bundle,'bundle epoch/flag unchanged')
        with seed.ro(case.journal) as db:
            preferred=db.execute('SELECT preferred_endpoint FROM reader_selection').fetchone()[0]
            row=db.execute('SELECT record FROM read_endpoints WHERE endpoint_id=?',(preferred,)).fetchone()
            self.require(preferred==selected.preferred_endpoint and row is not None,'published retained selection')
            record=json.loads(row[0])
            self.require(record['pinned_generation']==seal.old_generation,'persisted old generation pin')
            for key in ('catalog_path','binding_path','sidecar_path'):
                self.require(Path(record[key]).is_file(),'retained old component exists: '+key)
        mutation_evidence=None
        saved_container=None
        if mutation is not None:
            self.require(phase=='anchor-entry','negative mutates only an actual completed rename prefix')
            if mutation=='container-inode':
                saved_container=case.project/'preserved-original-container'
                folder.rename(saved_container)
                folder.mkdir(mode=stat.S_IMODE(saved_container.stat().st_mode))
                folder.chmod(stat.S_IMODE(saved_container.stat().st_mode))
                for entry in list(saved_container.iterdir()):entry.rename(folder/entry.name)
                mover._fsync(folder);mover._fsync(case.project)
                self.require(seed.reader._node(saved_container)==folder_node,'preserved original container inode')
                self.require(seed.reader._node(folder)!=folder_node,'actual replacement container inode')
                self.require(seed.reader._node(folder/'wiki')==wiki_node,'same original wiki inode under replacement')
                self.require({str(path.relative_to(folder)):path.read_bytes() for path in folder.rglob('*')
                              if path.is_file() and not path.is_symlink()}==original_project,
                             'all original settings/route/marker/pointer/wiki moved by rename')
                mutation_evidence=dict(kind=mutation,newContainerNode=seed.reader._node(folder),
                    preservedContainerNode=seed.reader._node(saved_container),sameWikiNode=seed.reader._node(folder/'wiki'))
            elif mutation=='reviewed-route-ahead':
                with seed.ro(case.catalog) as db:
                    meta=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('binding','roots')"))
                self.require(meta=={key:intent['catalog'][key][0] for key in ('binding','roots')},
                             'catalog remains at old roots/binding before illegal metadata change')
                self.require(seed.reader._node(case.store)==intent['oldLedger']['bindingStoreIdentity'],
                             'original old binding store inode remains')
                reviewed=case.state/'reviewed-route.json'
                self.require(reviewed.read_bytes()==intent['oldRoute'].encode('utf8'),'reviewed route initially old')
                reviewed.write_bytes(intent['newRoute'].encode('utf8'))
                self.require((folder/fresh.ROUTE).read_bytes()==intent['oldRoute'].encode('utf8'),
                             'project route remains old while only reviewed route advances')
                self.require((case.state/fresh.LEDGER).read_bytes()==mover._canonical(intent['fencedLedger']),
                             'ledger remains exact fenced old metadata')
                mutation_evidence=dict(kind=mutation,reviewedRoute='new',projectRoute='old',
                    catalog='old',binding='old',ledger='exact-fenced')
            else:raise RuntimeError('unknown test-owned prefix mutation')
            print(json.dumps(dict(case='physical-prefix-negative-input',mutation=mutation_evidence,
                                  actualRenameCheckpointProved=True)),flush=True)
        # Do not call seed.live(): its .auto-context path is deliberately stale.
        def snapshot():
            return dict(project={str(path.relative_to(folder)):path.read_bytes() for path in folder.rglob('*')
                                 if path.is_file() and not path.is_symlink()},
                state={str(path.relative_to(case.state)):path.read_bytes() for path in case.state.rglob('*')
                       if path.is_file() and not path.is_symlink()},
                folderNode=seed.reader._node(folder),wikiNode=seed.reader._node(folder/'wiki'),
                savedContainerNode=None if saved_container is None else seed.reader._node(saved_container))
        before=snapshot();journal_node=case.journal.stat()
        probe=journal_fixture.FRESH_READER if mutation is None else NEGATIVE_READER
        result=subprocess.run([sys.executable,'-B','-I','-c',probe,str(ROOT),str(payload)],
            input='release\n',capture_output=True,encoding='utf8',timeout=30)
        observed=json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else None
        print(json.dumps(dict(case='physical-project-prefix-fresh-reader',phase=phase,actualWriterExit=child.returncode,
            originalFolderNode=folder_node,originalWikiNode=wiki_node,exactFencedOldMetadataBeforeMutation=True,
            resumed=False,readReturncode=result.returncode,read=observed,stderr=result.stderr[:4096],
            mutation=mutation_evidence,queryUnchanged=snapshot()==before,
            classification='required-reader-feature-gate-after-actual-prefix-proof' if mutation is None else 'protection-guard-negative')),flush=True)
        self.assertEqual(snapshot(),before,'fresh prefix reader must not heal or mutate moved/state files')
        self.assertTrue(os.path.samestat(journal_node,case.journal.stat()))
        if mutation is not None:
            events=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            self.assertEqual(result.returncode,3,result.stdout+result.stderr)
            self.assertTrue(events,'negative reader must report an actual refusal')
            self.assertTrue(all(event['event']!='capability-issued' for event in events),
                            'even a subsequently failing query must not mint a typed capability')
            self.assertEqual(events[-1]['event'],'refused')
            return
        self.assertEqual(result.returncode,0,'feature RED: exact retained move prefix must be readable before resume\n'+result.stderr)
        self.assertIsNotNone(observed)
        self.assertEqual(observed['endpoint'],selected.preferred_endpoint)
        self.assertEqual(observed['pinnedGeneration'],seal.old_generation)
        self.assertEqual(observed['exclusionEpoch'],seal.exclusion_epoch)
        self.assertEqual(observed['installerCalls'],0)
        self.assertGreater(observed['ftsRows'],0)

    def test_actual_rename_and_project_fsync_before_anchor_is_readable_without_resume(self):
        self.check_prefix('anchor-entry')

    def test_actual_fenced_before_rename_is_readable_without_resume(self):
        self.check_prefix('fenced-before-rename')

    def test_renamed_prefix_rejects_replaced_container_even_when_original_wiki_inode_survives(self):
        self.check_prefix('anchor-entry',mutation='container-inode')

    def test_renamed_prefix_rejects_only_reviewed_route_advancing_ahead_of_catalog_binding_and_project_route(self):
        self.check_prefix('anchor-entry',mutation='reviewed-route-ahead')

    def test_public_mover_refuses_published_retained_intent_before_any_destructive_reprepare(self):
        seed=seed_module.MoveSealChecks('runTest')
        self.addCleanup(seed.doCleanups)
        try:
            seed.setUp()
            with seed.prepared() as intent:
                stage,seal_type=seed.producer()
                seal=stage(intent,installer=seed.installer)
                self.require(type(seal) is seal_type,'actual typed stage seal before public move')
                selected=seed.lease.publish_move_reader(seed.case.journal,seal=seal)
                seed.reader._recheck(seal.retained_endpoint)
        except Exception as exc:
            raise RuntimeError('public mover safety prerequisite failed before actual entry') from exc
        case=seed.case;mover=seed.mover
        self.require(not mover._fenced(intent),'actual publication is still unfenced')
        self.require(mover._canonical(mover._load_intent(case.state,case.project))==mover._canonical(intent),
                     'same durable published intent')
        def snapshot():
            return {str(path):(path.read_bytes(),path.lstat().st_dev,path.lstat().st_ino)
                    for root in (case.project,case.state) for path in root.rglob('*')
                    if path.is_file() and not path.is_symlink()}
        before=snapshot()
        intent_path=case.state/mover.INTENT;pointer=mover.pointer_path(case.project)
        intent_bytes=intent_path.read_bytes();pointer_bytes=pointer.read_bytes()
        error,result=None,None
        try:
            with seed.authority():result=mover.move(case.project,case.state)
        except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
        after=snapshot()
        print(json.dumps(dict(case='public-mover-published-retained-safety',actualPublishedEndpoint=selected.preferred_endpoint,
            actualOwnerReleasedBeforePublicEntry=True,error=error,result=result,filesUnchanged=after==before,
            intentPreserved=intent_path.exists() and intent_path.read_bytes()==intent_bytes,
            pointerPreserved=pointer.exists() and pointer.read_bytes()==pointer_bytes,
            classification='actual-public-path-safety-regression')),flush=True)
        with self.subTest(boundary='explicit-refusal'):
            self.assertIsNotNone(error,'unsupported retained successor must refuse before abort/reprepare')
            self.assertIsNone(result)
        self.assertTrue(after==before,'public mover must preserve published intent/stage/journal/files before successor support')
        self.assertEqual(intent_path.read_bytes(),intent_bytes)
        self.assertEqual(pointer.read_bytes(),pointer_bytes)


if __name__=='__main__':unittest.main(verbosity=2)
