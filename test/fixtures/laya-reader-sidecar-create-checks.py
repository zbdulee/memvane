#!/usr/bin/env python3
"""Coordinated first sidecar publication over real absent selected v5 seeds."""
from contextlib import contextmanager, ExitStack
import gc
import importlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock
import weakref

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('create_windows_fixture',ROOT/'test/fixtures/laya-reader-windows-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

def emit(case,**values):print(json.dumps(dict(case=case,**values),sort_keys=True),flush=True)
def node(path):
    st=path.lstat();return [st.st_dev,st.st_ino,st.st_mode,st.st_uid,st.st_nlink]
def files(state):
    return {str(path.relative_to(state)):(path.read_bytes(),node(path)) for path in state.rglob('*')
            if path.is_file() and not path.is_symlink()}

CREATE_CHILD=r'''
from contextlib import ExitStack,nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import importlib.util,json,os,signal,sqlite3,sys,weakref
root,payload,mode=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3];p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime'];signal.alarm(35)
spec=importlib.util.spec_from_file_location('create_child_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as R,laya_reader_lease as L,laya_reader_windows as W
import laya_project_dir_move as mover,laya_state_move, laya_sidecar_create as C
import laya_incremental_docs as I,laya_initial_build as build
state,project=Path(p['state']),Path(p['project']);helper=base.JournalReaderChecks('runTest')
case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
assert len(R._issued)==0
closed=set();temp_refs=[];actual_connect=sqlite3.connect;actual_rename=C._rename_noreplace
def path_of(value):
    from urllib.parse import urlparse,unquote
    value=str(value);return Path(unquote(urlparse(value).path)) if value.startswith('file:') else Path(value)
class Observed(sqlite3.Connection):
    def __init__(self,database,*a,**kw):
        super().__init__(database,*a,**kw);self.fixture_path=path_of(database)
        if self.fixture_path.parent==state and self.fixture_path.name.startswith(C.TEMP_PREFIX):
            temp_refs.append((str(self.fixture_path),weakref.ref(self)))
    def close(self):
        super().close()
        if self.fixture_path.parent==state and self.fixture_path.name.startswith(C.TEMP_PREFIX):
            closed.add(str(self.fixture_path))
def connect(*a,**kw):kw['factory']=Observed;return actual_connect(*a,**kw)
def checkpoint(parent_fd,src,dst):
    source=state/src;target=state/dst;info=os.stat(src,dir_fd=parent_fd,follow_symlinks=False)
    fd=os.open(src,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent_fd)
    try:header=os.pread(fd,100,0)
    finally:os.close(fd)
    assert os.path.samestat(os.fstat(parent_fd),state.lstat())
    assert str(source) in closed and header[:16]==b'SQLite format 3\x00' and header[18:20]==b'\x01\x01'
    for filename,reference in temp_refs:
        if filename==str(source) and reference() is not None:
            try:reference().execute('SELECT 1')
            except sqlite3.ProgrammingError:pass
            else:raise RuntimeError('temp connection remains open at native rename boundary')
    assert stat.S_IMODE(info.st_mode)==0o600 and info.st_nlink==1 and info.st_uid==os.getuid()
    evidence=dict(event='create-rename-checkpoint',mode=mode,source=src,destination=dst,
        parentNode=[os.fstat(parent_fd).st_dev,os.fstat(parent_fd).st_ino],
        sourceNode=[info.st_dev,info.st_ino],tempActuallyClosed=True,headerJournalBytes=list(header[18:20]),
        pid=os.getpid(),freshIssuedInstaller=True)
    if mode=='before':
        print(json.dumps(evidence),flush=True);os.kill(os.getpid(),signal.SIGKILL)
    result=actual_rename(parent_fd,src,dst)
    assert not os.path.lexists(source) and [target.stat().st_dev,target.stat().st_ino]==evidence['sourceNode']
    print(json.dumps(evidence),flush=True);os.kill(os.getpid(),signal.SIGKILL)
    return result
import stat
with helper.full_authority(case,R,L,W,mover),mock.patch.object(laya_state_move,'_provider_lane',lambda:nullcontext()):
    installer=R.seal_current_reader(state,project,executing_root=case.plugin)
    with ExitStack() as locks:
        mover._locks(locks,project,state,mover._queue_path(project,None))
        W.prepare_windows(installer);component=W.component_for(installer,'sidecar')
        identity=build.read(state)['identity'];target=state/I.FILE
        if mode in ('before','after'):
            with mock.patch.object(sqlite3,'connect',side_effect=connect),mock.patch.object(C,'_rename_noreplace',new=checkpoint):
                W.create_sidecar_owned(component,identity=identity,deadline=__import__('time').monotonic()+5)
            raise RuntimeError('actual rename checkpoint not reached')
        owner=None;db=None;existed=target.exists()
        try:
            if existed:db=I._open(state,coordination=component,identity=identity)
            else:
                owner=W.create_sidecar_owned(component,identity=identity,deadline=__import__('time').monotonic()+5)
                db=owner.db
            assert db.execute('SELECT * FROM meta').fetchall()==[I._expected_meta(identity)]
            assert db.execute('SELECT count(*) FROM docs').fetchone()==(0,)
            assert db.execute('SELECT count(*) FROM docs_fts').fetchone()==(0,)
            print(json.dumps(dict(event='fresh-create-retry',existingTarget=existed,
                targetNode=[target.stat().st_dev,target.stat().st_ino],metaValid=True,docs=0,fts=0,
                freshIssuedInstaller=True,pid=os.getpid())),flush=True)
        finally:
            if owner is not None:W.close_owned(owner,deadline=__import__('time').monotonic()+1)
            elif db is not None:I._close(db,deadline=__import__('time').monotonic()+1)
        assert W.pending_count()==0
'''

class SidecarCreateChecks(unittest.TestCase):
    def setUp(self):
        self.seed=self.new_seed();self.use_seed(self.seed)
    def new_seed(self):
        seed=existing.seed_module.MoveSealChecks('runTest');seed.PUBLISH_SIDECAR=False
        self.addCleanup(seed.doCleanups);seed.setUp();return seed
    def use_seed(self,seed):
        self.seed,self.case=seed,seed.case
        self.W=importlib.import_module('laya_reader_windows');self.I=importlib.import_module('laya_incremental_docs')
        self.build=importlib.import_module('laya_initial_build')
        self.identity=self.build.read(self.case.state)['identity'];self.target=self.case.state/self.I.FILE
    @contextmanager
    def owner(self):
        with self.seed.authority(),ExitStack() as locks:
            self.seed.mover._locks(locks,self.case.project,self.case.state,
                self.seed.mover._queue_path(self.case.project,None))
            yield
    def api(self):
        self.seed.reader._recheck(self.seed.installer.endpoint);self.W.prepare_windows(self.seed.installer)
        component=self.W.component_for(self.seed.installer,'sidecar')
        for name in ('create_sidecar_owned','publish_window'):
            if not callable(getattr(self.W,name,None)):
                emit('sidecar-create-feature-absence',actualV5=True,actualOwner=True,sidecarAbsent=not self.target.exists(),missing=name)
                self.fail('T3 first-create feature RED after actual absent v5/full installer/owner: '+name)
        if importlib.util.find_spec('laya_sidecar_create') is None:self.fail('T3 first-create feature RED: laya_sidecar_create absent')
        self.C=importlib.import_module('laya_sidecar_create')
        self.assertEqual(self.C.TEMP_PREFIX,'.reader-sidecar-new-')
        self.assertTrue(callable(self.C._rename_noreplace));return component
    def trace(self,rows):
        actual=sqlite3.connect
        def connect(database,*a,**kw):
            path=existing.database_path(database)
            rows.append(dict(path=str(path),raw=str(database)))
            return actual(database,*a,**kw)
        return connect
    def content(self,rows):
        return [row for row in rows if Path(row['path']).is_absolute()
                and self.case.state in Path(row['path']).parents and Path(row['path'])!=self.case.journal]
    def close_owner(self,owner):
        if owner is not None:self.W.close_owned(owner,deadline=time.monotonic()+1)
        self.W.drain_pending(deadline=time.monotonic()+1)

    def test_published_inode_replacement_refuses_before_replacement_target_sqlite_open(self):
        with self.owner():
            component=self.api();replacement=self.case.state/'replacement-sidecar.sqlite'
            with sqlite3.connect(replacement,isolation_level=None) as db:
                self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchone(),('delete',))
                self.I._setup(db,self.identity)
            db.close();replacement.chmod(0o600)
            replacement_before=(replacement.read_bytes(),node(replacement))
            original=self.W.component_for;swaps=[];rows=[];created=None;error=None;invalid=False
            def replace_at_reissue(installer,role):
                if role=='sidecar' and self.target.exists() and not swaps:
                    published=node(self.target)
                    os.replace(replacement,self.target)
                    swaps.append(dict(publishedNode=published,replacementNode=node(self.target)))
                return original(installer,role)
            try:
                with mock.patch.object(self.W,'component_for',new=replace_at_reissue), \
                        mock.patch.object(sqlite3,'connect',side_effect=self.trace(rows)):
                    try:created=self.W.create_sidecar_owned(component,identity=self.identity,deadline=time.monotonic()+3)
                    except Exception as exc:error=type(exc).__name__+':'+str(exc);invalid=isinstance(exc,ValueError);exc.__traceback__=None
                if len(swaps)!=1:raise RuntimeError('actual native publication/final component reissue seam was not reached')
                after=existing.ReaderWindowsChecks.files(self.target)
                target_sql=[row for row in rows if row['path']==str(self.target)]
                preserved=(self.target.read_bytes(),node(self.target))==replacement_before
                aux_absent=all(after[suffix] is None for suffix in ('-wal','-shm','-journal'))
                emit('published-inode-reissue-replacement',error=error,valueError=invalid,returned=created is not None,
                    swaps=swaps,targetSQLiteConnects=target_sql,replacementPreserved=preserved,auxAbsent=aux_absent)
                with self.subTest(boundary='published-node-must-bind-final-issued-component'):
                    self.assertTrue(invalid);self.assertIsNone(created)
                with self.subTest(boundary='replacement-first-sqlite-open-zero'):self.assertEqual(target_sql,[])
                with self.subTest(boundary='replacement-bytes-node-mode-owner-nlink-aux-preserved'):
                    self.assertTrue(preserved);self.assertTrue(aux_absent)
            finally:self.close_owner(created)

    def test_expired_deadline_after_actual_verify_never_enters_native_publication(self):
        with self.owner():
            component=self.api();actual_verify=self.C._verify;actual_rename=self.C._rename_noreplace
            verified=[];renamed=[];created=None;error=None;invalid=False;deadline=time.monotonic()+1.0
            def slow_verify(*args,**kwargs):
                digest=actual_verify(*args,**kwargs)
                verified.append(dict(actualVerificationCompleted=True,completedAt=time.monotonic()))
                time.sleep(max(0,deadline-time.monotonic())+0.03)
                return digest
            def rename(*args,**kwargs):
                renamed.append(dict(calledAt=time.monotonic(),deadline=deadline))
                return actual_rename(*args,**kwargs)
            try:
                with mock.patch.object(self.C,'_verify',new=slow_verify), \
                        mock.patch.object(self.C,'_rename_noreplace',new=rename):
                    try:created=self.W.create_sidecar_owned(component,identity=self.identity,deadline=deadline)
                    except Exception as exc:error=type(exc).__name__+':'+str(exc);invalid=isinstance(exc,ValueError);exc.__traceback__=None
                if not verified:raise RuntimeError('actual verifier did not complete before deadline-overrun observation')
                emit('actual-verify-deadline-overrun',verification=verified,deadline=deadline,now=time.monotonic(),
                    error=error,valueError=invalid,nativeRenames=renamed,targetPresent=self.target.exists(),
                    returned=created is not None,pending=self.W.pending_count())
                with self.subTest(boundary='expired-create-refused'):
                    self.assertTrue(invalid);self.assertIsNone(created)
                with self.subTest(boundary='no-native-publication-after-deadline'):
                    self.assertEqual(renamed,[]);self.assertFalse(self.target.exists())
                self.assertEqual(self.W.pending_count(),0)
            finally:self.close_owner(created)

    def test_verify_sql_error_then_one_close_failure_rolls_back_before_same_thread_drain(self):
        with self.owner():
            component=self.api();actual=sqlite3.connect;state=self.case.state;prefix=self.C.TEMP_PREFIX
            windows=self.W;expected_meta=self.I._expected_meta(self.identity)
            observed={'integrityReached':False,'closeAttempts':0,'rwSetupComplete':False};refs=[];events=[]
            def ex_held():
                try:
                    with windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.03):return False
                except windows.ReaderWindowBusy:return True
            class VerificationConnection(sqlite3.Connection):
                def __init__(db,database,*args,**kwargs):
                    super().__init__(database,*args,**kwargs)
                    path=existing.database_path(database)
                    db.fixture_temp=path.parent==state and path.name.startswith(prefix)
                    db.fixture_ro=db.fixture_temp and 'mode=ro' in str(database)
                    if db.fixture_ro:refs.append(weakref.ref(db))
                def execute(db,sql,*args,**kwargs):
                    if (db.fixture_ro and not observed['integrityReached']
                            and sql.strip().upper()=='PRAGMA INTEGRITY_CHECK'):
                        if not db.in_transaction:raise RuntimeError('actual verification BEGIN prerequisite missing')
                        observed['integrityReached']=True
                        return super().execute('SELECT FROM deliberately_invalid_verification_sql')
                    return super().execute(sql,*args,**kwargs)
                def close(db):
                    if db.fixture_temp and not db.fixture_ro:
                        observed['rwSetupComplete']=(super().execute('SELECT * FROM meta').fetchall()==[expected_meta])
                    if db.fixture_ro:
                        observed['closeAttempts']+=1
                        held=ex_held()
                        if observed['closeAttempts']==1:
                            observed['firstCloseInTransaction']=db.in_transaction
                            observed['firstClosePhysicalEX']=held
                            raise sqlite3.OperationalError('synthetic_one_time_verification_ro_close_failure')
                        super().close();events.append(dict(event='actual-ro-close',physicalExclusive=held));return
                    super().close()
            def connect(*args,**kwargs):kwargs['factory']=VerificationConnection;return actual(*args,**kwargs)
            created=None;error=None;chain=[];drain_error=None;reference=None
            try:
                with mock.patch.object(sqlite3,'connect',side_effect=connect):
                    try:created=self.W.create_sidecar_owned(component,identity=self.identity,deadline=time.monotonic()+3)
                    except Exception as exc:
                        error=type(exc).__name__+':'+str(exc)
                        pending=[exc];seen=set()
                        while pending:
                            item=pending.pop()
                            if id(item) in seen:continue
                            seen.add(id(item));chain.append(dict(type=type(item).__name__,message=str(item)[:300]))
                            pending.extend(value for value in (item.__cause__,item.__context__) if value is not None)
                            item.__traceback__=None
                        del pending,item
                if refs:reference=refs[0]
                if not observed['integrityReached'] or not observed['rwSetupComplete'] or len(refs)!=1:
                    raise RuntimeError('actual initialized-temp/BEGIN/integrity SQL/RO connection prerequisite missing')
                gc.collect()
                pending_before=self.W.pending_count();alive=existing.ReaderWindowsChecks.live(reference)
                try:self.W.drain_pending(deadline=time.monotonic()+0.5)
                except (ValueError,OSError,RuntimeError) as exc:drain_error=type(exc).__name__+':'+str(exc);exc.__traceback__=None
                emit('verify-failure-close-retry-before-fallback',error=error,exceptionChain=chain,observation=observed,
                    pendingAfterGc=pending_before,actualROConnectionAlive=alive,drainError=drain_error,
                    pendingAfterDrain=self.W.pending_count(),actualCloseEvents=list(events),targetPresent=self.target.exists())
                with self.subTest(boundary='failed-close-retains-real-ro-owner'):
                    self.assertIsNone(created);self.assertIsNotNone(error);self.assertEqual(pending_before,1);self.assertTrue(alive)
                    self.assertTrue(observed['firstClosePhysicalEX'])
                with self.subTest(boundary='validation-rollback-precedes-first-close'):
                    self.assertFalse(observed['firstCloseInTransaction'])
                with self.subTest(boundary='same-thread-drain-retries-actual-close'):
                    self.assertIsNone(drain_error);self.assertEqual(self.W.pending_count(),0)
                    self.assertEqual(observed['closeAttempts'],2);self.assertEqual(len(events),1)
                    self.assertTrue(events[0]['physicalExclusive'])
                    existing.ReaderWindowsChecks.assert_closed(self,reference)
                    with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
                self.assertFalse(self.target.exists())
            finally:
                # Only after the original metrics/assertions: a real rollback
                # and same-thread drain prevent the unsafe BASE from leaking.
                rollback=False
                if reference is not None and reference() is not None:
                    try:
                        if reference().in_transaction:reference().rollback();rollback=True
                    except sqlite3.ProgrammingError:pass
                self.close_owner(created)
                emit('verification-close-finally-cleanup',actualFallbackRollback=rollback,pending=self.W.pending_count())

    def test_admission_locks_block_creation_before_first_sqlite_or_new_temp(self):
        with self.owner():
            component=self.api()
            for kind in ('mutation','physical','publish-cut'):
                with self.subTest(blocker=kind):
                    blocker=(self.W.mutation_window(component,exclusive=True,deadline=time.monotonic()+0.2)
                        if kind=='mutation' else self.W.physical_window(component,exclusive=False,deadline=time.monotonic()+0.2)
                        if kind=='physical' else self.W.publish_window(component,exclusive=False,deadline=time.monotonic()+0.2))
                    rows=[];created=None;busy=False;error=None
                    with blocker:
                        before=files(self.case.state)
                        try:
                            with mock.patch.object(sqlite3,'connect',side_effect=self.trace(rows)):
                                created=self.W.create_sidecar_owned(component,identity=self.identity,deadline=time.monotonic()+0.05)
                        except self.W.ReaderWindowBusy as exc:busy=True;error=str(exc)
                        after=files(self.case.state)
                    self.close_owner(created)
                    emit('create-admission',blocker=kind,error=error,contentConnects=self.content(rows),
                        unchanged=after==before,targetCreated=self.target.exists(),pending=self.W.pending_count())
                    self.assertTrue(busy);self.assertIsNone(created);self.assertEqual(self.content(rows),[])
                    self.assertTrue(after==before,'busy creator must not create target/temp or mutate content files')
                    self.assertFalse(self.target.exists());self.assertEqual(self.W.pending_count(),0)
    def test_actual_create_reissues_component_and_commits_first_real_cached_document(self):
        created=None;runtime=None
        with self.owner():
            original=self.api()
            try:
                created=self.W.create_sidecar_owned(original,identity=self.identity,deadline=time.monotonic()+3)
                db=created.db;component=created.component
                self.assertIsInstance(db,sqlite3.Connection);self.assertIsNot(component,original)
                self.assertEqual(component.path,self.target)
                self.assertEqual(db.execute('SELECT * FROM meta').fetchall(),[self.I._expected_meta(self.identity)])
                self.assertEqual(db.execute('SELECT count(*) FROM docs').fetchone(),(0,))
                self.assertEqual(db.execute('SELECT count(*) FROM docs_fts').fetchone(),(0,))
                rows=[];old_error=None;old_db=None
                with mock.patch.object(sqlite3,'connect',side_effect=self.trace(rows)):
                    try:old_db=self.I._open(self.case.state,coordination=original,identity=self.identity)
                    except ValueError as exc:old_error=str(exc)
                if old_db is not None:self.I._close(old_db,deadline=time.monotonic()+1)
                with self.subTest(boundary='original-absence-cap-not-promoted'):
                    self.assertEqual(old_error,'reader_component_absent');self.assertIsNone(old_db)
                    self.assertEqual(self.content(rows),[])
                from laya_index_runtime import LayaIndexRuntime
                from laya_index_vector import LayaVectorIndex,qmd_markdown_title
                components={role:self.W.component_for(self.seed.installer,role) for role in ('base','queue','catalog','binding')}
                roots={self.case.WIKI:str(self.case.auto/'wiki')}
                runtime=LayaIndexRuntime(source_root=roots,scope=self.case.route['scopeId'],db_path=str(self.case.db),
                    queue_path=str(self.case.queue),db_incarnation=self.case.route['dbIncarnation'],
                    catalog_path=str(self.case.catalog),binding_store_path=str(self.case.store),wiki_only=True,
                    connection_windows=components)
                head=runtime.index.db.execute('SELECT gen FROM head WHERE scope=?',(self.identity['scopeId'],)).fetchone()[0]
                row=runtime.index.db.execute('SELECT m.path,m.hash,c.body FROM membership m JOIN content c ON c.hash=m.hash '
                    "WHERE m.gen=? AND c.body LIKE '%otter%' ORDER BY m.path LIMIT 1",(head,)).fetchone()
                if row is None:raise RuntimeError('actual cached otter source prerequisite missing')
                path,hashed,body=row;title=qmd_markdown_title(body,path)
                cached=LayaVectorIndex(runtime.index).cached_chunk_set(hashed,title,self.identity['vectorProfileId'])
                self.assertTrue(cached)
                result=self.I.publish_document(runtime,self.identity,runtime.catalog.require_complete(),
                    path,hashed,title,body,self.identity['vectorProfileId'],require_verified=False,coordination=component)
                self.assertEqual(result['status'],'published')
                self.assertEqual(db.execute('SELECT hash,profile_id,chunk_count FROM docs WHERE path=?',(path,)).fetchone(),
                    (hashed,self.identity['vectorProfileId'],len(cached)))
                fts=db.execute("SELECT count(*) FROM docs_fts WHERE docs_fts MATCH 'otter'").fetchone()[0]
                emit('actual-first-sidecar-document',newComponent=True,originalAbsentCapError=old_error,
                    actualHead=head,actualCachedChunks=len(cached),publish=result,ftsRows=fts,
                    creationOwnerRemainedOpen=True,coldReaderProved=False)
                self.assertGreater(fts,0)
            finally:
                if runtime is not None:runtime.close(deadline=time.monotonic()+1)
                self.close_owner(created)
            self.assertEqual(self.W.pending_count(),0)
    def test_actual_native_noreplace_collision_preserves_foreign_target_and_unrelated_temp(self):
        created=None;foreign_node=None;target_before=None
        with self.owner():
            component=self.api();unrelated=self.case.state/(self.C.TEMP_PREFIX+'unrelated-fixture')
            unrelated.write_bytes(b'unrelated private fixture');unrelated.chmod(0o600)
            unrelated_before=(unrelated.read_bytes(),node(unrelated));rows=[];calls=[];original=self.C._rename_noreplace
            def collide(parent_fd,src,dst):
                nonlocal foreign_node,target_before
                fd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent_fd)
                try:os.write(fd,b'foreign fixture target');os.fsync(fd)
                finally:os.close(fd)
                foreign_node=node(self.target);target_before=(self.target.read_bytes(),foreign_node)
                calls.append(dict(source=src,destination=dst,parent=[os.fstat(parent_fd).st_dev,os.fstat(parent_fd).st_ino]))
                return original(parent_fd,src,dst)
            error=None
            try:
                with mock.patch.object(self.C,'_rename_noreplace',new=collide),mock.patch.object(sqlite3,'connect',side_effect=self.trace(rows)):
                    try:created=self.W.create_sidecar_owned(component,identity=self.identity,deadline=time.monotonic()+3)
                    except (OSError,ValueError,RuntimeError) as exc:error=type(exc).__name__+':'+str(exc)
                if not calls:raise RuntimeError('actual native no-replace collision seam not reached')
                target_same=self.target.exists() and (self.target.read_bytes(),node(self.target))==target_before
                target_sql=[row for row in rows if row['path']==str(self.target)]
                emit('native-noreplace-collision',error=error,nativeCalls=calls,targetPreserved=target_same,
                    targetSQLiteConnects=target_sql,unrelatedPreserved=(unrelated.read_bytes(),node(unrelated))==unrelated_before)
                self.assertIsNotNone(error);self.assertIsNone(created);self.assertTrue(target_same)
                self.assertEqual(target_sql,[]);self.assertEqual((unrelated.read_bytes(),node(unrelated)),unrelated_before)
            finally:
                self.close_owner(created)
                if foreign_node is not None and self.target.exists() and node(self.target)==foreign_node:self.target.unlink()
            self.assertEqual(self.W.pending_count(),0)
    def test_fixed_initializer_actual_sql_failure_closes_temp_under_ex_without_publication(self):
        with self.owner():
            component=self.api();unrelated=self.case.state/(self.C.TEMP_PREFIX+'unrelated-fixture')
            unrelated.write_bytes(b'preserved unrelated fixture');unrelated.chmod(0o600)
            before=(unrelated.read_bytes(),node(unrelated));references=[];events=[];injected=[]
            actual_connect=sqlite3.connect;actual_setup=self.I._setup;windows=self.W;state=self.case.state;prefix=self.C.TEMP_PREFIX
            def ex_held():
                try:
                    with windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.03):return False
                except windows.ReaderWindowBusy:return True
            class TempConnection(sqlite3.Connection):
                def __init__(db,database,*a,**kw):
                    super().__init__(database,*a,**kw);path=existing.database_path(database)
                    db.fixture_temp=path.parent==state and path.name.startswith(prefix)
                    if db.fixture_temp:references.append(weakref.ref(db))
                def close(db):
                    held=ex_held() if db.fixture_temp else None
                    super().close()
                    if db.fixture_temp:events.append(dict(event='actual-temp-close',physicalExclusive=held))
            def connect(*a,**kw):kw['factory']=TempConnection;return actual_connect(*a,**kw)
            def broken(db,identity):
                actual_setup(db,identity)
                if not getattr(db,'fixture_temp',False):raise RuntimeError('fixed initializer did not use its actual private temp')
                injected.append(True);db.execute('CREATE TABLE broken(')
            created=None;error=None
            try:
                with mock.patch.object(sqlite3,'connect',side_effect=connect),mock.patch.object(self.I,'_setup',new=broken):
                    try:created=self.W.create_sidecar_owned(component,identity=self.identity,deadline=time.monotonic()+3)
                    except (sqlite3.Error,OSError,ValueError,RuntimeError) as exc:error=type(exc).__name__+':'+str(exc);exc.__traceback__=None
                emit('fixed-initializer-sql-failure',error=error,injected=len(injected),tempCloseEvents=events,
                    targetCreated=self.target.exists(),pending=self.W.pending_count())
                self.assertEqual(injected,[True]);self.assertIsNotNone(error);self.assertIsNone(created)
                self.assertTrue(events);self.assertTrue(all(event['physicalExclusive'] for event in events))
                for reference in references:existing.ReaderWindowsChecks.assert_closed(self,reference)
                self.assertFalse(self.target.exists());self.assertEqual((unrelated.read_bytes(),node(unrelated)),before)
                self.assertEqual(self.W.pending_count(),0)
            finally:self.close_owner(created)
    def test_fresh_sigkill_before_and_after_native_rename_retries_without_adopting_dead_temp(self):
        for index,phase in enumerate(('before','after')):
            with self.subTest(phase=phase):
                if index:self.use_seed(self.new_seed())
                with self.owner():self.api()
                payload=self.case.home/'create-crash.json'
                payload.write_text(json.dumps(dict(home=str(self.case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
                    state=str(self.case.state),project=str(self.case.project),plugin=str(self.case.plugin),proof=self.case.proof)),encoding='utf8')
                payload.chmod(0o600)
                result=subprocess.run([sys.executable,'-B','-I','-c',CREATE_CHILD,str(ROOT),str(payload),phase],
                    capture_output=True,encoding='utf8',timeout=40)
                records=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                if result.returncode!=-signal.SIGKILL or len(records)!=1 or records[0].get('event')!='create-rename-checkpoint':
                    raise RuntimeError('fresh native rename prerequisite failed: '+json.dumps(dict(returncode=result.returncode,
                        records=records,stderr=result.stderr[-4000:])))
                checkpoint=records[0];temp=self.case.state/checkpoint['source']
                self.assertEqual(checkpoint['parentNode'],node(self.case.state)[:2])
                self.assertEqual(checkpoint['headerJournalBytes'],[1,1]);self.assertTrue(checkpoint['tempActuallyClosed'])
                if phase=='before':
                    if self.target.exists() or not temp.is_file():raise RuntimeError('before-rename actual path prerequisite missing')
                    witness=(temp.read_bytes(),node(temp))
                else:
                    if temp.exists() or not self.target.is_file():raise RuntimeError('after-rename actual path prerequisite missing')
                    info=self.target.lstat();witness=node(self.target)
                    self.assertEqual(witness[:2],checkpoint['sourceNode'])
                    self.assertEqual(stat.S_IMODE(info.st_mode),0o600);self.assertEqual(info.st_uid,os.getuid());self.assertEqual(info.st_nlink,1)
                    self.assertEqual(self.target.read_bytes()[18:20],b'\x01\x01')
                    for suffix in ('-journal','-wal','-shm'):self.assertFalse(os.path.lexists(str(self.target)+suffix))
                retry=subprocess.run([sys.executable,'-B','-I','-c',CREATE_CHILD,str(ROOT),str(payload),'retry'],
                    capture_output=True,encoding='utf8',timeout=40)
                observed=[json.loads(line) for line in retry.stdout.splitlines() if line.strip()]
                emit('fresh-create-crash-retry',phase=phase,actualKill=result.returncode,checkpoint=checkpoint,
                    retryReturncode=retry.returncode,retry=observed,stderr=retry.stderr[-4000:],actualHostExecuted=False)
                self.assertEqual(retry.returncode,0,'fresh issuer must resume actual before/after-rename boundary')
                self.assertEqual(len(observed),1);self.assertEqual(observed[0]['event'],'fresh-create-retry')
                self.assertEqual(observed[0]['existingTarget'],phase=='after')
                if phase=='before':self.assertTrue((temp.read_bytes(),node(temp))==witness,'dead issuer temp is not reusable cleanup authority')
                else:self.assertEqual(node(self.target),witness,'retry existing target keeps the actual published inode')

if __name__=='__main__':unittest.main(verbosity=2)
