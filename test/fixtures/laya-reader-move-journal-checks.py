#!/usr/bin/env python3
"""Retained move publication contracts; real v5/SQLite/owner/lease boundaries.

Bundle identity and provider OS authority reuse the existing synthetic seam.
No physical move, journal-COMMIT crash or full-hook claim is made here.
"""
from contextlib import closing,contextmanager
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import unittest

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
spec=importlib.util.spec_from_file_location('move_journal_seed',HERE/'laya-reader-move-seal-checks.py')
seed_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(seed_module)

FRESH_READER=r'''
from pathlib import Path
from types import SimpleNamespace
import importlib.util,json,os,signal,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2])
sys.path.insert(0,str(root/'core'));p=json.loads(payload.read_text())
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime']
signal.alarm(25)
spec=importlib.util.spec_from_file_location('fresh_retained_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as R,laya_reader_lease as L,laya_index_a as index
initial=len(R._issued);assert initial==0
calls=[]
def forbidden_installer(*a,**k):
    calls.append('installer');raise AssertionError('fresh recall must not invoke installer issuance')
R.seal_current_reader=forbidden_installer
helper=base.JournalReaderChecks('runTest');case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
with helper.full_authority(case,R,L):
    with L.resolve_endpoint(Path(p['project']),action='recall',host_root=case.plugin) as endpoint:
        assert type(endpoint) is R.VerifiedEndpoint
        record=endpoint.record
        assert record['pinned_generation']==p['generation']
        assert record['exclusion_epoch']==p['exclusion']
        R._recheck(endpoint)
        with R._readonly(Path(record['db_path']),Path(record['db_path']).parent) as db:
            assert index.LayaIndexA._generation_fts_matches(SimpleNamespace(db=db),record['pinned_generation'])
            ordinal=db.execute('SELECT ord FROM gen_index WHERE gen=?',(record['pinned_generation'],)).fetchone()[0]
            table=index._gen_fts(ordinal)
            rows=db.execute('SELECT m.path,c.body FROM "'+table+'" f JOIN membership m ON m.path=f.path_key '
                'AND m.gen=? JOIN content c ON c.hash=m.hash WHERE "'+table+'" MATCH ?',
                (record['pinned_generation'],'otter')).fetchall()
            assert rows and any('otter' in body for _path,body in rows)
        print(json.dumps(dict(event='resolved',initialIssued=initial,installerCalls=len(calls),
            endpoint=endpoint.endpoint_id,pinnedGeneration=record['pinned_generation'],
            exclusionEpoch=record['exclusion_epoch'],ftsRows=len(rows),actualHostExecuted=False)),flush=True)
        assert sys.stdin.readline().strip()=='release'
assert not calls
'''

EXCLUSIVE=r'''
import json,sys
sys.path.insert(0,sys.argv[1])
import laya_reader_lease as lease
try:
    with lease.reader_lifetime(sys.argv[2],exclusive=True):print(json.dumps(dict(state='held')))
except (ValueError,OSError) as exc:print(json.dumps(dict(state='refused',reason=str(exc))))
'''

CRASH_PUBLISHER=r'''
from contextlib import ExitStack,nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import importlib.util,json,os,signal,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime']
signal.alarm(35)
spec=importlib.util.spec_from_file_location('crash_publisher_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as R,laya_reader_lease as L,laya_project_dir_move as mover,laya_state_move as state_move
helper=base.JournalReaderChecks('runTest');case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
state,project=Path(p['state']),Path(p['project']);initial=len(R._issued);phase='issuer'
try:
    assert initial==0
    with helper.full_authority(case,R,L,mover),mock.patch.object(state_move,'_provider_lane',lambda:nullcontext()):
        installer=R.seal_current_reader(state,project,executing_root=case.plugin)
        phase='owner'
        with ExitStack() as locks:
            intent=mover._load_intent(state,project);assert intent is not None
            mover._locks(locks,project,state,mover._queue_path(project,intent))
            with L.reader_lifetime(p['install'],exclusive=False):
                phase='stage-replay'
                seal=L.stage_move_reader(intent,installer=installer)
                assert seal.retained_endpoint.endpoint_id==p['retained']
                def checkpoint(name):
                    if name==p['step']:
                        print(json.dumps(dict(event='publisher-checkpoint',step=name,pid=os.getpid(),
                            initialIssued=initial,actualFreshIssuer=True,endpoint=seal.retained_endpoint.endpoint_id)),flush=True)
                        os.kill(os.getpid(),signal.SIGKILL)
                base.J._conversion_step=checkpoint
                phase='publish'
                L.publish_move_reader(state/'install-journal.sqlite',seal=seal)
                raise RuntimeError('actual COW publisher checkpoint was not reached')
except Exception as exc:
    print(json.dumps(dict(event='error',phase=phase,reason=str(exc),errorType=type(exc).__name__)),flush=True)
    sys.exit(2)
'''

