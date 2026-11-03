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
    STATE_PARTS=('.local','state','memvane','3'*32)
    PUBLISH_SIDECAR=True

    def setUp(self):
        self.helper=base.JournalReaderChecks('runTest')
        self.helper.setUp();self.addCleanup(self.helper.doCleanups)
        class PublishedInstall(base.published.MoveCase):
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


if __name__=='__main__':unittest.main(verbosity=2)
