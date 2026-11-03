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
from types import SimpleNamespace
import select
import signal
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


FRESH_STAGE_CHILD=r'''
from contextlib import ExitStack,nullcontext
import importlib.util,json,os,signal,sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root,payload,mode=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
sys.path.insert(0,str(root/'core'))
p=json.loads(payload.read_text())
os.environ['HOME']=p['home']
os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime']
signal.alarm(35)
spec=importlib.util.spec_from_file_location('fresh_move_stage_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as reader,laya_reader_lease as lease,laya_state_move as state_move
import laya_project_dir_move as project_move
assert Path(reader.__file__).resolve().parent==root/'core'
case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
helper=base.JournalReaderChecks('runTest')
project,state=Path(p['project']),Path(p['state'])
mover=state_move if p['kind']=='state' else project_move
phase='fresh-issuer'
initial=len(reader._issued)
try:
    assert initial==0,'fresh child must not inherit an issued capability'
    with helper.full_authority(case,reader,lease,mover),mock.patch.object(state_move,'_provider_lane',lambda:nullcontext()):
        installer=reader.seal_current_reader(state,project,executing_root=case.plugin)
        reader._recheck(installer.endpoint)
        phase='owner-acquire'
        with ExitStack() as locks:
            intent=mover._load_intent(state,project)
            assert intent is not None,'durable parent-prepared intent required'
            if p['kind']=='state':mover._locks(locks,project,state,home=Path(p['home']))
            else:mover._locks(locks,project,state,mover._queue_path(project,intent))
            phase='stage'
            install=base.J.inspect_journal(state/'install-journal.sqlite')['selected']['installId']
            with lease.reader_lifetime(install,exclusive=False):
                if mode=='kill-backup':
                    import sqlite3
                    from urllib.parse import urlparse,unquote
                    original_connect=sqlite3.connect
                    catalog=Path(p['catalog'])
                    protected={Path(value) for key,value in installer.endpoint.record.items()
                               if key in ('db_path','catalog_path','binding_path','sidecar_path') and value is not None}
                    protected.update(Path(json.loads(intent['oldRoute'])[key]) for key in ('journalPath','queuePath'))
                    def database_path(raw):
                        raw=str(raw)
                        if raw==':memory:':return None
                        return Path(unquote(urlparse(raw).path)) if raw.startswith('file:') else Path(raw)
                    class BackupCrashConnection(sqlite3.Connection):
                        def __init__(self,database,*args,**kwargs):
                            super().__init__(database,*args,**kwargs)
                            self.fixture_path=database_path(database)
                        def backup(self,target,*,pages=-1,progress=None,name='main',sleep=.250):
                            if self.fixture_path!=catalog:
                                return super().backup(target,pages=pages,progress=progress,name=name,sleep=sleep)
                            destination=getattr(target,'fixture_path',None)
                            assert destination is not None and destination!=catalog and destination.is_relative_to(state)
                            assert destination not in protected,'backup observer must never target an original store'
                            source_pages=self.execute('PRAGMA page_count').fetchone()[0]
                            assert source_pages>1,'actual multi-page catalog is required'
                            def copied(status,remaining,total):
                                if progress is not None:progress(status,remaining,total)
                                if remaining>0 and total>remaining:
                                    info=destination.lstat()
                                    print(json.dumps(dict(event='backup-page-copied',kind=p['kind'],pid=os.getpid(),
                                        initialIssued=initial,freshExec=True,source=str(catalog),destination=str(destination),
                                        sourcePages=source_pages,remaining=remaining,total=total,status=status,
                                        destinationNode=[info.st_dev,info.st_ino],destinationBytes=info.st_size)),flush=True)
                                    os.kill(os.getpid(),signal.SIGKILL)
                            result=super().backup(target,pages=1,progress=copied,name=name,sleep=sleep)
                            raise RuntimeError('actual backup completed without the required partial-page checkpoint')
                    def observed_connect(*args,**kwargs):
                        kwargs['factory']=BackupCrashConnection
                        return original_connect(*args,**kwargs)
                    sqlite3.connect=observed_connect
                seal=lease.stage_move_reader(intent,installer=installer)
            record=seal.retained_endpoint.record
            print(json.dumps(dict(event='staged',kind=p['kind'],pid=os.getpid(),initialIssued=initial,
                freshExec=True,endpoint=seal.retained_endpoint.endpoint_id,moveId=seal.move_id,
                sealType=type(seal).__name__,endpointType=type(seal.retained_endpoint).__name__,oldGeneration=seal.old_generation,
                oldDigest=seal.old_tuple_digest,newDigest=seal.new_tuple_digest,prefixDigest=seal.prefix_digest,
                retainedPaths={key:record.get(key) for key in ('db_path','catalog_path','binding_path','sidecar_path')},
                actualHostExecuted=False)),flush=True)
            if mode=='hold':
                assert sys.stdin.readline().strip()=='release'
except Exception as exc:
    print(json.dumps(dict(event='error',kind=p['kind'],phase=phase,errorType=type(exc).__name__,
        reason=str(exc),initialIssued=initial,freshExec=True)),flush=True)
    sys.exit(2)
'''


