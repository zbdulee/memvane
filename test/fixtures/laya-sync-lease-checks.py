#!/usr/bin/env python3
"""Issued sync-owner capabilities over actual kernel flock, with no fork."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'core'))
from memvane.sync import sync_owner_lock as sync

PROBE=r'''
import fcntl,json,os,signal,sys
signal.alarm(4)
fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
try:
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);held=True
    except BlockingIOError:held=False
    print(json.dumps(dict(acquired=held,pid=os.getpid())),flush=True)
finally:os.close(fd)
'''

OWNER_PROBE=r'''
import json,signal,sys
from pathlib import Path
signal.alarm(4)
sys.path.insert(0,sys.argv[1])
from laya_index_a import IndexContractError
from sync_owner_lock import sync_owner
try:
    with sync_owner(Path(sys.argv[2])) as owner:
        result=dict(acquired=owner is not None,error=None)
except IndexContractError as exc:result=dict(acquired=False,error=str(exc))
print(json.dumps(result),flush=True)
'''

ENGINE_PROBE=r'''
import json,signal,sys
from pathlib import Path
signal.alarm(8)
sys.path.insert(0,sys.argv[1])
try:
    import sync_owner_lock as sync
    with sync._owner_lock(Path(sys.argv[2])) as owned:
        result=dict(owned=owned,exactBool=type(owned) is bool,error=None,module=sync.__file__)
except Exception as exc:result=dict(owned=False,error=type(exc).__name__+': '+str(exc))
print(json.dumps(result),flush=True)
'''

def emit(case,**fields):print(json.dumps(dict(case=case,**fields),sort_keys=True),flush=True)
def evidence(path):
    info=path.lstat()
    return path.read_bytes(),(info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_nlink)

class SyncLeaseChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='sync-lease-',dir=os.environ['HOME'])
        self.addCleanup(self.tmp.cleanup);self.home=Path(self.tmp.name).resolve();self.home.chmod(0o700)
        self.queue=self.home/'queue.sqlite';self.other=self.home/'other.sqlite'
        for path in (self.queue,self.other):
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE fixture(value TEXT)');db.execute("INSERT INTO fixture VALUES('preserved')");db.commit()
            path.chmod(0o600)
        self.lock=self.home/'sync.lock'
    def probe(self):
        result=subprocess.run([sys.executable,'-B','-I','-c',PROBE,str(self.lock)],
            encoding='utf8',capture_output=True,timeout=5)
        if result.returncode:raise RuntimeError('fresh-exec kernel probe failed: '+result.stderr)
        return json.loads(result.stdout)['acquired']
    def api(self):
        # A real existing API baseline precedes every new-feature assertion.
        with sync._owner_lock(self.queue) as owned:
            self.assertIs(type(owned),bool);self.assertIs(owned,True)
            with sync._owner_lock(self.queue) as nested:self.assertIs(nested,False)
            busy=not self.probe()
            self.assertTrue(busy)
        free=self.probe();self.assertTrue(free)
        emit('legacy-sync-owner-kernel-baseline',exactBool=True,independentProbeBusy=busy,freeAfterExit=free)
        missing=[name for name in ('sync_owner','validate_sync_owner','borrow_sync_owner')
                 if not callable(getattr(sync,name,None))]
        if missing:self.fail('sync lease feature RED after actual legacy flock proof: '+','.join(missing))
    def rejected(self,claim,queue=None):
        with self.assertRaises((TypeError,ValueError,RuntimeError)):
            sync.validate_sync_owner(claim,self.queue if queue is None else queue)
    def borrow_rejected(self,claim,queue=None):
        try:
            with sync.borrow_sync_owner(self.queue if queue is None else queue,claim=claim) as owned:
                return type(owned) is bool and owned is False
        except (TypeError,ValueError,RuntimeError):return True

    def test_active_issued_claim_explicit_borrow_exact_bool_and_no_ambient_reentry(self):
        self.api();before=evidence(self.queue)
        with sync.sync_owner(self.queue) as claim:
            self.assertIsNotNone(claim);self.assertNotIsInstance(claim,(bool,dict))
            sync.validate_sync_owner(claim,self.queue)
            self.assertFalse(self.probe())
            with sync._owner_lock(self.queue) as legacy:self.assertIs(type(legacy),bool);self.assertIs(legacy,False)
            with sync.borrow_sync_owner(self.queue,claim=claim) as borrowed:
                self.assertIs(type(borrowed),bool);self.assertIs(borrowed,True)
            with sync.borrow_sync_owner(self.queue) as nested:self.assertIs(nested,False)
            with sync.sync_owner(self.queue) as nested:self.assertIsNone(nested)
            self.rejected(claim,self.other);self.rejected(True);self.rejected(False);self.rejected({})
            self.rejected(object.__new__(type(claim)))
            with self.assertRaises((TypeError,ValueError,RuntimeError)):type(claim)()
            self.assertTrue(self.borrow_rejected(True));self.assertTrue(self.borrow_rejected(claim,self.other))
            emit('active-sync-lease',claimType=type(claim).__name__,externalKernelBusy=True,
                explicitBorrow=True,ambientReentry=False,queueUnchanged=evidence(self.queue)==before)
        self.rejected(claim);self.assertTrue(self.probe());self.assertTrue(evidence(self.queue)==before)

    def test_named_lock_inode_symlink_hardlink_and_queue_alias_refuse_without_external_changes(self):
        self.api();external=self.home/'external-marker';external.write_bytes(b'outside fixture sentinel');external.chmod(0o600)
        original_external=evidence(external);original_queue=evidence(self.queue)
        with sync.sync_owner(self.queue) as claim:
            original=self.lock.lstat();saved=self.home/'saved-original-lock'
            for variant in ('replaced-inode','external-symlink','hardlink'):
                with self.subTest(variant=variant):
                    alias=self.home/'private-lock-alias'
                    if variant=='hardlink':os.link(self.lock,alias)
                    else:
                        os.rename(self.lock,saved)
                        if variant=='external-symlink':self.lock.symlink_to(external)
                        else:
                            fd=os.open(self.lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
                    try:
                        self.rejected(claim);self.assertTrue(self.borrow_rejected(claim))
                        self.assertTrue(evidence(external)==original_external)
                        self.assertTrue(evidence(self.queue)==original_queue)
                        emit('sync-lease-private-node-mutation',variant=variant,claimRejected=True,
                            externalBytesAndNodePreserved=True)
                    finally:
                        if variant=='hardlink':alias.unlink()
                        else:self.lock.unlink();os.rename(saved,self.lock)
                    self.assertTrue(os.path.samestat(original,self.lock.lstat()))
                    sync.validate_sync_owner(claim,self.queue)
            queue_alias=self.home/'queue-alias.sqlite';queue_alias.symlink_to(self.queue)
            self.rejected(claim,queue_alias)
            with self.assertRaises((TypeError,ValueError,RuntimeError)):
                with sync.sync_owner(queue_alias):pass
        self.assertEqual(stat.S_IMODE(self.lock.stat().st_mode),0o600);self.assertTrue(self.probe())
        self.assertTrue(evidence(external)==original_external)

    def test_close_only_pin_preserves_kernel_exclusion_after_lexical_authority_expires(self):
        self.api();pin=None
        try:
            with sync.sync_owner(self.queue) as claim:
                sync.validate_sync_owner(claim,self.queue);pin=claim.pin()
                self.assertTrue(callable(pin.close));self.rejected(pin)
            self.rejected(claim);self.assertTrue(self.borrow_rejected(claim))
            with self.assertRaises((TypeError,ValueError,RuntimeError)):claim.pin()
            held=not self.probe()
            emit('sync-lease-pin-after-lexical-exit',claimExpired=True,pinIsAuthority=False,
                 independentFreshExecBusy=held)
            self.assertTrue(held)
            with sync.sync_owner(self.queue) as busy:self.assertIsNone(busy)
            pin.close();pin.close();pin=None
            self.assertTrue(self.probe())
            with sync.sync_owner(self.queue) as fresh:
                self.assertIsNotNone(fresh);sync.validate_sync_owner(fresh,self.queue)
                self.assertIsNot(fresh,claim)
        finally:
            if pin is not None:pin.close()

    def test_fresh_owner_refuses_missing_or_replaced_lock_while_expired_claim_pin_lives(self):
        self.api();pin=None
        try:
            with sync.sync_owner(self.queue) as claim:pin=claim.pin()
            saved=self.home/'pinned-original-lock'
            for variant in ('missing','replaced','queue-and-lock-replaced'):
                with self.subTest(variant=variant):
                    saved_queue=self.home/'pinned-original-queue'
                    if variant=='queue-and-lock-replaced':
                        os.rename(self.queue,saved_queue)
                        with closing(sqlite3.connect(self.queue)) as db:
                            db.execute('CREATE TABLE replacement(value TEXT)');db.commit()
                        self.queue.chmod(0o600)
                    os.rename(self.lock,saved)
                    if variant!='missing':
                        self.lock.write_bytes(b'foreign lock sentinel');self.lock.chmod(0o600)
                    foreign=evidence(self.lock) if self.lock.exists() else None
                    try:
                        result=subprocess.run([sys.executable,'-B','-I','-c',OWNER_PROBE,
                            str(ROOT/'core'),str(self.queue)],encoding='utf8',capture_output=True,timeout=5)
                        self.assertEqual(result.returncode,0,result.stderr)
                        observed=json.loads(result.stdout)
                        created=foreign is None and self.lock.exists()
                        emit('fresh-owner-pinned-lock-continuity',variant=variant,**observed,newLockCreated=created)
                        self.assertFalse(created,'pinned lock name must not be recreated')
                        self.assertFalse(observed['acquired'],'pinned old inode must exclude a fresh owner')
                        if foreign is not None:self.assertEqual(evidence(self.lock),foreign)
                    finally:
                        if self.lock.exists():self.lock.unlink()
                        os.rename(saved,self.lock)
                        if saved_queue.exists():
                            self.queue.unlink();os.rename(saved_queue,self.queue)
                    self.assertFalse(self.probe())
            pin.close();pin=None
            self.assertTrue(self.probe())
        finally:
            if pin is not None:pin.close()

    def test_actual_other_thread_cannot_borrow_claim_but_none_borrow_obeys_kernel_lifecycle(self):
        self.api();result={}
        with sync.sync_owner(self.queue) as claim:
            def denied():
                try:sync.validate_sync_owner(claim,self.queue)
                except (TypeError,ValueError,RuntimeError):result['validateRejected']=True
                else:result['validateRejected']=False
                result['explicitBorrowRejected']=self.borrow_rejected(claim)
                with sync.borrow_sync_owner(self.queue) as owned:
                    result['noneBorrowBusy']=owned is False and type(owned) is bool
            thread=threading.Thread(target=denied,name='actual-sync-lease-other-thread')
            thread.start();thread.join(3)
            if thread.is_alive():raise RuntimeError('actual validation thread exceeded finite join')
            self.assertTrue(result.get('validateRejected'));self.assertTrue(result.get('explicitBorrowRejected'))
            self.assertTrue(result.get('noneBorrowBusy'))
        def acquire():
            with sync.borrow_sync_owner(self.queue) as owned:
                result['laterNoneBorrow']=owned is True and type(owned) is bool
                result['laterExternalBusy']=not self.probe()
        thread=threading.Thread(target=acquire,name='actual-sync-lease-free-thread')
        thread.start();thread.join(6)
        if thread.is_alive():raise RuntimeError('actual acquiring thread exceeded finite join')
        emit('sync-lease-thread-lifecycle',observations=result,freeAfterThread=self.probe())
        self.assertTrue(result.get('laterNoneBorrow'));self.assertTrue(result.get('laterExternalBusy'))
        self.assertTrue(self.probe())

    def test_body_io_error_keeps_original_exception_and_releases_both_owner_locks(self):
        self.api()
        for owner_context in (sync.sync_owner,sync._owner_lock):
            with self.subTest(api=owner_context.__name__):
                marker=OSError('synthetic caller IO failure');observed=None
                try:
                    with owner_context(self.queue):raise marker
                except BaseException as exc:observed=exc
                emit('sync-owner-caller-exception',api=owner_context.__name__,
                    originalException=observed is marker,observedType=type(observed).__name__)
                self.assertIs(observed,marker)
                with sync.sync_owner(self.queue) as fresh:self.assertIsNotNone(fresh)
                self.assertTrue(self.probe())

    def test_fixed_engine_artifacts_acquire_legacy_owner_without_full_plugin_dependencies(self):
        self.api()
        from memvane.install.product_bundle import ARTIFACTS
        core=self.home/'isolated-engine-core';core.mkdir(mode=0o700)
        for name in ARTIFACTS:shutil.copyfile(ROOT/'core'/name,core/name)
        self.assertFalse((core/'laya_journal_paths.py').exists())
        result=subprocess.run([sys.executable,'-B','-I','-c',ENGINE_PROBE,str(core),str(self.queue)],
            encoding='utf8',capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        observed=json.loads(result.stdout)
        emit('isolated-fixed-engine-sync-owner',**observed)
        self.assertIsNone(observed['error'])
        self.assertIs(observed['owned'],True);self.assertIs(observed['exactBool'],True)
        self.assertEqual(Path(observed['module']).parent,core)

    def test_anchor_fixture_namespace_rejects_non_temp_alias_and_non_private_root_before_create(self):
        from memvane.kernel.package import sync_owner_directory
        runtime=self.home/'private-runtime';runtime.mkdir(mode=0o700)
        spoof=self.home/'unused-home'
        with mock.patch.dict(os.environ,dict(MEMVANE_TEST_RUNTIME_ROOT=str(runtime),HOME=str(spoof))):
            parent=sync_owner_directory(create=True)
            self.assertEqual(parent,runtime/'memvane-journal'/'sync-owner-locks')
            self.assertFalse(spoof.exists())
        alias=self.home/'runtime-alias';alias.symlink_to(runtime)
        public=self.home/'public-runtime';public.mkdir(mode=0o755);public.chmod(0o755)
        for rejected in (ROOT,alias,public):
            with self.subTest(root=str(rejected)),mock.patch.dict(os.environ,
                    dict(MEMVANE_TEST_RUNTIME_ROOT=str(rejected))):
                with self.assertRaises(ValueError):sync_owner_directory(create=True)
        self.assertFalse((public/'memvane-journal').exists())
        emit('sync-anchor-test-root-boundary',homeIgnored=True,nonTempRejected=True,
            symlinkRejected=True,nonPrivateRejectedBeforeCreate=True)

if __name__=='__main__':unittest.main(verbosity=2)
