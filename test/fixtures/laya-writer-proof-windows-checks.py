#!/usr/bin/env python3
"""SQL-free writer proof admission: actual SyncLease, pin and kernel flocks."""
from contextlib import contextmanager, ExitStack
import importlib
import importlib.util
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('proof_plan_fixture',ROOT/'test/fixtures/laya-writer-open-plan-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)
ROLES=('base','binding','catalog','queue','sidecar')
LOCK_CHILD=r'''
import fcntl,json,os,signal,sys
signal.alarm(10)
fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
try:
    try:
        fcntl.flock(fd,(fcntl.LOCK_SH if sys.argv[2]=='SH' else fcntl.LOCK_EX)|fcntl.LOCK_NB)
        acquired=True
    except BlockingIOError:acquired=False
    print(json.dumps(dict(acquired=acquired,pid=os.getpid())),flush=True)
    if acquired and sys.argv[3]=='hold':
        if sys.stdin.readline()!='release\n':sys.exit(4)
finally:os.close(fd)
'''

def emit(label,**values):print(json.dumps(dict(case=label,**values),sort_keys=True),flush=True)

class WriterProofWindowsChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.WriterOpenPlanChecks('runTest')
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.seed,self.case,self.W=self.fixture.seed,self.fixture.case,self.fixture.W
        self.plan_module=importlib.import_module('laya_writer_open_plan')
        self.sync=importlib.import_module('sync_owner_lock')
        # Leave every mover scope before SyncLease acquisition. No ambient owner
        # or installer capability is passed to the new proof-window issuer.
        with self.seed.prepared():
            self.W.prepare_windows(self.seed.installer)
            self.plan=self.plan_module.capture_writer_plan(self.case.project,executing_root=self.case.plugin)
            self.plan_module.validate_writer_plan(self.plan)
        self.parent=self.seed.reader._lock_dir(self.seed.install,create=False)

    def api(self):
        with self.sync._owner_lock(Path(self.plan.queue_path)) as owned:
            self.assertIs(owned,True)
            with self.sync._owner_lock(Path(self.plan.queue_path)) as busy:self.assertIs(busy,False)
        missing=[name for name in ('writer_proof_windows','validate_writer_proof_window')
                 if not callable(getattr(self.plan_module,name,None))]
        emit('proof-window-prerequisite',actualV5=True,actualMetadataPlan=True,
             realLegacyOwnerExclusion=True,missing=missing)
        if missing:self.fail('writer proof windows feature RED after actual plan/legacy flock baseline: '+','.join(missing))
        return self.plan_module.writer_proof_windows,self.plan_module.validate_writer_proof_window

    def probe(self,name,mode):
        result=subprocess.run([sys.executable,'-B','-I','-c',LOCK_CHILD,str(self.parent/name),mode,'probe'],
            encoding='utf8',capture_output=True,timeout=5)
        if result.returncode:raise RuntimeError('fresh lock probe failed: '+result.stderr)
        return json.loads(result.stdout)['acquired']

    @contextmanager
    def held(self,name,mode):
        child=subprocess.Popen([sys.executable,'-B','-I','-c',LOCK_CHILD,str(self.parent/name),mode,'hold'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
        try:
            if not select.select([child.stdout],[],[],5)[0]:raise RuntimeError('holder checkpoint deadline')
            line=child.stdout.readline()
            if not line or not json.loads(line)['acquired']:raise RuntimeError('independent physical SH holder failed')
            yield
        finally:
            if child.poll() is None:
                try:_out,err=child.communicate('release\n',timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill();child.communicate();raise
            else:_out,err=child.communicate(timeout=5)
            if child.returncode:raise RuntimeError('owned holder failed: '+err)

    def test_expired_sync_authority_keeps_pin_and_all_proof_windows_until_context_exit(self):
        issuer,validate=self.api()
        before=self.fixture.snapshot(self.parent)
        owner_scope=ExitStack();context=None;entered=False;exit_error=None
        claim=owner_scope.enter_context(self.sync.sync_owner(Path(self.plan.queue_path)))
        self.assertIsNotNone(claim)
        try:
            with self.fixture.traced() as calls:
                context=issuer(self.plan,queue_owner=claim,deadline=time.monotonic()+2)
                window=context.__enter__();entered=True
                validate(window)
                physical={role:self.probe(role+'-physical.lock','SH') for role in ROLES}
                lifetime=self.probe('reader-lifetime.lock','EX')
                self.assertFalse(any(physical.values()))
                self.assertFalse(lifetime)
                owner_scope.close()
                with self.assertRaises((ValueError,RuntimeError,TypeError)):
                    validate(window)
                with self.sync.sync_owner(Path(self.plan.queue_path)) as contender:
                    self.assertIsNone(contender,'close-only pin must preserve actual queue exclusion')
                try:context.__exit__(None,None,None)
                except (ValueError,RuntimeError,TypeError) as exc:exit_error=str(exc)
                finally:entered=False
                with self.assertRaises((ValueError,RuntimeError,TypeError)):validate(window)
            free={role:self.probe(role+'-physical.lock','SH') for role in ROLES}
            lifetime_free=self.probe('reader-lifetime.lock','EX')
            with self.sync.sync_owner(Path(self.plan.queue_path)) as next_owner:
                self.assertIsNotNone(next_owner)
            emit('proof-window-pin-lifecycle',physicalWhileActive=physical,lifetimeEXWhileActive=lifetime,
                 exitReason=exit_error,physicalAfter=free,lifetimeEXAfter=lifetime_free,contentConnects=calls)
            self.assertTrue(all(free.values()));self.assertTrue(lifetime_free)
            self.assertEqual(calls,[])
            self.assertTrue(self.fixture.snapshot(self.parent)==before,'proof windows must not mutate UID registry')
        finally:
            owner_scope.close()
            if entered:
                try:context.__exit__(None,None,None)
                except (ValueError,RuntimeError,TypeError):pass

    def test_middle_role_busy_releases_partial_windows_pin_and_lifetime_without_sql(self):
        issuer,_validate=self.api()
        before=self.fixture.snapshot(self.parent)
        error=None;elapsed=None;returned=False
        with self.sync.sync_owner(Path(self.plan.queue_path)) as claim:
            self.assertIsNotNone(claim)
            with self.held('catalog-physical.lock','SH'):
                targets={role:tuple(getattr((self.parent/(role+'-physical.lock')).stat(),key)
                    for key in ('st_dev','st_ino')) for role in ('base','binding')}
                acquired=[];actual=fcntl.flock
                def flock(fd,operation):
                    result=actual(fd,operation)
                    if operation & fcntl.LOCK_EX:
                        info=os.fstat(fd);node=(info.st_dev,info.st_ino)
                        acquired.extend(role for role,value in targets.items() if value==node)
                    return result
                with self.fixture.traced() as calls,mock.patch.object(fcntl,'flock',side_effect=flock):
                    started=time.monotonic()
                    try:
                        with issuer(self.plan,queue_owner=claim,deadline=started+.5):returned=True
                    except (ValueError,RuntimeError,OSError) as exc:error=str(exc)
                    elapsed=time.monotonic()-started
                prior={role:self.probe(role+'-physical.lock','SH') for role in ('base','binding')}
                lifetime=self.probe('reader-lifetime.lock','EX')
                emit('middle-proof-window-busy',reason=error,elapsed=elapsed,returned=returned,
                     earlierWindowsReleased=prior,lifetimeReleased=lifetime,contentConnects=calls)
                self.assertIsNotNone(error);self.assertFalse(returned)
                self.assertTrue(any(word in error.lower() for word in ('busy','deadline','timeout')))
                self.assertLess(elapsed,2)
                self.assertEqual(acquired,['base','binding'],'actual partial EX prefix is required')
                self.assertTrue(all(prior.values()));self.assertTrue(lifetime)
                self.assertEqual(calls,[])
        with self.sync.sync_owner(Path(self.plan.queue_path)) as next_owner:self.assertIsNotNone(next_owner)
        self.assertTrue(all(self.probe(role+'-physical.lock','SH') for role in ROLES))
        self.assertTrue(self.probe('reader-lifetime.lock','EX'))
        self.assertTrue(self.fixture.snapshot(self.parent)==before,'failed admission must preserve registry bytes/inodes')

if __name__=='__main__':unittest.main()
