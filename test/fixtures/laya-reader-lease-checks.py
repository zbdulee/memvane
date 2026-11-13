#!/usr/bin/env python3
"""T2 lifetime admission on disposable, explicitly converted v5 installs.

This first T2 slice proves stable locks and steady endpoint resolution. It
does not claim move-prefix/retained-tuple or cold snapshot reader coverage.
"""
from contextlib import contextmanager
import fcntl
import importlib
import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
spec=importlib.util.spec_from_file_location('reader_lease_base',HERE/'laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

CHILD=r'''
import json,signal,sys
sys.path.insert(0,sys.argv[1])
import memvane.journal.reader_lease as lease
signal.alarm(12)
try:
    if sys.argv[3]=='prepare':
        lease._prepare_lifetime(sys.argv[2])
    with lease.reader_lifetime(sys.argv[2],exclusive=sys.argv[3]=='exclusive'):
        print(json.dumps(dict(state='held')),flush=True)
        if sys.argv[3]=='hold':
            assert sys.stdin.readline().strip()=='release'
except (ValueError,OSError) as exc:
    print(json.dumps(dict(state='refused',reason=str(exc))),flush=True)
'''

CAS_CHILD=r'''
import json,pathlib,sys
sys.path.insert(0,sys.argv[1])
import laya_product_bundle as bundle; import memvane.journal.install_journal as journal; import memvane.journal.journal_reader as reader
p=json.loads(pathlib.Path(sys.argv[2]).read_text())
plugin=pathlib.Path(p['plugin'])
def inspect(root):
    assert pathlib.Path(root)==plugin
    return p['proof']
journal._inspect_identity=lambda *a,**k:p['proof']
journal._verified_executing_root=lambda *a,**k:str(plugin)
journal._provider_file_identity=lambda *a,**k:[[str(plugin/'p.py'),1,2,3,4,5]]
journal.inspect_full_plugin_candidate=bundle.inspect_full_plugin_candidate=inspect
state,project=pathlib.Path(p['state']),pathlib.Path(p['project'])
seal=reader.seal_current_reader(state,project,executing_root=plugin)
target=reader.seal_reader_endpoint(seal,db_path=pathlib.Path(p['target']))
result=reader.cas_reader(state/'install-journal.sqlite',expected_epoch=p['epoch'],
    expected_endpoint=p['expected'],target=target,transition_id='c'*32)
print(json.dumps(dict(epoch=result.epoch,endpoint=result.preferred_endpoint)))
'''


class ReaderLeaseChecks(unittest.TestCase):
    def setUp(self):
        self.helper=base.JournalReaderChecks('runTest')
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        (self.case,self.reader,self.bridge,self.installer,self.readiness,
         self.selection,_)=self.helper.converted()
        self.install=base.J.inspect_journal(self.case.journal)['selected']['installId']

    def api(self):
        if importlib.util.find_spec('memvane.journal.reader_lease') is None:
            self.fail('T2 feature RED: lifetime API absent after actual v5 conversion; not a T1 regression')
        api=importlib.import_module('memvane.journal.reader_lease')
        for name in ('reader_lifetime','resolve_endpoint'):
            self.assertTrue(callable(getattr(api,name,None)),name)
        return api

    def path(self):
        return self.reader._lock_dir(self.install,create=False)/'reader-lifetime.lock'

    @contextmanager
    def child(self, mode, *, home=None):
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
        if home is not None:env['HOME']=str(home)
        process=subprocess.Popen([sys.executable,'-B','-I','-c',CHILD,
            str(ROOT/'core'),self.install,mode],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8',env=env)
        try:
            self.assertTrue(select.select([process.stdout],[],[],5)[0],'lease child readiness timeout')
            line=process.stdout.readline()
            if not line:
                _,error=process.communicate(timeout=3)
                self.fail('lease child setup failed: '+error)
            yield process,json.loads(line)
        finally:
            if process.poll() is None:
                # Only the unreaped Popen child created by this fixture.
                process.kill()
            process.communicate(timeout=5)

    def test_conversion_prepares_private_lifetime_lock_and_shared_read_opens_readonly(self):
        api=self.api()
        path=self.path()
        info=path.lstat()
        self.assertEqual(info.st_mode & 0o777,0o600)
        self.assertEqual((info.st_uid,info.st_nlink),(os.getuid(),1))
        self.assertFalse(path.is_relative_to(self.case.state))
        self.assertFalse(path.is_relative_to(self.case.project))
        before=path.read_bytes()
        with api.reader_lifetime(self.install,exclusive=False) as fd:
            self.assertEqual(fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE,os.O_RDONLY)
            self.assertTrue(os.path.samestat(os.fstat(fd),path.lstat()))
            with api.reader_lifetime(self.install,exclusive=False):pass
        self.assertEqual(path.read_bytes(),before)

    def test_shared_reader_defers_exclusive_child_across_home_then_release_permits_it(self):
        api=self.api()
        other=self.case.home/'other-home';other.mkdir(mode=0o700)
        with api.reader_lifetime(self.install,exclusive=False):
            with self.child('exclusive',home=other) as (process,row):
                self.assertEqual(row,dict(state='refused',reason='reader_lifetime_busy'))
                self.assertEqual(process.wait(timeout=3),0)
        with self.child('exclusive',home=other) as (process,row):
            self.assertEqual(row,dict(state='held'))
            self.assertEqual(process.wait(timeout=3),0)

    def test_independent_shared_child_blocks_writer_until_its_actual_exit(self):
        api=self.api()
        with self.child('hold') as (process,row):
            self.assertEqual(row,dict(state='held'))
            self.assertIsNone(process.poll(),'shared lock control must still be alive')
            with self.assertRaisesRegex(ValueError,'reader_lifetime_busy'):
                with api.reader_lifetime(self.install,exclusive=True):pass
            process.kill()
            process.wait(timeout=3)
            with api.reader_lifetime(self.install,exclusive=True):pass

    def test_missing_lock_and_unknown_install_never_create_a_reader_lock(self):
        api=self.api()
        path=self.path();saved=path.with_name('saved-lifetime.lock');path.rename(saved)
        try:
            before=set(path.parent.rglob('*'))
            with self.assertRaises((ValueError,OSError)):
                with api.reader_lifetime(self.install,exclusive=False):pass
            self.assertEqual(set(path.parent.rglob('*')),before)
            self.assertFalse(path.exists())
            with self.assertRaises((ValueError,OSError)):
                with api.reader_lifetime('f'*32,exclusive=False):pass
            self.assertEqual(set(path.parent.rglob('*')),before)
        finally:saved.rename(path)

    def test_symlink_hardlink_and_public_mode_are_refused(self):
        api=self.api();path=self.path()
        with self.subTest(kind='mode'):
            path.chmod(0o644)
            try:
                with self.assertRaises((ValueError,OSError)):
                    with api.reader_lifetime(self.install,exclusive=False):pass
            finally:path.chmod(0o600)
        with self.subTest(kind='hardlink'):
            alias=path.with_name('hardlink.lock');os.link(path,alias)
            try:
                with self.assertRaises((ValueError,OSError)):
                    with api.reader_lifetime(self.install,exclusive=False):pass
            finally:alias.unlink()
        with self.subTest(kind='symlink'):
            saved=path.with_name('saved-lifetime.lock');path.rename(saved);path.symlink_to(saved)
            try:
                with self.assertRaises((ValueError,OSError)):
                    with api.reader_lifetime(self.install,exclusive=False):pass
            finally:path.unlink();saved.rename(path)

    def test_conversion_lock_initializer_refuses_an_actual_fifo_without_waiting_for_a_writer(self):
        self.api();path=self.path();saved=path.with_name('saved-lifetime.lock')
        path.rename(saved);os.mkfifo(path,0o600)
        try:
            with self.child('prepare') as (process,row):
                self.assertEqual(row['state'],'refused')
                self.assertEqual(process.wait(timeout=3),0)
        finally:path.unlink();saved.rename(path)

    def test_real_flock_then_path_inode_replacement_is_refused(self):
        api=self.api();path=self.path();saved=path.with_name('replaced-lifetime.lock')
        flock=fcntl.flock;replaced=False
        def race(fd,operation):
            nonlocal replaced
            result=flock(fd,operation)
            if not replaced and operation & fcntl.LOCK_SH:
                replaced=True;path.rename(saved);path.touch(mode=0o600)
            return result
        try:
            with mock.patch.object(fcntl,'flock',side_effect=race):
                with self.assertRaisesRegex(ValueError,'reader_lock_changed|reader_lifetime_changed'):
                    with api.reader_lifetime(self.install,exclusive=False):pass
            self.assertTrue(replaced)
        finally:
            if replaced:path.unlink();saved.rename(path)

    def test_steady_resolve_yields_selected_endpoint_and_holds_lifetime_until_close(self):
        api=self.api();before=self.helper.metadata_snapshot(self.case)
        with self.helper.full_authority(self.case,self.reader,api):
            with api.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                self.assertIs(type(endpoint),self.reader.VerifiedEndpoint)
                self.assertEqual(endpoint.endpoint_id,self.selection.preferred_endpoint)
                with self.child('exclusive') as (process,row):
                    self.assertEqual(row,dict(state='refused',reason='reader_lifetime_busy'))
                    self.assertEqual(process.wait(timeout=3),0)
        self.assertEqual(self.helper.metadata_snapshot(self.case),before)
        with api.reader_lifetime(self.install,exclusive=True):pass

    def test_resolved_endpoint_capability_expires_when_its_lifetime_context_closes(self):
        api=self.api()
        with self.helper.full_authority(self.case,self.reader,api):
            with api.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                self.reader._recheck(endpoint)
            with self.assertRaisesRegex(ValueError,'reader_endpoint_not_issued'):
                self.reader._recheck(endpoint)

    def test_forward_bundle_epoch_preserves_the_persisted_reader_creation_identity(self):
        api=self.api()
        old=base.J.inspect_journal(self.case.journal)
        new=base.J.cas_mark_new_write(self.case.journal,old['epoch'])
        self.assertGreater(new['epoch'],old['epoch'])
        with self.helper.full_authority(self.case,self.reader,api):
            with api.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                self.assertEqual(endpoint.endpoint_id,self.selection.preferred_endpoint)

    def test_existing_read_keeps_a_new_read_gets_b_and_read_token_cannot_mutate_selection(self):
        api=self.api()
        other=self.helper.alternate(self.case,self.reader,self.installer,'lifetime-reader-b.sqlite')
        with self.helper.full_authority(self.case,self.reader,api):
            with api.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as old_reader:
                changed=self.reader.cas_reader(self.case.journal,expected_epoch=self.selection.epoch,
                    expected_endpoint=self.selection.preferred_endpoint,target=other,transition_id='a'*32)
                self.assertEqual(old_reader.endpoint_id,self.selection.preferred_endpoint)
                self.reader._recheck(old_reader)
                with api.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as new_reader:
                    self.assertEqual(new_reader.endpoint_id,changed.preferred_endpoint)
                    self.assertEqual(new_reader.record['db_path'],other.record['db_path'])
                before=self.case.journal.read_bytes()
                with mock.patch.object(self.reader,'mutate_image',wraps=self.reader.mutate_image) as mutate:
                    with self.assertRaisesRegex(ValueError,'reader_endpoint_read_only|reader_endpoint_readonly'):
                        self.reader.cas_reader(self.case.journal,expected_epoch=changed.epoch,
                            expected_endpoint=changed.preferred_endpoint,target=old_reader,transition_id='b'*32)
                    mutate.assert_not_called()
                self.assertEqual(self.case.journal.read_bytes(),before)
                self.assertEqual(self.reader.read_selection(self.case.journal),changed)

    def test_v5_conversion_replay_never_recreates_a_named_lock_while_old_inode_is_held(self):
        api=self.api();path=self.path();saved=path.with_name('still-held-lifetime.lock')
        with api.reader_lifetime(self.install,exclusive=False):
            path.rename(saved)
            try:
                error=None;new_inode=None;writer=None
                try:
                    with self.helper.full_authority(self.case,self.reader,self.bridge):
                        self.reader.convert_reader_v5(self.case.state,self.case.project,
                            installer=self.installer,readiness=self.readiness)
                except (ValueError,OSError) as exc:error=str(exc)
                if path.exists():
                    new_inode=[path.stat().st_dev,path.stat().st_ino]
                    with self.child('exclusive') as (process,writer):
                        process.wait(timeout=3)
                print(json.dumps(dict(case='v5-replay-held-unlinked-lock',error=error,
                    recreated_inode=new_inode,exclusive_child=writer)),flush=True)
                self.assertIsNotNone(error,'v5 replay must refuse a missing lifetime lock')
                self.assertFalse(path.exists(),'v5 replay must not publish a second lock inode')
            finally:
                if path.exists():path.unlink()
                saved.rename(path)

    def test_resolver_accepts_a_complete_pin_when_real_child_cas_replaces_journal_during_resolution(self):
        api=self.api()
        other=self.helper.alternate(self.case,self.reader,self.installer,'resolution-race-b.sqlite')
        payload=self.case.home/'reader-cas-child.json'
        payload.write_text(json.dumps(dict(plugin=str(self.case.plugin),proof=self.case.proof,
            state=str(self.case.state),project=str(self.case.project),target=other.record['db_path'],
            epoch=self.selection.epoch,expected=self.selection.preferred_endpoint)),encoding='utf8')
        payload.chmod(0o600)
        actual=self.reader._selection;published=[]
        def race(db):
            result=actual(db)
            if not published:
                run=subprocess.run([sys.executable,'-B','-I','-c',CAS_CHILD,str(ROOT),str(payload)],
                    capture_output=True,encoding='utf8',timeout=15)
                self.assertEqual(run.returncode,0,run.stderr)
                published.append(json.loads(run.stdout))
            return result
        with self.helper.full_authority(self.case,self.reader,api),mock.patch.object(self.reader,'_selection',side_effect=race):
            with api.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                self.assertIn(endpoint.endpoint_id,(self.selection.preferred_endpoint,other.endpoint_id))
                self.reader._recheck(endpoint)
        self.assertEqual(published[0]['endpoint'],other.endpoint_id)
        print(json.dumps(dict(case='resolve-real-child-publication',published=published)),flush=True)

    def test_resolver_rejects_unproved_same_bytes_journal_inode_replacement(self):
        api=self.api();actual=self.reader._selection;replaced=False
        def race(db):
            nonlocal replaced
            result=actual(db)
            if not replaced:
                replaced=True
                copy=self.case.state/'unproved-journal.sqlite'
                copy.write_bytes(self.case.journal.read_bytes());copy.chmod(0o600)
                os.replace(copy,self.case.journal)
            return result
        with self.helper.full_authority(self.case,self.reader,api),mock.patch.object(self.reader,'_selection',side_effect=race):
            with self.assertRaises(ValueError):
                with api.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin):pass
        self.assertTrue(replaced)

    def test_resolver_never_promotes_a_rejected_rollback_image_to_a_new_lineage_anchor(self):
        api=self.api();ancestor=self.case.journal.read_bytes()
        other=self.helper.alternate(self.case,self.reader,self.installer,'first-lineage-b.sqlite')
        fork=self.helper.alternate(self.case,self.reader,self.installer,'fork-lineage-c.sqlite')
        with self.helper.full_authority(self.case,self.reader,api):
            first=self.reader.cas_reader(self.case.journal,expected_epoch=self.selection.epoch,
                expected_endpoint=self.selection.preferred_endpoint,target=other,transition_id='d'*32)
        payload=self.case.home/'fork-cas-child.json'
        payload.write_text(json.dumps(dict(plugin=str(self.case.plugin),proof=self.case.proof,
            state=str(self.case.state),project=str(self.case.project),target=fork.record['db_path'],
            epoch=self.selection.epoch,expected=self.selection.preferred_endpoint)),encoding='utf8')
        payload.chmod(0o600)
        actual=self.reader._selection;reads=0;branched=[]
        def race(db):
            nonlocal reads
            result=actual(db);reads+=1
            if reads==1:
                copy=self.case.state/'old-journal-image.sqlite'
                copy.write_bytes(ancestor);copy.chmod(0o600);os.replace(copy,self.case.journal)
            elif reads==2:
                run=subprocess.run([sys.executable,'-B','-I','-c',CAS_CHILD,str(ROOT),str(payload)],
                    capture_output=True,encoding='utf8',timeout=15)
                self.assertEqual(run.returncode,0,run.stderr)
                branched.append(json.loads(run.stdout))
            return result
        with self.helper.full_authority(self.case,self.reader,api),mock.patch.object(self.reader,'_selection',side_effect=race):
            with self.assertRaises(ValueError,msg='a rejected ancestor cannot replace the proved lineage anchor'):
                with api.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin):pass
        self.assertEqual(first.epoch,branched[0]['epoch'])
        self.assertNotEqual(first.preferred_endpoint,branched[0]['endpoint'])
        print(json.dumps(dict(case='rejected-rollback-not-an-anchor',original=first.preferred_endpoint,
            branched=branched,reads=reads)),flush=True)


if __name__=='__main__':unittest.main(verbosity=2)