class MoveSealChecks(unittest.TestCase):
    INSTALL_ID='3'*32
    STATE_PARTS=('.local','state','memvane','3'*32)
    PUBLISH_SIDECAR=True

    def setUp(self):
        self.helper=base.JournalReaderChecks('runTest')
        self.helper.setUp();self.addCleanup(self.helper.doCleanups)
        class PublishedInstall(base.published.MoveCase):
            INSTALL_ID=getattr(self,'INSTALL_ID','3'*32)
            STATE_PARTS=self.STATE_PARTS
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
        self.sidecar=self.case.state/incremental.FILE
        if self.PUBLISH_SIDECAR:
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
            self.assertTrue(self.sidecar.is_file())
            with self.ro(self.sidecar) as db:self.assertEqual(db.execute('SELECT count(*) FROM docs').fetchone(),(3,))
        else:
            self.assertFalse(os.path.lexists(self.sidecar),'absence fixture must not create/delete an issued live sidecar')
            self.assertEqual(len(self.case.head()[1]),3)
        self.installer=self.helper.installer(self.case,self.reader,self.bridge)
        _challenge,_receipts,readiness=self.helper.ready(self.case,self.bridge)
        with self.authority():
            self.selection=self.reader.convert_reader_v5(self.case.state,self.case.project,
                installer=self.installer,readiness=readiness)
            self.reader._recheck(self.installer.endpoint)
        self.assertEqual(base.J.journal_version(self.case.journal),5)
        self.install=base.J.inspect_journal(self.case.journal)['selected']['installId']
        print(json.dumps(dict(case='move-seal-real-seed',journalVersion=5,publishedCards=3,
            completedSidecarRows=3 if self.PUBLISH_SIDECAR else 0,
            sidecarPublished=self.PUBLISH_SIDECAR,actualHostExecuted=False)),flush=True)

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
        paths=[self.case.journal,self.case.db,self.case.catalog,self.case.store]
        files={str(path):path.read_bytes() for path in paths}
        if self.PUBLISH_SIDECAR:files[str(self.sidecar)]=self.sidecar.read_bytes()
        else:
            self.assertFalse(os.path.lexists(self.sidecar))
            files[str(self.sidecar)]=None
        return dict(files=files,
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

    def _check_retained_tamper_rejection(self,stage,intent,seal,before,case_name,control):
        artifacts=set(self.case.state.rglob('*'))
        replay_error,reissued=None,None
        try:reissued=stage(intent,installer=self.installer)
        except (ValueError,OSError,RuntimeError) as exc:replay_error=str(exc)
        recheck_error=None
        try:self.reader._recheck(seal.retained_endpoint)
        except (ValueError,OSError,RuntimeError) as exc:recheck_error=str(exc)
        after=set(self.case.state.rglob('*'))
        print(json.dumps(dict(case=case_name,actualValidatorControl=control,
            reissued=reissued is not None,replayError=replay_error,issuedTokenRecheckError=recheck_error,
            newArtifacts=sorted(str(path) for path in after-artifacts),liveUnchanged=self.live()==before)),flush=True)
        self.assertEqual(self.live(),before)
        self.assertEqual(after,artifacts,'rejected replay must not create another stage')
        # Keep both independent violations visible in the same execution.
        with self.subTest(boundary='same-intent-reissue'):
            self.assertIsNotNone(replay_error,'tampered retained component must refuse replay')
            self.assertIsNone(reissued)
        with self.subTest(boundary='already-issued-retained-token'):
            self.assertIsNotNone(recheck_error,'original retained token must reject the same actual tamper')

    def test_retained_catalog_header_change_invisible_to_iterdump_refuses_replay_and_recheck(self):
        stage,_seal_type=self.producer()
        source=importlib.import_module('laya_source_catalog')
        with self.prepared() as intent:
            seal=stage(intent,installer=self.installer)
            self.reader._recheck(seal.retained_endpoint)
            retained=Path(seal.retained_endpoint.record['catalog_path'])
            self.assertNotEqual(retained,self.case.catalog)
            before=self.live();dump_before=self.dump(retained)
            with self.ro(retained) as db:
                header_before=db.execute('PRAGMA application_id').fetchone()[0]
                expected_binding=db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0]
                source.LayaSourceCatalog._check_identity(SimpleNamespace(db=db,binding=expected_binding))
            self.assertEqual(header_before,source.APP_ID)
            with closing(sqlite3.connect(retained)) as db:
                db.execute('PRAGMA application_id=0');db.commit()
            dump_after=self.dump(retained)
            with self.ro(retained) as db:
                header_after=db.execute('PRAGMA application_id').fetchone()[0]
                with self.assertRaisesRegex(ValueError,'^catalog_schema_unknown$'):
                    source.LayaSourceCatalog._check_identity(SimpleNamespace(db=db,binding=expected_binding))
            print(json.dumps(dict(case='retained-catalog-header-control',applicationIdBefore=header_before,
                applicationIdAfter=header_after,iterdumpUnchanged=dump_after==dump_before,
                actualReadonlyValidator='catalog_schema_unknown')),flush=True)
            self.assertEqual(header_after,0)
            self.assertEqual(dump_after,dump_before,'control must isolate header state omitted by iterdump')
            self._check_retained_tamper_rejection(stage,intent,seal,before,'retained-catalog-header',
                'actual-readonly-catalog-schema-refusal; iterdump-identical')

    def test_retained_sidecar_hidden_rowid_change_refuses_replay_and_recheck(self):
        stage,_seal_type=self.producer()
        validator=importlib.import_module('laya_move_read_seal')
        build=importlib.import_module('laya_initial_build')
        with self.prepared() as intent:
            seal=stage(intent,installer=self.installer)
            self.reader._recheck(seal.retained_endpoint)
            retained=Path(seal.retained_endpoint.record['sidecar_path'])
            self.assertNotEqual(retained,self.sidecar)
            identity=build.read(self.case.state)['identity'];decision=json.loads(intent['oldRoute'])
            validator._sidecar(retained,retained.parent,identity,self.case.db,self.case.state,decision)
            before=self.live();dump_before=self.dump(retained)
            with self.ro(retained) as db:
                maximum=db.execute('SELECT max(rowid) FROM docs').fetchone()[0]
                self.assertIsNotNone(maximum)
                logical_before=db.execute('SELECT path,hash,title,body,profile_id,chunk_count,scan_id FROM docs ORDER BY path').fetchall()
                fts_before=db.execute('SELECT rowid,path,title,body FROM docs_fts ORDER BY path').fetchall()
            guard=None
            with closing(sqlite3.connect(retained)) as db:
                try:
                    self.assertEqual(db.execute('UPDATE docs SET rowid=? WHERE rowid=?',(maximum+10,maximum)).rowcount,1)
                    db.commit()
                except sqlite3.IntegrityError as exc:
                    db.rollback();guard=str(exc)
            if guard is not None:
                print(json.dumps(dict(case='retained-sidecar-rowid',schemaGuard=guard,producerRefusalClaimed=False)),flush=True)
                self.assertEqual(self.dump(retained),dump_before)
                self.assertEqual(self.live(),before)
                return
            dump_after=self.dump(retained)
            with self.ro(retained) as db:
                logical_after=db.execute('SELECT path,hash,title,body,profile_id,chunk_count,scan_id FROM docs ORDER BY path').fetchall()
                actual=db.execute('SELECT rowid,path,title,body FROM docs ORDER BY path').fetchall()
                fts_after=db.execute('SELECT rowid,path,title,body FROM docs_fts ORDER BY path').fetchall()
            self.assertEqual(logical_after,logical_before)
            self.assertEqual(fts_after,fts_before)
            self.assertNotEqual(actual,fts_after)
            self.assertEqual(dump_after,dump_before,'last-row rowid mutation must remain invisible to iterdump')
            with self.assertRaisesRegex(ValueError,'^reader_move_sidecar_changed$'):
                validator._sidecar(retained,retained.parent,identity,self.case.db,self.case.state,decision)
            print(json.dumps(dict(case='retained-sidecar-rowid-control',oldRowid=maximum,newRowid=maximum+10,
                iterdumpUnchanged=True,logicalDocsUnchanged=True,ftsUnchanged=True,
                actualReadonlyValidator='reader_move_sidecar_changed')),flush=True)
            self._check_retained_tamper_rejection(stage,intent,seal,before,'retained-sidecar-hidden-rowid',
                'actual-readonly-docs-FTS-parity-refusal; iterdump-identical')

    def test_copy_refuses_a_real_hot_orphan_at_final_destination_before_creating_database(self):
        copy_module=importlib.import_module('laya_move_read_seal')
        with self.prepared():
            stage=self.case.state/'orphan-copy-fixture';stage.mkdir(mode=0o700)
            name='catalog-target.sqlite'
            destination=copy_module._copy(self.case.catalog,self.case.state,stage,name)
            with closing(sqlite3.connect(destination)) as db:
                self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchone(),('delete',))
                db.execute('CREATE TABLE hot_probe(id INTEGER PRIMARY KEY,value TEXT NOT NULL)')
                db.executemany('INSERT INTO hot_probe VALUES(?,?)',[(n,'original-'+str(n)+'-'+'x'*3000) for n in range(80)])
                db.commit()
                originals=db.execute('SELECT id,value FROM hot_probe ORDER BY id').fetchall()
            main_before=destination.read_bytes()
            writer=r'''import json,os,signal,sqlite3,sys
from pathlib import Path
signal.alarm(25)
path=Path(sys.argv[1])
db=sqlite3.connect(path,isolation_level=None)
assert db.execute('PRAGMA journal_mode=DELETE').fetchone()==('delete',)
db.execute('PRAGMA synchronous=FULL')
db.execute('PRAGMA cache_size=1')
db.execute('PRAGMA cache_spill=ON')
db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE hot_probe SET value='changed-'||value")
journal=Path(str(path)+'-journal')
assert journal.is_file() and journal.stat().st_size>0
print(json.dumps(dict(event='uncommitted-spill',pid=os.getpid(),rows=db.execute('SELECT count(*) FROM hot_probe').fetchone()[0],
    journalBytes=journal.stat().st_size)),flush=True)
assert sys.stdin.readline().strip()=='release'
'''
            process=subprocess.Popen([sys.executable,'-B','-I','-c',writer,str(destination)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
            try:
                if not select.select([process.stdout],[],[],15)[0]:raise RuntimeError('actual hot writer checkpoint unavailable')
                line=process.stdout.readline()
                if not line:raise RuntimeError('hot journal factory failed before its checkpoint')
                evidence=json.loads(line)
                if evidence.get('event')!='uncommitted-spill':raise RuntimeError('hot journal factory did not reach actual spill')
                process.kill();self.assertEqual(process.wait(timeout=5),-signal.SIGKILL)
            finally:
                if process.poll() is None:process.kill()
                process.communicate(timeout=5)
            journal=Path(str(destination)+'-journal')
            if destination.read_bytes()==main_before:
                raise RuntimeError('actual SQLite pager spill did not change main bytes; not an orphan-copy RED')
            journal_bytes=journal.read_bytes();journal_node=(journal.stat().st_dev,journal.stat().st_ino)
            control=stage/'rollback-control';control.mkdir(mode=0o700)
            trial=control/'trial.sqlite';trial.write_bytes(destination.read_bytes());trial.chmod(0o600)
            trial_journal=Path(str(trial)+'-journal');trial_journal.write_bytes(journal_bytes);trial_journal.chmod(0o600)
            try:
                with closing(sqlite3.connect(trial)) as db:
                    restored=db.execute('SELECT id,value FROM hot_probe ORDER BY id').fetchall()
                    integrity=db.execute('PRAGMA integrity_check').fetchall()
            except sqlite3.Error as exc:
                raise RuntimeError('actual hot rollback control could not recover copied metadata') from exc
            if restored!=originals or integrity!=[('ok',)] or trial_journal.exists():
                raise RuntimeError('valid hot journal/SQLite rollback cleanup was not demonstrated')
            self.assertEqual(journal.read_bytes(),journal_bytes)
            destination.unlink()  # Only this test-owned main, after actual hot proof.
            before=self.live();entries=set(stage.rglob('*'))
            error,result=None,None
            try:result=copy_module._copy(self.case.catalog,self.case.state,stage,name)
            except (ValueError,OSError,RuntimeError,sqlite3.Error) as exc:error=str(exc)
            print(json.dumps(dict(case='final-copy-orphan-hot-journal',checkpoint=evidence,actualExit=process.returncode,
                realRollbackControl=True,error=error,destinationCreated=destination.exists(),
                orphanPreserved=journal.exists() and journal.read_bytes()==journal_bytes,
                newArtifacts=sorted(str(path) for path in set(stage.rglob('*'))-entries),liveUnchanged=self.live()==before)),flush=True)
            self.assertEqual(self.live(),before)
            with self.subTest(boundary='refusal-before-final-main'):
                self.assertIsNotNone(error)
                self.assertIsNone(result)
                self.assertFalse(destination.exists())
            self.assertEqual(journal.read_bytes(),journal_bytes)
            self.assertEqual((journal.stat().st_dev,journal.stat().st_ino),journal_node)
            self.assertEqual(set(stage.rglob('*')),entries)

    def test_actual_catalog_sidecar_body_profile_and_symlink_tamper_are_refused(self):
        stage,_seal_type=self.producer()
        with self.prepared() as intent:
            before=self.live()
            with closing(sqlite3.connect(self.case.store)) as db:
                with self.assertRaisesRegex(sqlite3.IntegrityError,'binding_store_sealed'):
                    db.execute("UPDATE meta SET value=? WHERE key='scope'",('f'*32,))
                db.rollback()
            self.assertEqual(self.live(),before)
            print(json.dumps(dict(case='binding-metadata-mutation',result='blocked-by-actual-schema-trigger',
                producerRefusalNotClaimedForUnchangedInput=True)),flush=True)
            mutations=((self.case.catalog,"UPDATE meta SET value=? WHERE key='scope'",'f'*32,
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

    def test_preferred_b_is_retained_or_refused_before_any_staging_artifact(self):
        stage,_seal_type=self.producer()
        other=self.helper.alternate(self.case,self.reader,self.installer,'preferred-b.sqlite')
        with self.authority():
            selected=self.reader.cas_reader(self.case.journal,expected_epoch=self.selection.epoch,
                expected_endpoint=self.selection.preferred_endpoint,target=other,transition_id='d'*32)
        self.assertEqual(self.reader.read_selection(self.case.journal).preferred_endpoint,other.endpoint_id)
        with self.prepared() as intent:
            before=self.live()
            tree_before={str(path.relative_to(self.case.state)) for path in self.case.state.rglob('*')}
            directories_before={str(path.relative_to(self.case.state)) for path in self.case.state.rglob('*') if path.is_dir()}
            error,seal=None,None
            try:seal=stage(intent,installer=self.installer)
            except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
            tree_after={str(path.relative_to(self.case.state)) for path in self.case.state.rglob('*')}
            directories_after={str(path.relative_to(self.case.state)) for path in self.case.state.rglob('*') if path.is_dir()}
            observed=dict(case='preferred-b-retained-or-prestage-refusal',selected=selected.preferred_endpoint,
                actualPreferred=self.reader.read_selection(self.case.journal).preferred_endpoint,
                expectedDb=other.record['db_path'],returnedDb=None if seal is None else seal.retained_endpoint.record['db_path'],
                error=error,newArtifacts=sorted(tree_after-tree_before),liveUnchanged=self.live()==before)
            print(json.dumps(observed),flush=True)
            self.assertEqual(self.reader.read_selection(self.case.journal),selected)
            self.assertEqual(self.live(),before)
            if error is not None:
                self.assertRegex(error,r'^reader_[a-z0-9_]+$','only explicit reader refusal is admissible')
                self.assertEqual(directories_after,directories_before,'unsupported preferred endpoint must refuse before staging')
                self.assertEqual(tree_after,tree_before,'prestage refusal must not leave producer artifacts')
            else:
                self.assertIsNotNone(seal)
                self.assertEqual(seal.retained_endpoint.record['db_path'],other.record['db_path'],observed)

    def test_complete_catalog_row_digest_mismatch_is_not_sealed(self):
        stage,_seal_type=self.producer()
        source=importlib.import_module('laya_source_catalog')
        with self.prepared() as intent:
            with self.ro(self.case.store) as db:
                row=db.execute('SELECT record FROM bindings WHERE scope=? AND generation=? AND db_incarnation=?',
                    (self.case.route['scopeId'],intent['generation']['base'],self.case.incarnation)).fetchone()
                self.assertIsNotNone(row,'actual selected generation binding is required')
                bound=source.GenerationSourceBinding.from_record(json.loads(row[0]))
            def digest(db,seq):
                value=hashlib.sha256()
                for path,sha,size in db.execute('SELECT path,sha256,size FROM scan_files WHERE seq=? ORDER BY path',(seq,)):
                    value.update(source._raw_line(path,sha,size))
                return value.hexdigest()
            guard=None
            with closing(sqlite3.connect(self.case.catalog)) as db:
                scan=db.execute('SELECT seq,phase,raw_digest FROM scans WHERE scan_id=?',(bound.catalog_scan_id,)).fetchone()
                self.assertIsNotNone(scan)
                self.assertEqual(scan[1:],('complete',bound.catalog_raw_digest))
                self.assertEqual(digest(db,scan[0]),scan[2],'seed scan rows must match the committed raw digest')
                target=db.execute('SELECT path,sha256 FROM scan_files WHERE seq=? ORDER BY path LIMIT 1',(scan[0],)).fetchone()
                self.assertIsNotNone(target)
                corrupted='f'*64 if target[1]!='f'*64 else 'e'*64
                before_guard=self.live()
                try:
                    db.execute('UPDATE scan_files SET sha256=? WHERE seq=? AND path=?',(corrupted,scan[0],target[0]));db.commit()
                except sqlite3.IntegrityError as exc:
                    db.rollback();guard=str(exc)
                recomputed=digest(db,scan[0])
                stored=db.execute('SELECT raw_digest FROM scans WHERE seq=?',(scan[0],)).fetchone()[0]
            if guard is not None:
                print(json.dumps(dict(case='catalog-row-tamper',schemaGuard=guard,producerRefusalClaimed=False)),flush=True)
                self.assertEqual(self.live(),before_guard)
                return
            self.assertNotEqual(recomputed,stored,'actual row mutation must break the complete scan commitment')
            self.assertEqual(stored,bound.catalog_raw_digest)
            before=self.live();error,seal=None,None
            try:seal=stage(intent,installer=self.installer)
            except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
            print(json.dumps(dict(case='catalog-row-tamper',boundScan=bound.catalog_scan_id,storedDigest=stored,
                recomputedDigest=recomputed,issued=seal is not None,error=error,liveUnchanged=self.live()==before)),flush=True)
            self.assertEqual(self.live(),before)
            self.assertIsNotNone(error,'mismatched actual scan rows must refuse issuance')
            self.assertIsNone(seal)

    def test_prepared_new_generation_fts_damage_is_not_sealed(self):
        stage,_seal_type=self.producer()
        index_module=importlib.import_module('laya_index_a')
        vector=importlib.import_module('laya_index_vector')
        with self.prepared() as intent:
            old,new=intent['generation']['base'],intent['generation']['gen']
            guard=None
            with closing(sqlite3.connect(self.case.db)) as db:
                index=SimpleNamespace(db=db)
                self.assertTrue(index_module.LayaIndexA._generation_fts_matches(index,old))
                self.assertTrue(index_module.LayaIndexA._generation_fts_matches(index,new))
                seal_before=vector.verify_generation_seal(index,new)
                members_before=db.execute('SELECT path,hash,eligible FROM membership WHERE gen=? ORDER BY path',(new,)).fetchall()
                ordinal=db.execute('SELECT ord FROM gen_index WHERE gen=?',(new,)).fetchone()
                self.assertIsNotNone(ordinal)
                table=index_module._gen_fts(ordinal[0])
                rowid=db.execute('SELECT rowid FROM "'+table+'" ORDER BY rowid LIMIT 1').fetchone()
                self.assertIsNotNone(rowid)
                before_guard=self.live()
                try:
                    db.execute('DELETE FROM "'+table+'" WHERE rowid=?',rowid);db.commit()
                except sqlite3.IntegrityError as exc:
                    db.rollback();guard=str(exc)
                old_matches=index_module.LayaIndexA._generation_fts_matches(index,old)
                new_matches=index_module.LayaIndexA._generation_fts_matches(index,new)
                self.assertEqual(db.execute('SELECT path,hash,eligible FROM membership WHERE gen=? ORDER BY path',(new,)).fetchall(),members_before)
                self.assertEqual(vector.verify_generation_seal(index,new),seal_before)
            if guard is not None:
                print(json.dumps(dict(case='new-fts-tamper',schemaGuard=guard,producerRefusalClaimed=False)),flush=True)
                self.assertEqual(self.live(),before_guard)
                return
            self.assertTrue(old_matches,'old published head remains valid')
            self.assertFalse(new_matches,'the actual prepared FTS must be damaged before invoking producer')
            before=self.live();error,seal=None,None
            try:seal=stage(intent,installer=self.installer)
            except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
            print(json.dumps(dict(case='new-fts-tamper',generation=new,ftsTable=table,oldFtsValid=old_matches,
                newFtsValid=new_matches,issued=seal is not None,error=error,liveUnchanged=self.live()==before)),flush=True)
            self.assertEqual(self.live(),before)
            self.assertIsNotNone(error,'prepared FTS damage must refuse issuance')
            self.assertIsNone(seal)


class StateMoveSealChecks(unittest.TestCase):
    """Reuse seed/helpers without inheriting the seven project tests."""
    STATE_PARTS=('.local','state','laya','3'*32)
    PUBLISH_SIDECAR=True
    setUp=MoveSealChecks.setUp
    authority=MoveSealChecks.authority
    producer=MoveSealChecks.producer
    ro=staticmethod(MoveSealChecks.ro)
    dump=MoveSealChecks.dump
    live=MoveSealChecks.live

    @contextmanager
    def prepared_state(self):
        mover=importlib.import_module('laya_state_move')
        with self.authority(),self.helper.full_authority(self.case,self.reader,mover),ExitStack() as locks:
            mover._locks(locks,self.case.project,self.case.state,home=self.case.home)
            intent=mover._prepare(self.case.project,self.case.state,self.case.home)
            self.assertEqual(mover._canonical(mover._load_intent(self.case.state,self.case.project)),
                             mover._canonical(intent))
            if self.PUBLISH_SIDECAR:
                self.assertIsNotNone(intent['docs'],'actual completed sidecar must be bound in state intent')
            else:self.assertIsNone(intent['docs'],'absence seed must have an actual None docs intent')
            yield mover,intent

    def test_state_staging_rejects_changed_actual_pending_pointer_before_artifacts(self):
        stage,_seal_type=self.producer()
        with self.prepared_state() as (mover,intent):
            pointer=mover.pointer_path(self.case.project)
            value=json.loads(pointer.read_bytes())
            self.assertEqual(value['newRouteSha256'],hashlib.sha256(intent['newRoute'].encode('utf8')).hexdigest())
            value['newRouteSha256']='f'*64 if value['newRouteSha256']!='f'*64 else 'e'*64
            pointer.write_bytes(mover._canonical(value))
            tampered=pointer.read_bytes()
            with self.assertRaisesRegex(mover.StateMoveBlocked,'^state_move_pointer_changed$'):
                mover._write_pointer(intent)
            self.assertEqual(pointer.read_bytes(),tampered,'actual pointer control must refuse without rewriting')
            before=self.live();artifacts=set(self.case.state.rglob('*'))
            error,seal=None,None
            try:seal=stage(intent,installer=self.installer)
            except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
            after=set(self.case.state.rglob('*'))
            print(json.dumps(dict(case='state-stage-pending-pointer-digest',actualPointerControl='state_move_pointer_changed',
                issued=seal is not None,error=error,newArtifacts=sorted(str(p) for p in after-artifacts),
                pointerPreserved=pointer.read_bytes()==tampered,liveUnchanged=self.live()==before)),flush=True)
            self.assertEqual(self.live(),before)
            self.assertEqual(pointer.read_bytes(),tampered)
            self.assertIsNotNone(error,'producer must honor the actual pending pointer proof')
            self.assertIsNone(seal)
            self.assertEqual(after,artifacts,'pointer disagreement must refuse before staging')

    def test_state_staging_under_shared_reader_retains_old_and_rebases_new_completed_sidecar(self):
        stage,seal_type=self.producer()
        with self.prepared_state() as (mover,intent):
            before=self.live()
            files_before=set(self.case.state.rglob('*'))
            old_generation=self.case.head()[0]
            originals={'catalog_path':self.case.catalog,'binding_path':self.case.store,'sidecar_path':self.sidecar}
            logical={key:self.dump(path) for key,path in originals.items()}
            with self.ro(self.sidecar) as db:
                docs_before=db.execute('SELECT path,hash,title,body,profile_id,chunk_count,scan_id FROM docs ORDER BY path').fetchall()
                fts_before=db.execute('SELECT path,title,body FROM docs_fts ORDER BY path').fetchall()
            error,seal=None,None
            with self.lease.reader_lifetime(self.install,exclusive=False):
                try:seal=stage(intent,installer=self.installer)
                except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
            print(json.dumps(dict(case='state-move-tuple-stage',error=error,issued=seal is not None,
                ownerScope='actual-state-mover-locks',sharedLifetimeHeldDuringStage=True,
                featureAbsence=error=='reader_move_stage_unsupported',liveUnchanged=self.live()==before)),flush=True)
            self.assertEqual(self.live(),before)
            self.assertIsNone(error,'state staging feature RED follows real v5/owner/intent proof')
            self.assertIs(type(seal),seal_type)
            self.assertIs(type(seal.retained_endpoint),self.reader.VerifiedEndpoint)
            self.assertEqual(seal.old_generation,old_generation)
            self.assertRegex(seal.new_tuple_digest,r'^[0-9a-f]{64}$')
            self.assertNotEqual(seal.new_tuple_digest,'0'*64)
            self.assertNotEqual(seal.new_tuple_digest,seal.old_tuple_digest)
            for key,source in originals.items():
                retained=Path(seal.retained_endpoint.record[key])
                self.assertNotEqual(retained,source)
                self.assertEqual(retained.resolve(strict=True),retained)
                self.assertTrue(retained.is_relative_to(self.case.state))
                info=retained.lstat()
                self.assertEqual((info.st_uid,info.st_nlink),(os.getuid(),1))
                self.assertTrue(stat.S_ISREG(info.st_mode))
                self.assertEqual(info.st_mode & 0o077,0)
                self.assertEqual(self.dump(retained),logical[key])
            with self.ro(seal.retained_endpoint.record['db_path']) as db:
                bodies=db.execute('SELECT c.body FROM membership m JOIN content c ON c.hash=m.hash WHERE m.gen=?',
                                  (seal.old_generation,)).fetchall()
                self.assertEqual(len(bodies),3)
                self.assertTrue(any('otter' in body for (body,) in bodies))
            import laya_incremental_docs as incremental
            new_identity=intent['newBuild']['identity']
            self.assertEqual(new_identity['candidateDb'],json.loads(intent['newRoute'])['dbPath'])
            matches=[]
            # Locate staged SQLite by actual schema and metadata, never by
            # a producer-specific directory/file naming convention.
            for path in set(self.case.state.rglob('*'))-files_before:
                if not path.is_file() or path.is_symlink():continue
                with path.open('rb') as stream:header=stream.read(16)
                if header!=b'SQLite format 3\x00':continue
                with self.ro(path) as db:
                    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not {'meta','docs','docs_fts'}<=tables:continue
                    meta=db.execute('SELECT * FROM meta').fetchall()
                    if meta!=[incremental._expected_meta(new_identity)]:continue
                    self.assertEqual(db.execute('SELECT path,hash,title,body,profile_id,chunk_count,scan_id FROM docs ORDER BY path').fetchall(),docs_before)
                    self.assertEqual(db.execute('SELECT path,title,body FROM docs_fts ORDER BY path').fetchall(),fts_before)
                    for row in docs_before:
                        self.assertTrue(incremental._verified(new_identity,row[0],row[3]))
                        self.assertEqual(row[4],new_identity['vectorProfileId'])
                        self.assertGreater(row[5],0)
                    matches.append(str(path))
            print(json.dumps(dict(case='state-new-sidecar-material',candidateDb=new_identity['candidateDb'],
                actualCompletedCopies=matches,rows=len(docs_before))),flush=True)
            self.assertTrue(matches,'a real staged completed sidecar must name the new route/build candidate')
            self.assertEqual(self.live(),before)

    def test_state_staging_rejects_durable_docs_target_outside_the_new_route_build_identity(self):
        stage,_seal_type=self.producer()
        with self.prepared_state() as (mover,intent):
            wrong=self.case.state/'wrong-candidate.sqlite'
            wrong.touch(mode=0o600)
            with closing(sqlite3.connect(self.case.db.as_uri()+'?mode=ro',uri=True)) as source, \
                    closing(sqlite3.connect(wrong)) as destination:
                source.backup(destination)
            bad=copy.deepcopy(intent)
            self.assertEqual(bad['docs'][1],bad['newBuild']['identity']['candidateDb'])
            bad['docs'][1]=str(wrong)
            intent_path=self.case.state/mover.INTENT
            intent_path.write_bytes(mover._canonical(bad))
            before=self.live();wrong_before=wrong.read_bytes()
            validator_error=None
            try:mover._load_intent(self.case.state,self.case.project)
            except (ValueError,OSError,RuntimeError) as exc:validator_error=str(exc)
            if validator_error is not None:
                print(json.dumps(dict(case='state-docs-target-tamper',privateValidatorError=validator_error,
                    producerRefusalNotClaimed=True)),flush=True)
                self.assertEqual(self.live(),before)
                self.assertEqual(wrong.read_bytes(),wrong_before)
                return
            error,seal=None,None
            try:seal=stage(bad,installer=self.installer)
            except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
            print(json.dumps(dict(case='state-docs-target-tamper',wrongCandidate=str(wrong),
                routeCandidate=json.loads(bad['newRoute'])['dbPath'],buildCandidate=bad['newBuild']['identity']['candidateDb'],
                issued=seal is not None,error=error,featureAbsence=error=='reader_move_stage_unsupported',
                liveUnchanged=self.live()==before)),flush=True)
            self.assertEqual(self.live(),before)
            self.assertEqual(wrong.read_bytes(),wrong_before)
            self.assertIsNotNone(error)
            self.assertNotEqual(error,'reader_move_stage_unsupported',
                'feature RED: unsupported state staging does not prove docs identity validation')
            self.assertIsNone(seal)


class _AbsentSidecarFixture:
    PUBLISH_SIDECAR=False
    setUp=MoveSealChecks.setUp
    authority=MoveSealChecks.authority
    producer=MoveSealChecks.producer
    ro=staticmethod(MoveSealChecks.ro)
    dump=MoveSealChecks.dump
    live=MoveSealChecks.live

    def check_absent_stage(self,prepared,kind):
        stage,seal_type=self.producer()
        self.assertFalse(os.path.lexists(self.sidecar))
        with prepared() as prepared_value:
            intent=prepared_value[1] if kind=='state' else prepared_value
            before=self.live();files=set(self.case.state.rglob('*'))
            error,seal=None,None
            with self.lease.reader_lifetime(self.install,exclusive=False):
                try:seal=stage(intent,installer=self.installer)
                except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
            print(json.dumps(dict(case=kind+'-stage-sidecar-absence',error=error,issued=seal is not None,
                explicitAbsence=None if seal is None else (seal.retained_endpoint.record.get('sidecar_path') is None
                    or seal.retained_endpoint.record.get('sidecar_absent') is True),
                sidecarCreated=os.path.lexists(self.sidecar),liveUnchanged=self.live()==before)),flush=True)
            self.assertEqual(self.live(),before)
            self.assertIsNone(error,'a normal sidecar-free v5 install must support tuple staging')
            self.assertIs(type(seal),seal_type)
            with self.assertRaises((ValueError,TypeError)):seal_type()
            with self.assertRaises((AttributeError,TypeError)):seal.move_id='caller'
            self.assertIs(type(seal.retained_endpoint),self.reader.VerifiedEndpoint)
            record=seal.retained_endpoint.record
            self.assertTrue(record.get('sidecar_path') is None or record.get('sidecar_absent') is True,
                'an unchanged nonexistent path alone is not a sealed absence claim')
            for field in ('old_tuple_digest','new_tuple_digest','prefix_digest'):
                self.assertRegex(getattr(seal,field),r'^[0-9a-f]{64}$')
                self.assertNotEqual(getattr(seal,field),'0'*64)
            for key,original in (('catalog_path',self.case.catalog),('binding_path',self.case.store)):
                self.assertNotEqual(Path(record[key]),original)
                self.assertEqual(self.dump(record[key]),self.dump(original))
            with self.ro(record['db_path']) as db:
                rows=db.execute('SELECT c.body FROM membership m JOIN content c ON c.hash=m.hash WHERE m.gen=?',
                                (seal.old_generation,)).fetchall()
                self.assertEqual(len(rows),3)
                self.assertTrue(any('otter' in body for (body,) in rows))
            self.assertFalse(os.path.lexists(self.sidecar))
            for path in set(self.case.state.rglob('*'))-files:
                if not path.is_file() or path.is_symlink():continue
                with path.open('rb') as stream:header=stream.read(16)
                if header!=b'SQLite format 3\x00':continue
                with self.ro(path) as db:
                    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    self.assertFalse({'meta','docs','docs_fts'}<=tables,'absence staging must not create a sidecar copy')
            self.assertEqual(self.live(),before)


class AbsentSidecarStateMoveSealChecks(_AbsentSidecarFixture,unittest.TestCase):
    STATE_PARTS=('.local','state','laya','3'*32)
    prepared_state=StateMoveSealChecks.prepared_state

    def test_state_staging_preserves_explicit_old_and_new_sidecar_absence(self):
        self.check_absent_stage(self.prepared_state,'state')


class AbsentSidecarProjectMoveSealChecks(_AbsentSidecarFixture,unittest.TestCase):
    STATE_PARTS=('.local','state','memvane','3'*32)
    prepared=MoveSealChecks.prepared

    def test_project_staging_preserves_explicit_old_and_new_sidecar_absence(self):
        self.check_absent_stage(self.prepared,'project')


class _FreshStageReplayFixture:
    PUBLISH_SIDECAR=True
    setUp=MoveSealChecks.setUp
    authority=MoveSealChecks.authority
    producer=MoveSealChecks.producer
    ro=staticmethod(MoveSealChecks.ro)
    dump=MoveSealChecks.dump
    live=MoveSealChecks.live

    def replay(self,kind,prepare):
        self.producer()
        # Prepare once in the parent, then release every owner lock. Neither
        # child invokes _prepare or consumes a parent-issued token.
        with prepare() as value:
            intent=value[1] if kind=='state' else value
        intent_bytes=(self.case.state/('state-move-intent.json' if kind=='state' else self.mover.INTENT)).read_bytes()
        before=self.live();existing=set(self.case.state.rglob('*'))
        payload=self.case.home/('fresh-'+kind+'-stage.json')
        payload.write_text(json.dumps(dict(kind=kind,home=str(self.case.home),project=str(self.case.project),
            state=str(self.case.state),plugin=str(self.case.plugin),proof=self.case.proof,
            runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'])),encoding='utf8')
        payload.chmod(0o600)
        argv=[sys.executable,'-B','-I','-c',FRESH_STAGE_CHILD,str(ROOT),str(payload)]
        first=subprocess.Popen([*argv,'hold'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,encoding='utf8',cwd=str(self.case.home),
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        first_result=None
        try:
            if not select.select([first.stdout],[],[],30)[0]:
                raise RuntimeError('first fresh child did not reach staging within its finite deadline')
            line=first.stdout.readline()
            if not line:
                raise RuntimeError('first fresh child exited before a staging result')
            first_result=json.loads(line)
            print(json.dumps(dict(case=kind+'-fresh-stage-first',result=first_result)),flush=True)
            if first_result.get('event')!='staged':
                raise RuntimeError('first-stage setup/factory failure, not replay RED: '+json.dumps(first_result))
            self.assertEqual(first_result['initialIssued'],0)
            self.assertNotEqual(first_result['pid'],os.getpid())
            self.assertIsNone(first.poll(),'owned first child must still hold actual owner scope')
            first.kill()
            self.assertEqual(first.wait(timeout=5),-signal.SIGKILL)
        finally:
            if first.poll() is None:first.kill()
            first.communicate(timeout=5)
        self.assertEqual(self.live(),before)
        tuple_nodes={}
        for path in set(self.case.state.rglob('*'))-existing:
            if not path.is_file() or path.is_symlink():continue
            with path.open('rb') as stream:header=stream.read(16)
            if header==b'SQLite format 3\x00':
                info=path.lstat();tuple_nodes[str(path)]=(info.st_dev,info.st_ino)
        self.assertTrue(tuple_nodes,'first child must leave actual staged SQLite tuples')
        # Independently reacquire the actual mover owners after SIGKILL,
        # without replacing/re-preparing the durable intent.
        with self.authority(),ExitStack() as locks:
            if kind=='state':
                mover=importlib.import_module('laya_state_move')
                mover._locks(locks,self.case.project,self.case.state,home=self.case.home)
            else:
                self.mover._locks(locks,self.case.project,self.case.state,self.mover._queue_path(self.case.project,intent))
        result=subprocess.run([*argv,'finish'],capture_output=True,encoding='utf8',timeout=40,
            cwd=str(self.case.home),env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        lines=result.stdout.strip().splitlines()
        second=json.loads(lines[-1]) if lines else None
        print(json.dumps(dict(case=kind+'-fresh-stage-replay',first=first_result,second=second,
            returncode=result.returncode,stderr=result.stderr[:4096],firstExit=-signal.SIGKILL,
            stagedNodes=tuple_nodes,liveUnchanged=self.live()==before)),flush=True)
        self.assertEqual(self.live(),before)
        self.assertEqual((self.case.state/('state-move-intent.json' if kind=='state' else self.mover.INTENT)).read_bytes(),intent_bytes)
        self.assertEqual(result.returncode,0,'replay RED after actual first-child stage+SIGKILL: '+result.stdout+result.stderr)
        self.assertIsNotNone(second)
        self.assertEqual(second['event'],'staged')
        self.assertEqual(second['initialIssued'],0)
        for field in ('endpoint','moveId','oldDigest','newDigest','prefixDigest','retainedPaths'):
            self.assertEqual(second[field],first_result[field],field)
        for path,node in tuple_nodes.items():
            info=Path(path).lstat()
            self.assertEqual((info.st_dev,info.st_ino),node,'replay must preserve existing staged component inodes')


class ProjectFreshStageReplayChecks(_FreshStageReplayFixture,unittest.TestCase):
    STATE_PARTS=('.local','state','memvane','3'*32)
    prepared=MoveSealChecks.prepared

    def test_project_staging_reissues_after_completed_owned_child_is_killed(self):
        self.replay('project',self.prepared)

    def test_project_staging_retries_after_actual_partial_sqlite_backup_child_kill(self):
        self.partial_backup_retry()

    def test_partial_backup_cleanup_refuses_replaced_symlink_without_touching_its_target(self):
        self.partial_backup_retry(replace_with_symlink=True)

    def test_partial_backup_cleanup_refuses_nonexact_private_mode_without_removing_pending(self):
        self.partial_backup_retry(pending_mode=0o400)

    def partial_backup_retry(self, *, replace_with_symlink=False,pending_mode=None):
        self.producer()
        with self.prepared() as intent:pass
        with self.ro(self.case.catalog) as db:
            source_pages=db.execute('PRAGMA page_count').fetchone()[0]
        self.assertGreater(source_pages,1,'fixture must reach a real multi-page SQLite backup')
        intent_path=self.case.state/self.mover.INTENT
        intent_before=intent_path.read_bytes();before=self.live()
        payload=self.case.home/'partial-backup-stage.json'
        payload.write_text(json.dumps(dict(kind='project',home=str(self.case.home),project=str(self.case.project),
            state=str(self.case.state),plugin=str(self.case.plugin),proof=self.case.proof,
            catalog=str(self.case.catalog),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'])),encoding='utf8')
        payload.chmod(0o600)
        argv=[sys.executable,'-B','-I','-c',FRESH_STAGE_CHILD,str(ROOT),str(payload)]
        child=subprocess.Popen([*argv,'kill-backup'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,encoding='utf8',cwd=str(self.case.home),
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        try:
            if not select.select([child.stdout],[],[],30)[0]:
                raise RuntimeError('partial backup child did not reach actual copy progress')
            line=child.stdout.readline()
            if not line:raise RuntimeError('partial backup child exited before progress evidence')
            checkpoint=json.loads(line)
            print(json.dumps(dict(case='actual-partial-catalog-backup',checkpoint=checkpoint)),flush=True)
            if checkpoint.get('event')!='backup-page-copied':
                raise RuntimeError('factory/setup failure, not partial-backup replay RED: '+json.dumps(checkpoint))
            self.assertEqual(child.wait(timeout=5),-signal.SIGKILL)
        finally:
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)
        self.assertEqual(checkpoint['initialIssued'],0)
        self.assertEqual(checkpoint['source'],str(self.case.catalog))
        self.assertGreater(checkpoint['remaining'],0)
        self.assertGreater(checkpoint['total'],checkpoint['remaining'])
        self.assertEqual(checkpoint['sourcePages'],source_pages)
        partial=Path(checkpoint['destination'])
        self.assertTrue(partial.is_relative_to(self.case.state))
        self.assertNotEqual(partial,self.case.catalog)
        info=partial.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual([info.st_dev,info.st_ino],checkpoint['destinationNode'])
        self.assertEqual((info.st_uid,info.st_nlink),(os.getuid(),1))
        self.assertEqual(info.st_mode & 0o077,0)
        self.assertEqual(self.live(),before)
        self.assertEqual(intent_path.read_bytes(),intent_before)
        sentinel=None
        mode_evidence=None
        if pending_mode is not None:
            original_node=(partial.lstat().st_dev,partial.lstat().st_ino)
            partial.chmod(pending_mode)
            mode_evidence=(original_node,partial.read_bytes(),set(partial.parent.iterdir()))
            def restore_mode():
                if os.path.lexists(partial):
                    current=partial.lstat()
                    if stat.S_ISREG(current.st_mode) and (current.st_dev,current.st_ino)==original_node:
                        partial.chmod(0o600)
            self.addCleanup(restore_mode)
        if replace_with_symlink:
            sentinel=self.case.home/'unowned-copy-target'
            sentinel.write_bytes(b'fixture target must survive unchanged\n')
            sentinel.chmod(0o600)
            partial.unlink()
            partial.symlink_to(sentinel)
            stage_entries=set(partial.parent.iterdir())
        with self.authority(),ExitStack() as locks:
            self.mover._locks(locks,self.case.project,self.case.state,self.mover._queue_path(self.case.project,intent))
        result=subprocess.run([*argv,'finish'],capture_output=True,encoding='utf8',timeout=40,
            cwd=str(self.case.home),env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        lines=result.stdout.strip().splitlines()
        recovered=json.loads(lines[-1]) if lines else None
        print(json.dumps(dict(case='partial-backup-fresh-retry',checkpoint=checkpoint,actualExit=child.returncode,
            recovered=recovered,returncode=result.returncode,stderr=result.stderr[:4096],
            liveUnchanged=self.live()==before,intentPreserved=intent_path.read_bytes()==intent_before)),flush=True)
        self.assertEqual(self.live(),before)
        self.assertEqual(intent_path.read_bytes(),intent_before)
        if pending_mode is not None:
            node,contents,entries=mode_evidence
            preserved=(partial.exists() and (partial.stat().st_dev,partial.stat().st_ino)==node
                       and partial.read_bytes()==contents and stat.S_IMODE(partial.stat().st_mode)==pending_mode)
            print(json.dumps(dict(case='pending-copy-exact-private-mode',requestedMode=oct(pending_mode),
                error=None if recovered is None else recovered.get('reason'),pendingPreserved=preserved,
                stageEntriesPreserved=set(partial.parent.iterdir())==entries)),flush=True)
            with self.subTest(boundary='explicit-mode-refusal'):
                self.assertNotEqual(result.returncode,0)
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered['event'],'error')
                self.assertRegex(recovered['reason'],r'^reader_[a-z0-9_]+$')
            with self.subTest(boundary='pending-inode-and-bytes-preservation'):
                self.assertTrue(preserved,'non-0600 scratch must not be removed or replaced')
                self.assertEqual(set(partial.parent.iterdir()),entries)
            return
        if replace_with_symlink:
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(recovered['event'],'error')
            self.assertEqual(recovered['reason'],'reader_store_path_invalid')
            self.assertTrue(partial.is_symlink())
            self.assertEqual(partial.readlink(),sentinel)
            self.assertEqual(sentinel.read_bytes(),b'fixture target must survive unchanged\n')
            self.assertEqual(set(partial.parent.iterdir()),stage_entries)
            return
        self.assertEqual(result.returncode,0,'replay RED only after actual backup progress and SIGKILL: '+result.stdout+result.stderr)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered['event'],'staged')
        self.assertEqual(recovered['initialIssued'],0)
        self.assertEqual(recovered['sealType'],'MoveReadSeal')
        self.assertEqual(recovered['endpointType'],'VerifiedEndpoint')
        self.assertEqual(recovered['oldGeneration'],self.case.head()[0])
        with self.ro(recovered['retainedPaths']['db_path']) as db:
            rows=db.execute('SELECT c.body FROM membership m JOIN content c ON c.hash=m.hash WHERE m.gen=?',
                            (recovered['oldGeneration'],)).fetchall()
            self.assertEqual(len(rows),3)
            self.assertTrue(any('otter' in body for (body,) in rows))
        for key,path in (('catalog_path',self.case.catalog),('binding_path',self.case.store),('sidecar_path',self.sidecar)):
            self.assertEqual(self.dump(recovered['retainedPaths'][key]),self.dump(path),key)
        self.assertEqual(self.live(),before)


class StateFreshStageReplayChecks(_FreshStageReplayFixture,unittest.TestCase):
    STATE_PARTS=('.local','state','laya','3'*32)
    prepared_state=StateMoveSealChecks.prepared_state

    def test_state_staging_reissues_after_completed_owned_child_is_killed(self):
        self.replay('state',self.prepared_state)


if __name__=='__main__':unittest.main(verbosity=2)