CRASH_READ_PROBE=r'''
from pathlib import Path
from types import SimpleNamespace
import importlib.util,json,os,signal,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime']
signal.alarm(25)
spec=importlib.util.spec_from_file_location('crash_read_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as R,laya_reader_lease as L,laya_index_a as index
assert len(R._issued)==0
calls=[]
def forbidden(*a,**k):calls.append('installer');raise AssertionError('read probe may not issue an installer')
R.seal_current_reader=forbidden
helper=base.JournalReaderChecks('runTest');case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
path=Path(p['state'])/'install-journal.sqlite'
with helper.full_authority(case,R,L):
    with R._readonly(path,path.parent) as db:
        bundle=base.J._row(db);selected,record=R._selection(db)
        table=bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='move_read_seals'").fetchone())
        count=db.execute('SELECT count(*) FROM move_read_seals').fetchone()[0] if table else 0
        endpoint=db.execute('SELECT record,attestation FROM read_endpoints WHERE endpoint_id=?',
                            (selected.preferred_endpoint,)).fetchone();assert endpoint is not None
        transition=db.execute('SELECT epoch,preferred_endpoint,previous_endpoint FROM reader_transitions WHERE transition_id=?',
                              (selected.transition_id,)).fetchone()
        assert transition==(selected.epoch,selected.preferred_endpoint,selected.previous_endpoint)
        retained=record.get('capability')=='v6-retained-ro-v1'
        if retained:
            assert table and count==1 and selected.preferred_endpoint==p['retained']
            assert selected.previous_endpoint==p['oldEndpoint'] and selected.epoch==p['oldEpoch']+1
            assert record['move_id']==p['move'] and record['pinned_generation']==p['generation']
            assert record['exclusion_epoch']==p['exclusion']
        else:
            assert not table and count==0 and selected.preferred_endpoint==p['oldEndpoint'] and selected.epoch==p['oldEpoch']
    with L.resolve_endpoint(Path(p['project']),action='recall',host_root=case.plugin) as token:
        assert type(token) is R.VerifiedEndpoint and token.endpoint_id==selected.preferred_endpoint
        R._recheck(token)
        with R._readonly(Path(token.record['db_path']),Path(token.record['db_path']).parent) as db:
            generation=token.record.get('pinned_generation')
            if generation is None:generation=db.execute('SELECT gen FROM head WHERE scope=?',(token.record['scope'],)).fetchone()[0]
            assert generation==p['generation']
            assert index.LayaIndexA._generation_fts_matches(SimpleNamespace(db=db),generation)
            table_name=index._gen_fts(db.execute('SELECT ord FROM gen_index WHERE gen=?',(generation,)).fetchone()[0])
            rows=db.execute('SELECT m.path,c.body FROM "'+table_name+'" f JOIN membership m ON m.path=f.path_key AND m.gen=? '
                'JOIN content c ON c.hash=m.hash WHERE "'+table_name+'" MATCH ?',(generation,'otter')).fetchall()
            assert rows and any('otter' in body for _path,body in rows)
    assert not calls
print(json.dumps(dict(event='read-only-crash-image',retained=retained,readerEpoch=selected.epoch,
    endpoint=selected.preferred_endpoint,sealRows=count,ftsRows=len(rows),installerCalls=len(calls),
    bundleEpoch=bundle['epoch'],newWriteSeen=bundle['new_write_seen'],actualHostExecuted=False)),flush=True)
'''


