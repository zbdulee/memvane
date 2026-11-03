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


if __name__=='__main__':unittest.main(verbosity=2)
