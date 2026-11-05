#!/usr/bin/env python3
"""Base physical open/close contracts; real SQLite and weak observations only."""
from contextlib import ExitStack, closing
from contextvars import copy_context
import fcntl
import gc
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import threading
import unittest
from unittest import mock
from urllib.parse import unquote,urlparse
import weakref

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('windows_real_seed',ROOT/'test/fixtures/laya-reader-move-seal-checks.py')
seed_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(seed_module)

MANIFEST_LINK_CHILD=r'''
from contextlib import ExitStack,nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import importlib.util,json,os,signal,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime'];signal.alarm(30)
spec=importlib.util.spec_from_file_location('manifest_link_seed',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as R,laya_reader_lease as L,laya_reader_windows as W
import laya_project_dir_move as mover,laya_state_move
helper=base.JournalReaderChecks('runTest');case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
project,state=Path(p['project']),Path(p['state']);actual_link=os.link
assert len(R._issued)==0
def linked(src,dst,*args,**kwargs):
    result=actual_link(src,dst,*args,**kwargs)
    if Path(dst).name=='component-windows-v1.json':
        info=os.stat(dst,dir_fd=kwargs.get('dst_dir_fd'),follow_symlinks=False)
        print(json.dumps(dict(event='manifest-linked-before-temp-unlink',pid=os.getpid(),
            source=str(src),destination=str(dst),node=[info.st_dev,info.st_ino],nlink=info.st_nlink,
            freshIssuedInstaller=True,actualOwner=True)),flush=True)
        os.kill(os.getpid(),signal.SIGKILL)
    return result
with helper.full_authority(case,R,L,W,mover),mock.patch.object(laya_state_move,'_provider_lane',lambda:nullcontext()):
    installer=R.seal_current_reader(state,project,executing_root=case.plugin)
    with ExitStack() as locks:
        mover._locks(locks,project,state,mover._queue_path(project,None))
        with mock.patch.object(os,'link',side_effect=linked):W.prepare_windows(installer)
raise RuntimeError('actual manifest os.link checkpoint was not reached')
'''

def emit(case,**values):print(json.dumps(dict(case=case,**values),sort_keys=True),flush=True)

def database_path(raw):
    raw=str(raw)
    return Path(unquote(urlparse(raw).path)) if raw.startswith('file:') else Path(raw)

class QueueConstructorFailure(RuntimeError):pass

class ConnectionObservation:
    """Observes actual Connection calls; never retains a connection strongly."""
    def __init__(self,path,windows,component,*,fail_first_pragma=False):
        self.path=Path(path);self.windows=windows;self.component=component
        self.refs=[];self.events=[];self.fail_first_pragma=fail_first_pragma;self.injected=False
        self.actual_connect=sqlite3.connect
        owner=self
        class ObservedConnection(sqlite3.Connection):
            def __init__(self,database,*args,**kwargs):
                super().__init__(database,*args,**kwargs)
                self.is_base_rw=(database_path(database)==owner.path and 'mode=rw' in str(database))
                if self.is_base_rw:owner.refs.append(weakref.ref(self))
            def execute(self,sql,*args,**kwargs):
                if (self.is_base_rw and owner.fail_first_pragma and not owner.injected
                        and sql.strip().upper().startswith('PRAGMA')):
                    owner.injected=True
                    owner.events.append(dict(event='first-pragma-failure',physicalExclusive=owner.exclusive_held()))
                    raise sqlite3.OperationalError('synthetic_first_pragma_failure')
                return super().execute(sql,*args,**kwargs)
            def _observed_close(self,finish):
                held=owner.exclusive_held() if self.is_base_rw else None
                finish()
                if self.is_base_rw:owner.events.append(dict(event='actual-db-close',physicalExclusive=held))
            def close(self):
                if isinstance(self,getattr(owner.windows,'_OwnedSQLite',())):
                    return super().close()
                return self._observed_close(super().close)
            def _native_close_locked(self):
                return self._observed_close(super()._native_close_locked)
        self.factory=ObservedConnection
    def connect(self,*args,**kwargs):
        supplied=kwargs.get('factory',sqlite3.Connection)
        kwargs['factory']=(self.factory if supplied is sqlite3.Connection else
            type('ObservedManagedConnection',(self.factory,supplied),{}))
        return self.actual_connect(*args,**kwargs)
    def exclusive_held(self):
        # Valid component controls before/after construction distinguish the
        # competing SH refusal here from a malformed component/path rejection.
        try:
            with self.windows.physical_window(self.component,exclusive=False,deadline=time.monotonic()+0.03):
                return False
        except Exception as exc:
            text=(type(exc).__name__+':'+str(exc)).lower()
            if 'busy' in text or 'timeout' in text or 'deadline' in text:return True
            raise
    def latest(self):
        if not self.refs:raise RuntimeError('actual base RW connection observation missing')
        return self.refs[-1]