class MoveJournalChecks(unittest.TestCase):
    def setUp(self):
        self.seed=seed_module.MoveSealChecks('runTest')
        self.seed.setUp();self.addCleanup(self.seed.doCleanups)
        self.case,self.reader,self.lease=self.seed.case,self.seed.reader,self.seed.lease

    @contextmanager
    def staged(self):
        with self.seed.prepared() as intent:
            stage,_type=self.seed.producer()
            seal=stage(intent,installer=self.seed.installer)
            self.assertIs(type(seal),_type)
            self.reader._recheck(seal.retained_endpoint)
            print(json.dumps(dict(case='move-journal-stage-prerequisite',journalVersion=5,
                oldGeneration=seal.old_generation,actualPublishedCards=3,actualOwnerScope=True)),flush=True)
            yield seal

    def publisher(self):
        publish=getattr(self.lease,'publish_move_reader',None)
        if not callable(publish):
            print(json.dumps(dict(case='move-reader-publication-feature-absence',
                actualStageProved=True,classification='feature-RED-not-independent-product-defect')),flush=True)
            self.fail('T2 feature RED: publish_move_reader absent after actual v5/owner/staging proof')
        return publish

    def stores(self):
        snapshot=self.seed.live()
        snapshot['files'].pop(str(self.case.journal))
        return snapshot

    @staticmethod
    def scalars(value):
        if isinstance(value,bytes):
            try:decoded=value.decode('utf8')
            except UnicodeError:return [value.hex()]
            return [value.hex(),*MoveJournalChecks.scalars(decoded)]
        if isinstance(value,(tuple,list)):
            return [item for child in value for item in MoveJournalChecks.scalars(child)]
        if isinstance(value,dict):
            return [item for child in value.values() for item in MoveJournalChecks.scalars(child)]
        if isinstance(value,str):
            try:decoded=json.loads(value)
            except (ValueError,TypeError):return [value]
            if isinstance(decoded,(dict,list)):return MoveJournalChecks.scalars(decoded)
        return [value]

    def test_publish_is_atomic_reader_only_and_exact_replay_is_an_inode_stable_noop(self):
        with self.staged() as seal:
            publish=self.publisher()
            before=self.reader.read_selection(self.case.journal)
            bundle=seed_module.base.J.inspect_journal(self.case.journal)
            stores=self.stores()
            with self.seed.ro(self.case.journal) as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='move_read_seals'").fetchone(),
                    'initial plain v5 stays plain until writer publication')
            with self.lease.reader_lifetime(self.seed.install,exclusive=False):
                selected=publish(self.case.journal,seal=seal)
            self.assertIs(type(selected),self.reader.ReaderSelection)
            with self.assertRaises((AttributeError,TypeError)):selected.epoch=0
            self.assertEqual(selected.epoch,before.epoch+1)
            self.assertEqual(selected.previous_endpoint,before.preferred_endpoint)
            self.assertEqual(selected.preferred_endpoint,seal.retained_endpoint.endpoint_id)
            self.assertEqual(seed_module.base.J.inspect_journal(self.case.journal),bundle)
            with self.seed.ro(self.case.journal) as db:
                # Read every proof component from the same actual RO snapshot.
                selection=db.execute('SELECT epoch,preferred_endpoint,previous_endpoint,transition_id FROM reader_selection').fetchall()
                rows=db.execute('SELECT * FROM move_read_seals').fetchall()
                self.assertEqual(len(rows),1)
                values=self.scalars(rows[0])
                for expected in (seal.move_id,selected.preferred_endpoint,seal.old_generation,
                                 seal.old_tuple_digest,seal.new_tuple_digest,seal.prefix_digest):
                    self.assertIn(expected,values)
                self.assertEqual(selection,[(selected.epoch,selected.preferred_endpoint,selected.previous_endpoint,selected.transition_id)])
                self.assertEqual(db.execute('SELECT epoch,preferred_endpoint,previous_endpoint FROM reader_transitions '
                    'WHERE transition_id=?',(selected.transition_id,)).fetchone(),
                    (selected.epoch,selected.preferred_endpoint,selected.previous_endpoint))
                endpoint=db.execute('SELECT record,attestation FROM read_endpoints WHERE endpoint_id=?',
                                    (selected.preferred_endpoint,)).fetchone()
                self.assertIsNotNone(endpoint)
                self.assertEqual(endpoint[1],(self.case.state/seed_module.base.J.PRODUCTION_ATTESTATION).read_bytes())
            original=self.case.journal.read_bytes();node=self.case.journal.stat()
            self.assertEqual(publish(self.case.journal,seal=seal),selected)
            self.assertEqual(self.case.journal.read_bytes(),original)
            self.assertTrue(os.path.samestat(node,self.case.journal.stat()))
            self.assertEqual(self.stores(),stores)
            copy_path=self.case.state/'move-seal-immutability-control.sqlite'
            copy_path.touch(mode=0o600)
            with self.seed.ro(self.case.journal) as source,closing(sqlite3.connect(copy_path)) as db:
                source.backup(db)
                with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM move_read_seals')
                db.rollback()
                self.assertEqual(db.execute('SELECT count(*) FROM move_read_seals').fetchone(),(1,))

    def test_fresh_reader_uses_persisted_retained_pin_without_installer_and_holds_real_shared_lease(self):
        with self.staged() as seal:
            selected=self.publisher()(self.case.journal,seal=seal)
            before=self.seed.live()
            payload=self.case.home/'fresh-retained-reader.json'
            payload.write_text(json.dumps(dict(home=str(self.case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
                project=str(self.case.project),plugin=str(self.case.plugin),proof=self.case.proof,
                generation=seal.old_generation,exclusion=seal.exclusion_epoch)),encoding='utf8')
            payload.chmod(0o600)
            child=subprocess.Popen([sys.executable,'-B','-I','-c',FRESH_READER,str(ROOT),str(payload)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
            try:
                if not select.select([child.stdout],[],[],20)[0]:raise RuntimeError('fresh retained reader readiness timeout')
                line=child.stdout.readline()
                if not line:raise RuntimeError('fresh retained reader failed: '+child.stderr.read())
                observed=json.loads(line);print(json.dumps(observed),flush=True)
                self.assertEqual(observed['event'],'resolved')
                self.assertEqual(observed['endpoint'],selected.preferred_endpoint)
                self.assertEqual(observed['initialIssued'],0);self.assertEqual(observed['installerCalls'],0)
                writer=subprocess.run([sys.executable,'-B','-I','-c',EXCLUSIVE,str(ROOT/'core'),self.seed.install],
                    capture_output=True,encoding='utf8',timeout=5)
                self.assertEqual(writer.returncode,0,writer.stderr)
                self.assertEqual(json.loads(writer.stdout),dict(state='refused',reason='reader_lifetime_busy'))
                child.stdin.write('release\n');child.stdin.flush()
                self.assertEqual(child.wait(timeout=5),0)
            finally:
                if child.poll() is None:child.kill()
                child.communicate(timeout=5)
            self.assertEqual(self.seed.live(),before)

    def test_publication_and_retained_release_reject_unissued_and_readonly_authority(self):
        with self.staged() as seal:
            publish=self.publisher();original=self.case.journal.read_bytes()
            for bad in ({'move_id':seal.move_id},object.__new__(self.lease.MoveReadSeal)):
                with self.subTest(kind=type(bad).__name__):
                    with self.assertRaises((ValueError,OSError,RuntimeError)):publish(self.case.journal,seal=bad)
                    self.assertEqual(self.case.journal.read_bytes(),original)
            with self.seed.authority(),self.lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as read_a:
                selected=publish(self.case.journal,seal=seal)
                self.assertNotEqual(read_a.endpoint_id,selected.preferred_endpoint)
                before=self.case.journal.read_bytes()
                with self.assertRaises((ValueError,OSError,RuntimeError)):
                    self.reader.cas_reader(self.case.journal,expected_epoch=selected.epoch,
                        expected_endpoint=selected.preferred_endpoint,target=read_a,transition_id='a'*32)
                self.assertEqual(self.case.journal.read_bytes(),before)
                # This is a genuine installer-issued target, so the required
                # refusal proves general CAS cannot silently release the pin.
                with self.assertRaises((ValueError,OSError,RuntimeError)):
                    self.reader.cas_reader(self.case.journal,expected_epoch=selected.epoch,
                        expected_endpoint=selected.preferred_endpoint,target=self.seed.installer.endpoint,transition_id='b'*32)
                self.assertEqual(self.case.journal.read_bytes(),before)
        before=self.case.journal.read_bytes()
        with self.assertRaises((ValueError,OSError,RuntimeError)):publish(self.case.journal,seal=seal)
        self.assertEqual(self.case.journal.read_bytes(),before)

    def test_publisher_actual_cow_kill_leaves_old_or_whole_retained_readable_without_repair(self):
        for step in ('v5-image-fsynced','v5-image-replaced'):
            with self.subTest(step=step):
                seed=seed_module.MoveSealChecks('runTest')
                try:
                    seed.setUp()
                    with seed.prepared():
                        stage,_type=seed.producer()
                        seal=stage(seed.mover._load_intent(seed.case.state,seed.case.project),installer=seed.installer)
                        seed.reader._recheck(seal.retained_endpoint)
                        self.assertTrue(callable(getattr(seed.lease,'publish_move_reader',None)))
                    case=seed.case
                    old=seed.reader.read_selection(case.journal)
                    bundle=seed_module.base.J.inspect_journal(case.journal)
                    stores=seed.live();stores['files'].pop(str(case.journal))
                    payload=case.home/'cow-crash-publication.json'
                    payload.write_text(json.dumps(dict(home=str(case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
                        project=str(case.project),state=str(case.state),plugin=str(case.plugin),proof=case.proof,
                        install=seed.install,step=step,retained=seal.retained_endpoint.endpoint_id,move=seal.move_id,
                        oldEndpoint=old.preferred_endpoint,oldEpoch=old.epoch,generation=seal.old_generation,
                        exclusion=seal.exclusion_epoch)),encoding='utf8');payload.chmod(0o600)
                    child=subprocess.Popen([sys.executable,'-B','-I','-c',CRASH_PUBLISHER,str(ROOT),str(payload)],
                        stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
                    try:
                        if not select.select([child.stdout],[],[],30)[0]:raise RuntimeError('publisher checkpoint timeout')
                        line=child.stdout.readline()
                        if not line:raise RuntimeError('publisher factory exited without a checkpoint')
                        checkpoint=json.loads(line);print(json.dumps(checkpoint),flush=True)
                        if checkpoint.get('event')!='publisher-checkpoint':
                            raise RuntimeError('publisher factory failure, not crash atomicity RED: '+json.dumps(checkpoint))
                        self.assertEqual(checkpoint['step'],step)
                        self.assertEqual(child.wait(timeout=5),-signal.SIGKILL)
                    finally:
                        if child.poll() is None:child.kill()
                        child.communicate(timeout=5)
                    named_before=case.journal.read_bytes();node=case.journal.stat()
                    result=subprocess.run([sys.executable,'-B','-I','-c',CRASH_READ_PROBE,str(ROOT),str(payload)],
                        capture_output=True,encoding='utf8',timeout=30)
                    observed=json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else None
                    print(json.dumps(dict(case='actual-publisher-crash-reader',step=step,exit=child.returncode,
                        result=observed,readReturncode=result.returncode,stderr=result.stderr[:4096])),flush=True)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertEqual(observed['retained'],step=='v5-image-replaced')
                    self.assertEqual(observed['installerCalls'],0)
                    self.assertEqual(case.journal.read_bytes(),named_before)
                    self.assertTrue(os.path.samestat(node,case.journal.stat()))
                    self.assertEqual(seed_module.base.J.inspect_journal(case.journal),bundle)
                    after=seed.live();after['files'].pop(str(case.journal))
                    self.assertEqual(after,stores)
                finally:seed.doCleanups()

    def test_actual_a_to_b_cas_makes_previously_issued_move_seal_stale_without_journal_write(self):
        with self.staged() as seal:
            publish=self.publisher()
            other=self.seed.helper.alternate(self.case,self.reader,self.seed.installer,'publication-stale-b.sqlite')
            before=self.reader.read_selection(self.case.journal)
            changed=self.reader.cas_reader(self.case.journal,expected_epoch=before.epoch,
                expected_endpoint=before.preferred_endpoint,target=other,transition_id='c'*32)
            self.assertEqual(changed.preferred_endpoint,other.endpoint_id)
            self.assertEqual(changed.epoch,before.epoch+1)
            self.reader._recheck(other)
            original=self.case.journal.read_bytes();node=self.case.journal.stat()
            error,selected=None,None
            try:selected=publish(self.case.journal,seal=seal)
            except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
            print(json.dumps(dict(case='actual-cas-stales-issued-move-seal',old=before.preferred_endpoint,
                current=changed.preferred_endpoint,readerEpoch=changed.epoch,error=error,published=selected is not None,
                journalUnchanged=self.case.journal.read_bytes()==original)),flush=True)
            self.assertIsNotNone(error)
            self.assertIsNone(selected)
            self.assertEqual(self.case.journal.read_bytes(),original)
            self.assertTrue(os.path.samestat(node,self.case.journal.stat()))
            self.assertEqual(self.reader.read_selection(self.case.journal),changed)


if __name__=='__main__':unittest.main(verbosity=2)
