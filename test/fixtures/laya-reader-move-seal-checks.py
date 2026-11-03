#!/usr/bin/env python3
"""T2 retained tuple producer contracts; actual SQLite, synthetic bundle/OS only.

No publication, crash-prefix reader or physical/cold reader proof is claimed.
Missing producer APIs are feature RED after real published-v5 proof succeeds.
"""
from contextlib import closing, contextmanager, ExitStack
import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import select
import sqlite3
import stat
import subprocess
import sys
import unittest

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
spec=importlib.util.spec_from_file_location('move_seal_reader_fixture',HERE/'laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


class MoveSealChecks(unittest.TestCase):
    def setUp(self):
        self.helper=base.JournalReaderChecks('runTest')
        self.helper.setUp();self.addCleanup(self.helper.doCleanups)
        class PublishedInstall(base.published.MoveCase):
            STATE_PARTS=('.local','state','memvane','3'*32)
            TEMP_DIR=str(Path('/tmp').resolve())
            TEMP_PREFIX=''
            PROVIDER_EXTRA={'serviceSocketPath':'{state}/runtime/laya-embed.sock'}
            def publish_all(self,roots):
                if not getattr(self,'_provenance_seeded',False):
                    source=self.project/'retained-source.md'
                    source.write_text('Synthetic retained tuple provenance.\n',encoding='utf8')
                    revision=dict(kind='file',path=source.name,collection=self.RAW,
                        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                        size=source.stat().st_size,mtimeNs=source.stat().st_mtime_ns)
                    for page in (self.auto/'wiki').rglob('*.md'):
                        body=page.read_text(encoding='utf8');end=body.index('\n---\n',4)
                        page.write_text(body[:end]+'\nsourceRevisions:\n  - '+json.dumps(revision)+body[end:],encoding='utf8')
                    self._provenance_seeded=True
                return super().publish_all(roots)
        self.case=self.helper.compose(PublishedInstall)
        self.reader=self.helper.reader_api();self.bridge=self.helper.bridge_api()
        self.lease=importlib.import_module('laya_reader_lease')
        self.mover=importlib.import_module('laya_project_dir_move')
        roots={self.case.WIKI:str(self.case.auto/'wiki')}
        import laya_initial_build as build
        import laya_incremental_docs as incremental
        from laya_index_vector import qmd_markdown_title
        identity=build.read(self.case.state)['identity']
        with self.case.runtime(roots) as runtime:
            snapshot=runtime.catalog.require_complete()
            gen=self.case.head()[0]
            rows=runtime.index.db.execute('SELECT m.path,m.hash,c.body FROM membership m '
                'JOIN content c ON c.hash=m.hash WHERE m.gen=? ORDER BY m.path',(gen,)).fetchall()
            runtime.index.db.commit()
            self.assertEqual(len(rows),3)
            for path,hashed,body in rows:
                result=incremental.publish_document(runtime,identity,snapshot,path,hashed,
                    qmd_markdown_title(body,path),body,identity['vectorProfileId'],require_verified=True)
                self.assertIn(result['status'],('published','already_published'),result)
        self.sidecar=self.case.state/incremental.FILE
        self.assertTrue(self.sidecar.is_file())
        with self.ro(self.sidecar) as db:self.assertEqual(db.execute('SELECT count(*) FROM docs').fetchone(),(3,))
        self.installer=self.helper.installer(self.case,self.reader,self.bridge)
        _challenge,_receipts,readiness=self.helper.ready(self.case,self.bridge)
        with self.authority():
            self.selection=self.reader.convert_reader_v5(self.case.state,self.case.project,
                installer=self.installer,readiness=readiness)
            self.reader._recheck(self.installer.endpoint)
        self.assertEqual(base.J.journal_version(self.case.journal),5)
        self.install=base.J.inspect_journal(self.case.journal)['selected']['installId']
        print(json.dumps(dict(case='move-seal-real-seed',journalVersion=5,publishedCards=3,
            completedSidecarRows=3,actualHostExecuted=False)),flush=True)

    def authority(self):
        return self.helper.full_authority(self.case,self.reader,self.bridge,self.lease,self.mover)

    def producer(self):
        stage=getattr(self.lease,'stage_move_reader',None)
        seal=getattr(self.lease,'MoveReadSeal',None)
        if not callable(stage) or seal is None:
            print(json.dumps(dict(case='move-seal-feature-absence',actualV5Proved=True,
                missing=[name for name in ('stage_move_reader','MoveReadSeal')
                         if not hasattr(self.lease,name)],classification='feature-RED-not-existing-product-defect')),flush=True)
            self.fail('T2 feature RED: retained tuple issuer absent after actual published-v5 proof')
        return stage,seal

    @staticmethod
    @contextmanager
    def ro(path):
        with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,isolation_level=None)) as db:
            db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN')
            yield db

    def dump(self,path):
        with self.ro(path) as db:return tuple(db.iterdump())

    def live(self):
        paths=[self.case.journal,self.case.db,self.case.catalog,self.case.store,self.sidecar]
        return dict(files={str(path):path.read_bytes() for path in paths},
                    metadata=self.helper.metadata_snapshot(self.case))

    @contextmanager
    def prepared(self):
        with self.authority(),ExitStack() as locks:
            queue=self.mover._queue_path(self.case.project,None)
            verify=self.mover._locks(locks,self.case.project,self.case.state,queue)
            mode=self.mover.needed(self.case.project)
            self.assertEqual(mode,'rename')
            intent=self.mover._prepare(self.case.project,self.case.state,mode)
            verify()
            self.assertEqual(self.mover._canonical(self.mover._load_intent(self.case.state,self.case.project)),
                             self.mover._canonical(intent))
            yield intent

    def test_real_owner_stage_succeeds_with_shared_reader_and_retains_completed_tuple(self):
        stage,seal_type=self.producer()
        with self.prepared() as intent:
            before=self.live();head=self.case.head()[0]
            originals={key:path for key,path in (('catalog_path',self.case.catalog),
                ('binding_path',self.case.store),('sidecar_path',self.sidecar))}
            logical={key:self.dump(path) for key,path in originals.items()}
            with self.lease.reader_lifetime(self.install,exclusive=False):
                seal=stage(intent,installer=self.installer)
            self.assertIs(type(seal),seal_type)
            with self.assertRaises((ValueError,TypeError)):seal_type()
            for field in ('move_id','retained_endpoint','old_generation','exclusion_epoch',
                          'old_tuple_digest','new_tuple_digest','path_aliases','prefix_digest'):
                self.assertTrue(hasattr(seal,field),field)
            with self.assertRaises((AttributeError,TypeError)):seal.move_id='caller-value'
            endpoint=seal.retained_endpoint
            self.assertIs(type(endpoint),self.reader.VerifiedEndpoint)
            self.assertEqual(seal.old_generation,head)
            self.assertIs(type(seal.exclusion_epoch),int)
            self.assertGreaterEqual(seal.exclusion_epoch,0)
            for field in ('old_tuple_digest','new_tuple_digest','prefix_digest'):
                self.assertRegex(getattr(seal,field),r'^[0-9a-f]{64}$')
            for key,source in originals.items():
                retained=Path(endpoint.record[key])
                self.assertNotEqual(retained,source,key)
                self.assertEqual(retained.resolve(strict=True),retained)
                self.assertTrue(retained.is_relative_to(self.case.state))
                info=retained.lstat()
                self.assertTrue(stat.S_ISREG(info.st_mode))
                self.assertEqual((info.st_uid,info.st_nlink),(os.getuid(),1))
                self.assertEqual(info.st_mode & 0o077,0)
                self.assertEqual(self.dump(retained),logical[key],key)
            with self.ro(endpoint.record['db_path']) as db:
                rows=db.execute('SELECT m.path,c.body FROM membership m JOIN content c ON c.hash=m.hash '
                                'WHERE m.gen=? ORDER BY m.path',(seal.old_generation,)).fetchall()
                self.assertEqual(len(rows),3)
                self.assertTrue(any('otter' in body for _path,body in rows))
            self.assertEqual(self.live(),before)

    def test_caller_capabilities_and_source_paths_cannot_issue_a_move_seal(self):
        stage,_seal_type=self.producer()
        with self.prepared() as intent:
            outside=self.case.home/'unowned.sqlite';outside.write_bytes(b'not a store');outside.chmod(0o600)
            changed=copy.deepcopy(intent);changed['catalog']['path']=str(outside)
            for label,request,installer in (('raw-installer',intent,{}),
                    ('public-constructor',intent,object.__new__(self.reader.InstallerSeal)),
                    ('caller-source-path',changed,self.installer)):
                with self.subTest(kind=label):
                    before=self.live()
                    with self.assertRaises((ValueError,OSError,RuntimeError,TypeError)):
                        stage(request,installer=installer)
                    self.assertEqual(self.live(),before)

    def test_actual_catalog_sidecar_body_profile_and_symlink_tamper_are_refused(self):
        stage,_seal_type=self.producer()
        with self.prepared() as intent:
            mutations=((self.case.catalog,"UPDATE meta SET value=? WHERE key='scope'",'f'*32,
                        "SELECT value FROM meta WHERE key='scope'"),
                       (self.case.store,"UPDATE meta SET value=? WHERE key='scope'",'f'*32,
                        "SELECT value FROM meta WHERE key='scope'"),
                       (self.sidecar,'UPDATE meta SET profile_id=?','f'*64,'SELECT profile_id FROM meta'),
                       (self.sidecar,'UPDATE docs SET body=? WHERE path=(SELECT min(path) FROM docs)',
                        'tampered completed body','SELECT body FROM docs ORDER BY path LIMIT 1'))
            for path,sql,bad,query in mutations:
                with self.subTest(path=path.name,query=query):
                    with closing(sqlite3.connect(path)) as db:
                        old=db.execute(query).fetchone()[0];db.execute(sql,(bad,));db.commit()
                    try:
                        before=self.live()
                        with self.assertRaises((ValueError,OSError,RuntimeError)):
                            stage(intent,installer=self.installer)
                        self.assertEqual(self.live(),before)
                    finally:
                        with closing(sqlite3.connect(path)) as db:db.execute(sql,(old,));db.commit()
            source=self.case.store;saved=source.with_name(source.name+'.saved')
            source.rename(saved);source.symlink_to(saved)
            try:
                before=self.live()
                with self.assertRaises((ValueError,OSError,RuntimeError)):
                    stage(intent,installer=self.installer)
                self.assertEqual(self.live(),before)
            finally:source.unlink();saved.rename(source)

    def test_outside_owner_call_cannot_bypass_a_real_competing_install_writer(self):
        stage,_seal_type=self.producer()
        with self.prepared() as intent:pass
        script='''import signal,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from laya_product_install import _install_lock
signal.alarm(12)
with _install_lock(Path(sys.argv[2])):
    print('held',flush=True)
    assert sys.stdin.readline().strip()=='release'
'''
        process=subprocess.Popen([sys.executable,'-B','-I','-c',script,str(ROOT/'core'),str(self.case.state)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
        try:
            self.assertTrue(select.select([process.stdout],[],[],5)[0],'owned writer startup timeout')
            self.assertEqual(process.stdout.readline().strip(),'held')
            before=self.live()
            with self.authority(),self.assertRaises((ValueError,OSError,RuntimeError)):
                stage(intent,installer=self.installer)
            self.assertEqual(self.live(),before)
        finally:
            if process.poll() is None:
                process.stdin.write('release\n');process.stdin.flush()
            try:process.communicate(timeout=4)
            except subprocess.TimeoutExpired:process.kill();process.communicate(timeout=4)


if __name__=='__main__':unittest.main(verbosity=2)