class ReaderWindowsChecks(unittest.TestCase):
    def setUp(self):
        self.seed=seed_module.MoveSealChecks('runTest')
        self.addCleanup(self.seed.doCleanups)
        if self._testMethodName == 'test_manifest_link_sigkill_retry_recovers_exact_temp_alias_without_replacing_lock_inodes':
            # Reproduce a legacy v5 fixture without its later window manifest
            # so the real first-link crash/recovery protocol remains exercised.
            windows=importlib.import_module('laya_reader_windows')
            with mock.patch.object(windows,'_prepare_conversion_windows'):
                self.seed.setUp()
        else:
            self.seed.setUp()
        self.case=self.seed.case
    def api(self):
        if importlib.util.find_spec('laya_reader_windows') is None:
            emit('windows-feature-absence',actualV5=True,actualPublishedCards=3,actualPreparedOwner=True,
                 required='laya_reader_windows',classification='new-T3-feature-not-implemented')
            self.fail('T3 feature RED after real v5/owner proof: laya_reader_windows is absent')
        windows=importlib.import_module('laya_reader_windows')
        for name in ('prepare_windows','component_for','physical_window','pending_count','drain_pending'):
            self.assertTrue(callable(getattr(windows,name,None)),name+' API required')
        self.assertTrue(isinstance(getattr(windows,'ReaderCloseDeferred',None),type))
        windows.prepare_windows(self.seed.installer)
        component=windows.component_for(self.seed.installer,'base')
        self.assertEqual(type(component).__module__,windows.__name__)
        self.assertEqual(windows.pending_count(),0)
        return windows,component
    @staticmethod
    def live(reference):
        if reference() is None:return False
        try:return reference().execute('SELECT 1').fetchone()==(1,)
        except sqlite3.ProgrammingError:return False
    def assert_closed(self,reference):
        if reference() is not None:
            with self.assertRaises(sqlite3.ProgrammingError):reference().execute('SELECT 1')
    @staticmethod
    def drain(windows):
        return windows.drain_pending(deadline=time.monotonic()+1.0)

    @staticmethod
    def files(path):
        return {suffix:None if not os.path.lexists(str(path)+suffix) else
            (Path(str(path)+suffix).read_bytes(),tuple(getattr(Path(str(path)+suffix).lstat(),key)
                for key in ('st_dev','st_ino','st_mode','st_uid','st_nlink')))
            for suffix in ('','-wal','-shm','-journal')}

    def test_completed_manifest_never_deletes_an_unrecorded_temp_shaped_hardlink(self):
        with self.seed.prepared():
            windows, _component = self.api()
            parent = self.seed.reader._lock_dir(self.seed.install, create=False)
            manifest = parent / 'component-windows-v1.json'
            unrelated = parent / ('.component-windows-' + 'f' * 32)
            if os.path.lexists(unrelated):raise RuntimeError('fixture hardlink name already exists')
            original = manifest.lstat()
            os.link(manifest, unrelated)
            error = None
            try:
                try:windows.prepare_windows(self.seed.installer)
                except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
                kept = os.path.lexists(unrelated)
                emit('unrecorded-temp-shaped-hardlink',error=error,unrelatedNamePreserved=kept,
                     manifestSameInode=os.path.samestat(original,manifest.lstat()),
                     nlink=manifest.lstat().st_nlink)
                with self.subTest(boundary='unknown-alias-refused'):self.assertIsNotNone(error)
                with self.subTest(boundary='unknown-alias-not-deleted'):self.assertTrue(kept)
                self.assertTrue(os.path.samestat(original,manifest.lstat()))
            finally:
                if os.path.lexists(unrelated):
                    if not os.path.samestat(original,unrelated.lstat()):
                        raise RuntimeError('fixture hardlink identity changed')
                    unrelated.unlink()

    def test_manifest_link_sigkill_retry_recovers_exact_temp_alias_without_replacing_lock_inodes(self):
        windows=importlib.import_module('laya_reader_windows')
        parent=self.seed.reader._lock_dir(self.seed.install,create=False)
        manifest=parent/'component-windows-v1.json'
        if os.path.lexists(manifest):raise RuntimeError('first prepare prerequisite requires a fresh unprepared registry')
        payload=self.case.home/'window-manifest-crash.json'
        payload.write_text(json.dumps(dict(home=str(self.case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
            project=str(self.case.project),state=str(self.case.state),plugin=str(self.case.plugin),proof=self.case.proof)),encoding='utf8')
        payload.chmod(0o600)
        child=subprocess.Popen([sys.executable,'-B','-I','-c',MANIFEST_LINK_CHILD,str(ROOT),str(payload)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
        try:
            if not select.select([child.stdout],[],[],35)[0]:raise RuntimeError('actual link checkpoint deadline')
            line=child.stdout.readline()
            if not line:
                _,stderr=child.communicate(timeout=5)
                raise RuntimeError('fresh manifest issuer failed before os.link checkpoint: '+stderr[-4000:])
            checkpoint=json.loads(line)
            if checkpoint.get('event')!='manifest-linked-before-temp-unlink' or child.wait(timeout=5)!=-signal.SIGKILL:
                raise RuntimeError('actual successful-link/SIGKILL prerequisite missing')
        finally:
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)
        info=manifest.lstat();raw=manifest.read_bytes();document=json.loads(raw)
        aliases=[path for path in parent.iterdir() if os.path.samestat(path.lstat(),info)]
        temps=[path for path in aliases if path!=manifest]
        required={'publish-cut.lock','reader-lifetime.lock'}|{
            role+'-'+kind+'.lock' for role in ('base','binding','catalog','queue','sidecar')
            for kind in ('mutation','physical')}
        if (info.st_nlink!=2 or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600
                or len(temps)!=1 or not temps[0].name.startswith('.component-windows-')
                or temps[0].name!=Path(checkpoint['source']).name
                or document.get('install_id')!=self.seed.install or document.get('version')!=1
                or raw!=self.seed.reader._raw(document) or set(document['locks'])!=required):
            raise RuntimeError('actual linked canonical manifest/private unique-alias/twelve-lock prerequisite missing')
        locks={name:(parent/name).lstat() for name in document['locks']}
        if any([node.st_dev,node.st_ino]!=document['locks'][name] or node.st_uid!=os.getuid()
               or not stat.S_ISREG(node.st_mode) or stat.S_IMODE(node.st_mode)!=0o600 or node.st_nlink!=1
               for name,node in locks.items()):
            raise RuntimeError('actual manifest twelve lock nodes differ before retry')
        error=None
        with self.seed.prepared():
            try:windows.prepare_windows(self.seed.installer)
            except (ValueError,OSError,RuntimeError) as exc:error=type(exc).__name__+':'+str(exc)
        same_manifest=manifest.exists() and os.path.samestat(info,manifest.lstat()) and manifest.read_bytes()==raw
        same_locks=all((parent/name).exists() and os.path.samestat(node,(parent/name).lstat()) for name,node in locks.items())
        emit('manifest-link-crash-retry',checkpoint=checkpoint,actualExit=child.returncode,error=error,
            originalNlink=info.st_nlink,manifestSameNodeAndBytes=same_manifest,allTwelveLockNodesPreserved=same_locks,
            tempAliasRemoved=not os.path.lexists(temps[0]),currentNlink=manifest.lstat().st_nlink)
        with self.subTest(boundary='actual-link-crash-retry-succeeds'):
            if error is not None:self.fail('boundary recovery unsupported after actual linked-manifest crash: '+error)
        with self.subTest(boundary='same-manifest-and-all-lock-nodes'):
            self.assertTrue(same_manifest);self.assertTrue(same_locks)
        with self.subTest(boundary='exact-owned-temp-alias-reclaimed'):
            self.assertFalse(os.path.lexists(temps[0]));self.assertEqual(manifest.lstat().st_nlink,1)

    def test_missing_unrelated_queue_lock_refuses_new_open_but_preserves_existing_close_authority(self):
        from laya_index_a import LayaIndexA
        windows=None;writer=None;new_writer=None;saved=None;named=None
        with self.seed.prepared():
            windows,component=self.api()
            parent=self.seed.reader._lock_dir(self.seed.install,create=False)
            base_lock=parent/'base-physical.lock'
            def raw_base_exclusive():
                fd=os.open(base_lock,os.O_RDONLY|os.O_NOFOLLOW)
                try:
                    try:fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
                    except BlockingIOError:return True
                    return False
                finally:os.close(fd)
            self.assertFalse(raw_base_exclusive(),'raw valid base lock is free before actual open')
            observer=ConnectionObservation(self.case.db,windows,component)
            observer.exclusive_held=raw_base_exclusive
            with mock.patch.object(sqlite3,'connect',side_effect=observer.connect):
                writer=LayaIndexA(self.case.db,coordination=component)
            reference=observer.latest();initial_rw=len(observer.refs)
            named=parent/'queue-mutation.lock';saved=parent/'queue-mutation.fixture-preserved'
            original=named.lstat();os.rename(named,saved)
            error=None;close_error=None;drain_error=None;lifetime_error=None
            try:
                with mock.patch.object(sqlite3,'connect',side_effect=observer.connect):
                    try:new_writer=LayaIndexA(self.case.db,coordination=component)
                    except (ValueError,OSError,RuntimeError) as exc:error=type(exc).__name__+':'+str(exc)
                try:writer.close(deadline=time.monotonic()+0.1)
                except (ValueError,OSError,RuntimeError) as exc:close_error=type(exc).__name__+':'+str(exc)
                try:windows.drain_pending(deadline=time.monotonic()+0.1)
                except (ValueError,OSError,RuntimeError) as exc:drain_error=type(exc).__name__+':'+str(exc)
                try:
                    with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
                except (ValueError,OSError,RuntimeError) as exc:lifetime_error=str(exc)
                closes=[row for row in observer.events if row['event']=='actual-db-close']
                emit('unrelated-window-missing-existing-owner-close',newOpenError=error,
                    newRwConnects=len(observer.refs)-initial_rw,closeError=close_error,drainError=drain_error,
                    actualCloseEvents=closes,pending=windows.pending_count(),lifetimeExclusiveError=lifetime_error,
                    queueNameStillMissing=not os.path.lexists(named),rawBaseEXProbe=True)
                with self.subTest(boundary='new-open-still-refused'):
                    self.assertIsNotNone(error);self.assertEqual(len(observer.refs)-initial_rw,0)
                with self.subTest(boundary='existing-own-base-close-survives-unrelated-loss'):
                    self.assertIsNone(close_error);self.assertIsNone(drain_error)
                    self.assertEqual(len(closes),1);self.assertTrue(closes[0]['physicalExclusive'])
                    self.assert_closed(reference);self.assertEqual(windows.pending_count(),0)
                with self.subTest(boundary='closed-handle-releases-independent-lifetime'):
                    self.assertIsNone(lifetime_error)
            finally:
                if os.path.lexists(named):
                    node=named.lstat()
                    if not stat.S_ISREG(node.st_mode) or node.st_uid!=os.getuid() or node.st_nlink!=1:
                        raise RuntimeError('unsafe fixture queue-lock cleanup refused')
                    named.unlink()
                os.rename(saved,named)
                if not os.path.samestat(original,named.lstat()):raise RuntimeError('queue lock original inode not restored')
                if new_writer is not None:new_writer.close(deadline=time.monotonic()+0.5)
                if writer is not None:writer.close(deadline=time.monotonic()+0.5)
                self.drain(windows)

    def test_open_owned_cannot_redirect_component_a_opener_to_foreign_database_b(self):
        foreign=self.case.home/'foreign-window.sqlite'
        with closing(sqlite3.connect(foreign)) as db:
            db.execute('CREATE TABLE sentinel(value TEXT)');db.execute("INSERT INTO sentinel VALUES('original')");db.commit()
        foreign.chmod(0o600);before=self.files(foreign)
        with self.seed.prepared():
            windows,component=self.api();actual=sqlite3.connect;connections=[];initialized=[];owner=None;error=None
            def connect(database,*args,**kwargs):
                if database_path(database)==foreign:connections.append(str(database))
                return actual(database,*args,**kwargs)
            def opener():return sqlite3.connect(foreign.as_uri()+'?mode=rw',uri=True,isolation_level=None)
            def initialize(db):
                initialized.append(True);db.execute("INSERT INTO sentinel VALUES('wrong-component-sideeffect')");db.commit()
            try:
                with mock.patch.object(sqlite3,'connect',side_effect=connect):
                    try:owner=windows.open_owned(component,path=self.case.db,opener=opener,
                        initialize=initialize,deadline=time.monotonic()+0.2)
                    except (TypeError,ValueError,RuntimeError) as exc:error=type(exc).__name__+':'+str(exc)
            finally:
                if owner is not None:windows.close_or_defer(owner,deadline=time.monotonic()+0.5)
                self.drain(windows)
            unchanged=self.files(foreign)==before
            emit('foreign-opener-authority',error=error,foreignConnects=connections,
                 initializeCalls=len(initialized),foreignFilesUnchanged=unchanged)
            with self.subTest(boundary='explicit-preconnect-refusal'):self.assertIsNotNone(error)
            with self.subTest(boundary='foreign-first-open-zero'):self.assertEqual(connections,[])
            with self.subTest(boundary='foreign-sideeffect-zero'):self.assertEqual(initialized,[])
            with self.subTest(boundary='foreign-bytes-nodes-aux-preserved'):
                self.assertTrue(unchanged,'B must remain unchanged under component A authority')

    def test_stale_issued_installer_attestation_refuses_coordinated_rw_before_connect(self):
        from laya_index_a import LayaIndexA
        attestation=self.case.state/seed_module.base.J.PRODUCTION_ATTESTATION
        original=attestation.read_bytes();writer=None
        with self.seed.prepared():
            windows,component=self.api();actual=sqlite3.connect;rw=[];error=None;proof_error=None
            def connect(database,*args,**kwargs):
                if database_path(database)==self.case.db and 'mode=rw' in str(database):rw.append(str(database))
                return actual(database,*args,**kwargs)
            try:
                document=json.loads(original);document['sourceCommit']='f'*40
                attestation.write_text(json.dumps(document,sort_keys=True,separators=(',',':')),encoding='utf8')
                try:self.seed.reader._recheck(self.seed.installer.endpoint)
                except (ValueError,OSError,RuntimeError) as exc:proof_error=str(exc)
                if proof_error is None:raise RuntimeError('actual full installer recheck did not reject changed attestation')
                before=self.files(self.case.db)
                with mock.patch.object(sqlite3,'connect',side_effect=connect):
                    try:writer=LayaIndexA(self.case.db,coordination=component)
                    except (ValueError,OSError,RuntimeError) as exc:error=type(exc).__name__+':'+str(exc)
            finally:
                attestation.write_bytes(original)
                if writer is not None:writer.close(deadline=time.monotonic()+0.5)
                self.drain(windows)
            unchanged=self.files(self.case.db)==before
            emit('stale-installer-window-open',actualInstallerRecheckError=proof_error,
                originalInstallerStillIssued=self.seed.installer in self.seed.reader._issued,
                coordinatedError=error,rwConnects=rw,baseFilesUnchanged=unchanged)
            with self.subTest(boundary='fresh-full-proof-required'):self.assertIsNotNone(error)
            with self.subTest(boundary='stale-rw-connect-zero'):self.assertEqual(rw,[])
            with self.subTest(boundary='stale-base-preserved'):
                self.assertTrue(unchanged,'failed authority must not change base or auxiliaries')

    def test_complete_prepare_never_recreates_missing_physical_name_beneath_held_old_sh(self):
        with self.seed.prepared():
            windows,component=self.api()
            parent=self.seed.reader._lock_dir(self.seed.install,create=False)
            named=parent/'base-physical.lock';saved=parent/'base-physical.fixture-preserved'
            original=named.lstat()
            windows.prepare_windows(self.seed.installer)
            self.assertTrue(os.path.samestat(original,named.lstat()),'normal completed reprepare keeps the same inode')
            with windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.2):
                os.rename(named,saved)
                error=None;new_created=False;replacement_ex=False;old_busy=False
                try:
                    fd=os.open(saved,os.O_RDONLY|os.O_NOFOLLOW)
                    try:
                        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        except BlockingIOError:old_busy=True
                    finally:os.close(fd)
                    if not old_busy:raise RuntimeError('actual original physical SH was not held after rename')
                    try:windows.prepare_windows(self.seed.installer)
                    except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
                    new_created=os.path.lexists(named)
                    if new_created:
                        try:
                            with windows.physical_window(component,exclusive=True,deadline=time.monotonic()+0.03):replacement_ex=True
                        except (ValueError,OSError,RuntimeError):pass
                    emit('completed-window-lock-recreate',error=error,actualOldInodeSHBusy=old_busy,
                        originalNode=[original.st_dev,original.st_ino],newNameCreated=new_created,
                        newNode=None if not new_created else [named.lstat().st_dev,named.lstat().st_ino],
                        replacementExclusiveAcquired=replacement_ex)
                    with self.subTest(boundary='completed-prepare-missing-name-refused'):self.assertIsNotNone(error)
                    with self.subTest(boundary='no-second-physical-inode'):self.assertFalse(new_created)
                    with self.subTest(boundary='no-replacement-exclusive'):self.assertFalse(replacement_ex)
                finally:
                    if os.path.lexists(named):
                        info=named.lstat()
                        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1
                                or os.path.samestat(info,original)):
                            raise RuntimeError('unsafe fixture replacement cleanup refused')
                        named.unlink()
                    os.rename(saved,named)
                    if not os.path.samestat(original,named.lstat()):raise RuntimeError('original physical inode not restored')

    def test_background_thread_coordinated_rw_is_rejected_before_connect_and_cannot_orphan_pending(self):
        from laya_index_a import LayaIndexA
        with self.seed.prepared():
            windows,component=self.api();observer=ConnectionObservation(self.case.db,windows,component)
            progressed=threading.Event();opened=threading.Event();close_allowed=threading.Event()
            outcome=threading.Event();cleanup_allowed=threading.Event();finished=threading.Event()
            result={};external=ExitStack()
            def worker():
                writer=None
                try:
                    try:
                        writer=LayaIndexA(self.case.db,coordination=component)
                        opened.set();progressed.set()
                        if not close_allowed.wait(3):raise RuntimeError('thread close handshake deadline')
                        try:writer.close(deadline=time.monotonic()+0.03)
                        except windows.ReaderCloseDeferred as exc:result['deferred']=True;result['error']=str(exc)
                        writer=None;gc.collect()
                    except Exception as exc:
                        result['error']=str(exc);result['errorType']=type(exc).__name__;exc.__traceback__=None
                    result['pendingAtOutcome']=windows.pending_count()
                finally:
                    progressed.set();outcome.set()
                    if not cleanup_allowed.wait(5):result['cleanupHandshakeExpired']=True
                    try:
                        if writer is not None:writer.close(deadline=time.monotonic()+0.5)
                        result['remainingAfterWorkerDrain']=windows.drain_pending(deadline=time.monotonic()+1)
                        result['workerPendingAfterCleanup']=windows.pending_count()
                    except Exception as exc:result['cleanupError']=type(exc).__name__+':'+str(exc)
                    finished.set()
            context=copy_context();thread=threading.Thread(target=lambda:context.run(worker),name='owned-window-negative')
            try:
                with mock.patch.object(sqlite3,'connect',side_effect=observer.connect):
                    thread.start()
                    if not progressed.wait(4):raise RuntimeError('thread neither opened nor explicitly refused')
                    if opened.is_set():
                        external.enter_context(windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.2))
                    close_allowed.set()
                    if not outcome.wait(4):raise RuntimeError('thread close/refusal outcome deadline')
                    result['parentPendingAtOutcome']=windows.pending_count()
                    external.close();cleanup_allowed.set();thread.join(5)
                    if thread.is_alive():raise RuntimeError('owned thread did not finish same-thread drain')
            finally:
                external.close();close_allowed.set();cleanup_allowed.set()
                if thread.ident is not None:thread.join(5)
            emit('background-thread-coordination',actualOwnerContextCopied=True,opened=opened.is_set(),
                rwConnections=len(observer.refs),actualCloseEvents=observer.events,outcome=result,
                threadAlive=thread.is_alive(),parentPending=windows.pending_count())
            with self.subTest(boundary='explicit-main-thread-only-refusal'):
                self.assertEqual(result.get('error'),'reader_connection_main_thread_required')
            with self.subTest(boundary='background-first-rw-zero'):
                self.assertFalse(opened.is_set());self.assertEqual(len(observer.refs),0)
            with self.subTest(boundary='no-background-deferred-owner'):
                self.assertFalse(result.get('deferred',False));self.assertEqual(result.get('pendingAtOutcome'),0)
            self.assertFalse(thread.is_alive());self.assertTrue(finished.is_set())
            self.assertNotIn('cleanupError',result);self.assertNotIn('cleanupHandshakeExpired',result)
            self.assertEqual(result.get('workerPendingAfterCleanup'),0);self.assertEqual(windows.pending_count(),0)

    def test_uncheckpointed_wal_reader_defers_close_and_gc_keeps_strong_owner(self):
        windows=None;writer=None;reader_db=None;external=ExitStack();reference=None
        try:
            with self.seed.prepared():
                windows,component=self.api()
                with windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.2):pass
                with self.assertRaises((TypeError,ValueError,RuntimeError)):
                    with windows.physical_window({'kind':'base','path':str(self.case.db)},
                                                 exclusive=False,deadline=time.monotonic()+0.1):pass
                with self.assertRaises((TypeError,ValueError,RuntimeError)):type(component)()
                observer=ConnectionObservation(self.case.db,windows,component)
                from laya_index_a import LayaIndexA
                with mock.patch.object(sqlite3,'connect',side_effect=observer.connect):
                    writer=LayaIndexA(self.case.db,coordination=component)
                reference=observer.latest()
                main_before=self.case.db.read_bytes()
                body='synthetic windows committed WAL sentinel'
                digest=hashlib.sha256(body.encode('utf8')).hexdigest()
                writer.ensure_content(digest,body)
                wal=Path(str(self.case.db)+'-wal')
                self.assertTrue(wal.is_file());self.assertGreater(wal.stat().st_size,0)
                self.assertEqual(self.case.db.read_bytes(),main_before,'sentinel must remain an uncheckpointed WAL commit')
                external.enter_context(windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.2))
                reader_db=sqlite3.connect(self.case.db.as_uri()+'?mode=ro',uri=True,isolation_level=None)
                self.assertEqual(reader_db.execute('SELECT body FROM content WHERE hash=?',(digest,)).fetchone(),(body,))
                with self.assertRaises(windows.ReaderCloseDeferred):writer.close(deadline=time.monotonic()+0.03)
                writer=None;gc.collect()
                self.assertTrue(self.live(reference),'module must strongly retain the actual connection after caller GC')
                self.assertEqual(windows.pending_count(),1)
                pending_elsewhere=[]
                counter_thread=threading.Thread(target=lambda:pending_elsewhere.append(windows.pending_count()),
                    name='pending-count-only-observer')
                counter_thread.start();counter_thread.join(1)
                if counter_thread.is_alive():raise RuntimeError('pending-count-only observer did not terminate')
                emit('process-wide-pending-observer',mainPending=windows.pending_count(),
                     observerThreadPending=pending_elsewhere,observerSQLiteCalls=0)
                with self.subTest(boundary='process-wide-pending-count-visible-from-other-thread'):
                    self.assertEqual(pending_elsewhere,[1])
                self.assertFalse(any(row['event']=='actual-db-close' for row in observer.events))
            # Actual mover owner/context is gone. The handle's independent
            # lifetime SH must survive until its deferred physical close.
            lifetime_busy=None
            try:
                with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
            except ValueError as exc:lifetime_busy=str(exc)
            emit('wal-close-owner',pending=windows.pending_count(),connectionAliveAfterGc=self.live(reference),
                 lifetimeAfterOuterOwner=lifetime_busy,actualCloseEvents=observer.events,walExists=wal.exists())
            self.assertEqual(lifetime_busy,'reader_lifetime_busy')
            reader_db.close();reader_db=None;external.close()
            self.assertEqual(self.drain(windows),0);self.assertEqual(windows.pending_count(),0)
            self.assert_closed(reference)
            closes=[row for row in observer.events if row['event']=='actual-db-close']
            self.assertEqual(len(closes),1);self.assertTrue(closes[0]['physicalExclusive'])
            with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
        finally:
            if reader_db is not None:reader_db.close()
            external.close()
            if writer is not None:
                try:writer.close(deadline=time.monotonic()+0.5)
                except Exception:pass
            if windows is not None:self.drain(windows)

    def test_runtime_queue_constructor_failure_retains_pending_base_after_gc(self):
        windows=None;external=ExitStack();observer=None
        try:
            with self.seed.prepared():
                windows,component=self.api()
                observer=ConnectionObservation(self.case.db,windows,component)
                runtime_module=importlib.import_module('laya_index_runtime')
                def queue_failure(*args,**kwargs):
                    # The callback retains only a weak observation, never an
                    # index/runtime/db strong reference from the failed ctor.
                    observer.latest()
                    external.enter_context(windows.physical_window(component,exclusive=False,
                        deadline=time.monotonic()+0.2))
                    raise QueueConstructorFailure('synthetic_queue_constructor_failure')
                error=None;returned=False
                with mock.patch.object(sqlite3,'connect',side_effect=observer.connect), \
                        mock.patch.object(runtime_module.ClaimQueue,'open',side_effect=queue_failure):
                    try:
                        runtime_module.LayaIndexRuntime(source_root={self.case.WIKI:str(self.case.auto/'wiki')},
                            scope=self.case.route['scopeId'],db_path=str(self.case.db),queue_path=str(self.case.queue),
                            db_incarnation=self.case.route['dbIncarnation'],wiki_only=True,
                            connection_windows={'base':component})
                        returned=True
                    except (QueueConstructorFailure,windows.ReaderCloseDeferred) as exc:
                        error=type(exc).__name__+':'+str(exc)
                        exc.__traceback__=None
                gc.collect();reference=observer.latest()
                emit('runtime-partial-failure',error=error,returned=returned,pending=windows.pending_count(),
                    connectionAliveAfterGc=self.live(reference),actualCloseEvents=observer.events,
                    testOwnsOnlyWeakConnectionReferences=True)
                self.assertFalse(returned);self.assertIsNotNone(error)
                self.assertEqual(windows.pending_count(),1)
                self.assertTrue(self.live(reference),'failed runtime must not rely on its traceback/GC to retain base')
                self.assertFalse(any(row['event']=='actual-db-close' for row in observer.events))
            external.close()
            self.assertEqual(self.drain(windows),0);self.assertEqual(windows.pending_count(),0)
            self.assert_closed(reference)
            closes=[row for row in observer.events if row['event']=='actual-db-close']
            self.assertEqual(len(closes),1);self.assertTrue(closes[0]['physicalExclusive'])
        finally:
            external.close()
            if windows is not None:self.drain(windows)

    def test_first_pragma_failure_closes_actual_connection_under_physical_exclusive(self):
        windows=None
        try:
            with self.seed.prepared():
                windows,component=self.api()
                with windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.2):pass
                observer=ConnectionObservation(self.case.db,windows,component,fail_first_pragma=True)
                from laya_index_a import LayaIndexA
                error=None;returned=False
                with mock.patch.object(sqlite3,'connect',side_effect=observer.connect):
                    try:
                        LayaIndexA(self.case.db,coordination=component);returned=True
                    except (sqlite3.OperationalError,ValueError,RuntimeError) as exc:
                        error=type(exc).__name__+':'+str(exc);exc.__traceback__=None
                gc.collect()
                emit('first-pragma-init-failure',error=error,returned=returned,injected=observer.injected,
                     actualConnectionEvents=observer.events,pending=windows.pending_count(),
                     activeOwnerRegistryPrivateStateInspected=False)
                self.assertFalse(returned);self.assertTrue(observer.injected)
                self.assertIsNotNone(error);self.assertIn('synthetic_first_pragma_failure',error)
                failures=[row for row in observer.events if row['event']=='first-pragma-failure']
                closes=[row for row in observer.events if row['event']=='actual-db-close']
                self.assertEqual(len(failures),1);self.assertTrue(failures[0]['physicalExclusive'])
                self.assertEqual(len(closes),1);self.assertTrue(closes[0]['physicalExclusive'])
                self.assert_closed(observer.latest());self.assertEqual(windows.pending_count(),0)
                with windows.physical_window(component,exclusive=True,deadline=time.monotonic()+0.2):pass
        finally:
            if windows is not None:self.drain(windows)

if __name__=='__main__':unittest.main(verbosity=2)
