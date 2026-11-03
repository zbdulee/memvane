#!/usr/bin/env python3
"""Five actual prefix review boundaries; no existing positive suite rerun."""
from contextlib import ExitStack,closing
import importlib
import importlib.util
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
spec=importlib.util.spec_from_file_location('prefix_review_existing',HERE/'laya-reader-move-prefix-checks.py')
prefix=importlib.util.module_from_spec(spec);spec.loader.exec_module(prefix)
seed_module=prefix.seed_module

PREPARE_HELD=r'''
from contextlib import ExitStack,nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import importlib.util,json,os,signal,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime'];signal.alarm(30)
spec=importlib.util.spec_from_file_location('race_prepare_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_project_dir_move as mover,laya_state_move
helper=base.JournalReaderChecks('runTest');case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
project,state=Path(p['project']),Path(p['state'])
with helper.full_authority(case,mover),mock.patch.object(laya_state_move,'_provider_lane',lambda:nullcontext()),ExitStack() as stack:
    mover._locks(stack,project,state,mover._queue_path(project,None))
    intent=mover._prepare(project,state,'rename')
    print(json.dumps(dict(event='prepared-owner-held',version=base.J.journal_version(Path(p['journal'])),
        pointer=mover.read_pointer(project),intentPresent=mover.intent_present(state))),flush=True)
    if sys.stdin.readline().strip()!='release':raise RuntimeError('owner release handshake missing')
'''

HOT_READER=r'''
from pathlib import Path
from types import SimpleNamespace
import importlib.util,json,os,signal,sqlite3,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime'];signal.alarm(20)
spec=importlib.util.spec_from_file_location('hot_read_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as R,laya_reader_lease as L
actual=sqlite3.connect;calls=[]
def observe(name,*args,**kwargs):
    value=str(name)
    if value==p['journal'] or (value.split('?',1)[0]==Path(p['journal']).as_uri() and
                              ('mode=rw' in value or 'mode=rwc' in value)):calls.append(value)
    return actual(name,*args,**kwargs)
sqlite3.connect=observe
helper=base.JournalReaderChecks('runTest');case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
issued=False;error=None
with helper.full_authority(case,R,L):
    try:
        with L.resolve_endpoint(Path(p['project']),action='recall',host_root=case.plugin) as endpoint:
            issued=type(endpoint) is R.VerifiedEndpoint
    except Exception as exc:error=type(exc).__name__+':'+str(exc)
print(json.dumps(dict(event='hot-journal-read-attempt',issued=issued,error=error,rwOpens=calls)),flush=True)
'''


class PrefixReviewChecks(unittest.TestCase):
    def test_concurrent_v4_settle_between_pointer_exists_and_open_is_current(self):
        base=seed_module.base
        helper=base.JournalReaderChecks('runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        class PublishedV4(base.published.MoveCase):
            STATE_PARTS=('.local','state','memvane','3'*32)
            TEMP_DIR=str(Path('/tmp').resolve());TEMP_PREFIX=''
            PROVIDER_EXTRA={'serviceSocketPath':'{state}/runtime/laya-embed.sock'}
        case=helper.compose(PublishedV4)
        mover=importlib.import_module('laya_project_dir_move')
        with helper.full_authority(case),ExitStack() as locks:
            mover._locks(locks,case.project,case.state,mover._queue_path(case.project,None))
            mover._prepare(case.project,case.state,'rename')
        original_path=mover.pointer_path(case.project)
        payload=case.home/'settle-race.json'
        payload.write_text(json.dumps(dict(home=str(case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
            project=str(case.project),state=str(case.state),plugin=str(case.plugin),proof=case.proof)),encoding='utf8')
        payload.chmod(0o600)
        complete=PREPARE_HELD.split('with helper.full_authority',1)[0]+r'''
with helper.full_authority(case,mover),mock.patch.object(laya_state_move,'_provider_lane',lambda:nullcontext()):
    result=mover.move(project,state)
    assert result['status']=='project_dir_move_complete'
    assert mover.read_pointer(project) is None and not mover.intent_present(state)
    print(json.dumps(dict(event='actual-settle-complete',result=result)),flush=True)
'''
        actual_exists,actual_path=os.path.lexists,mover.pointer_path
        armed=False;settled=None;after_b=None
        def snapshot():
            return {str(path):(path.read_bytes(),path.stat().st_dev,path.stat().st_ino)
                for root in (case.project,case.state) for path in root.rglob('*')
                if path.is_file() and not path.is_symlink() and not path.name.endswith('.lock')}
        def locate(project):
            nonlocal armed
            path=actual_path(project)
            if path==original_path:armed=True
            return path
        def exists(path):
            nonlocal armed,settled,after_b
            present=actual_exists(path)
            if armed and settled is None and Path(path)==original_path and present:
                armed=False
                child=subprocess.run([sys.executable,'-B','-I','-c',complete,str(ROOT),str(payload)],
                    capture_output=True,encoding='utf8',timeout=35)
                if child.returncode:raise RuntimeError('actual B completion prerequisite failed: '+child.stderr)
                settled=json.loads(child.stdout)
                if settled.get('event')!='actual-settle-complete':raise RuntimeError('actual settle checkpoint missing')
                if actual_exists(original_path):raise RuntimeError('actual B did not remove original pointer')
                after_b=snapshot()
            return present
        error,outcome=None,None
        with helper.full_authority(case,mover),mock.patch.object(mover,'pointer_path',side_effect=locate), \
                mock.patch('os.path.lexists',side_effect=exists):
            try:outcome=mover.move(case.project,case.state)
            except (ValueError,OSError,RuntimeError) as exc:error=type(exc).__name__+':'+str(exc)
        if settled is None or after_b is None:raise RuntimeError('actual B settle race not reached')
        print(json.dumps(dict(case='v4-concurrent-settle-pointer',actualB=settled,error=error,result=outcome,
            filesUnchanged=snapshot()==after_b)),flush=True)
        self.assertIsNone(error,'normal completed move must not fail a pending pointer read')
        self.assertEqual(outcome,dict(status='project_dir_current'))
        self.assertTrue(snapshot()==after_b,'A must not modify B completed installation')

    def test_concurrent_v4_prepare_is_busy_not_stale_missing_intent(self):
        base=seed_module.base
        helper=base.JournalReaderChecks('runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        class PublishedV4(base.published.MoveCase):
            STATE_PARTS=('.local','state','memvane','3'*32)
            TEMP_DIR=str(Path('/tmp').resolve());TEMP_PREFIX=''
            PROVIDER_EXTRA={'serviceSocketPath':'{state}/runtime/laya-embed.sock'}
        case=helper.compose(PublishedV4)
        mover=importlib.import_module('laya_project_dir_move')
        upgrade=importlib.import_module('laya_bundle_upgrade')
        self.assertEqual(base.J.journal_version(case.journal),4)
        self.assertFalse(mover.intent_present(case.state))
        real_read_pointer=mover.read_pointer
        self.assertIsNone(real_read_pointer(case.project))
        payload=case.home/'race-owner-payload.json'
        payload.write_text(json.dumps(dict(home=str(case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
            project=str(case.project),state=str(case.state),journal=str(case.journal),
            plugin=str(case.plugin),proof=case.proof)),encoding='utf8')
        payload.chmod(0o600)
        child=None;prepared=None;before=None;calls=0
        def snapshot():
            return {str(path):(path.read_bytes(),path.stat().st_dev,path.stat().st_ino)
                for root in (case.project,case.state) for path in root.rglob('*')
                if path.is_file() and not path.is_symlink() and not path.name.endswith('.lock')}
        def schedule(project):
            nonlocal child,prepared,before,calls
            calls+=1
            if calls==1:
                # A already observed no intent. B publishes a real intent and
                # pointer before A's first actual pointer read, retaining all
                # actual owner flocks until the parent explicitly releases it.
                child=subprocess.Popen([sys.executable,'-B','-I','-c',PREPARE_HELD,str(ROOT),str(payload)],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
                readable,_,_=select.select([child.stdout],[],[],20)
                if not readable:raise RuntimeError('B actual prepare owner checkpoint timed out')
                line=child.stdout.readline()
                if not line:
                    _,stderr=child.communicate(timeout=5)
                    raise RuntimeError('B actual prepare failed before race checkpoint: '+stderr)
                prepared=json.loads(line)
                if (prepared.get('event')!='prepared-owner-held' or prepared.get('version')!=4 or
                        not prepared.get('intentPresent') or prepared.get('pointer') is None or child.poll() is not None):
                    raise RuntimeError('B did not establish actual v4 prepared owner scope')
                actual=mover._load_intent(case.state,case.project)
                if actual is None or real_read_pointer(case.project)!=prepared['pointer']:
                    raise RuntimeError('B checkpoint lacks actual matching durable intent/pointer')
                before=snapshot()
            return real_read_pointer(project)
        error=None;outcome=None;failure=None
        try:
            with helper.full_authority(case,mover),mock.patch.object(mover,'read_pointer',side_effect=schedule):
                try:outcome=mover.move(case.project,case.state)
                except (ValueError,OSError,RuntimeError) as exc:
                    failure=exc;error=upgrade._reason(exc)
            if prepared is None or before is None:
                raise RuntimeError('race did not reach B actual preparation checkpoint')
            unchanged=snapshot()==before
            busy=failure is not None and error in upgrade.MOVE_DEFERRALS
            complete=isinstance(outcome,dict) and outcome.get('status')=='project_dir_move_complete'
            print(json.dumps(dict(case='v4-concurrent-prepare-pointer',prepared=prepared,
                pointerReads=calls,ownerChildAlive=child.poll() is None,error=error,result=outcome,
                busyClassified=busy,filesUnchanged=unchanged)),flush=True)
            with self.subTest(boundary='no-false-missing-intent'):
                self.assertNotEqual(error,'project_dir_move_intent_missing')
            with self.subTest(boundary='normal-busy-or-complete'):
                self.assertTrue(busy or complete,'a normally prepared competing mover must defer or complete')
            with self.subTest(boundary='competing-intent-pointer-preserved'):
                self.assertTrue(unchanged,'A must preserve B durable intent/pointer and prepared project/state files')
        finally:
            if child is not None:
                if child.poll() is None:
                    try:child.stdin.write('release\n');child.stdin.flush()
                    except (BrokenPipeError,OSError):pass
                try:child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill();child.communicate(timeout=5)

    def test_blocked_v4_hot_journal_pointer_refuses_without_rw_recovery(self):
        base=seed_module.base
        helper=base.JournalReaderChecks('runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        class PublishedV4(base.published.MoveCase):
            STATE_PARTS=('.local','state','memvane','3'*32)
            TEMP_DIR=str(Path('/tmp').resolve());TEMP_PREFIX=''
            PROVIDER_EXTRA={'serviceSocketPath':'{state}/runtime/laya-embed.sock'}
        case=helper.compose(PublishedV4)
        mover=importlib.import_module('laya_project_dir_move')
        J=base.J
        self.assertEqual(J.journal_version(case.journal),4)
        with helper.full_authority(case),ExitStack() as locks:
            mover._locks(locks,case.project,case.state,mover._queue_path(case.project,None))
            intent=mover._prepare(case.project,case.state,'rename')
            mover._replace_ledger(case.state,intent['oldLedger'],intent['fencedLedger'])
            self.assertIsNotNone(mover.read_pointer(case.project))
            self.assertEqual(J.select_route(str(case.project),action='recall')['state'],'blocked')
        with closing(sqlite3.connect(case.journal.as_uri()+'?mode=ro',uri=True)) as db:
            original=db.execute('SELECT * FROM selection').fetchall()
        hot_writer=r'''
import os,signal,sqlite3,sys
db=sqlite3.connect(sys.argv[1],isolation_level=None)
db.execute('PRAGMA cache_size=1');db.execute('BEGIN IMMEDIATE')
db.execute('CREATE TABLE synthetic_recovery_hot(x)')
for _ in range(80):db.execute('INSERT INTO synthetic_recovery_hot VALUES(randomblob(4000))')
print('ACTUAL_HOT_CHECKPOINT',flush=True)
os.kill(os.getpid(),signal.SIGKILL)
'''
        hot=subprocess.run([sys.executable,'-B','-I','-c',hot_writer,str(case.journal)],
            capture_output=True,encoding='utf8',timeout=20)
        if hot.returncode!=-9 or hot.stdout.strip()!='ACTUAL_HOT_CHECKPOINT':
            raise RuntimeError('fresh hot writer missed actual SIGKILL checkpoint: '+hot.stderr)
        side=Path(str(case.journal)+'-journal')
        control=case.home/'actual-hot-control';control.mkdir(mode=0o700)
        trial=control/'install-journal.sqlite';trial.write_bytes(case.journal.read_bytes());trial.chmod(0o600)
        trial_side=Path(str(trial)+'-journal');trial_side.write_bytes(side.read_bytes());trial_side.chmod(0o600)
        with closing(sqlite3.connect(trial)) as db:
            if (db.execute('SELECT * FROM selection').fetchall()!=original or
                    db.execute("SELECT 1 FROM sqlite_master WHERE name='synthetic_recovery_hot'").fetchone() is not None):
                raise RuntimeError('actual hot rollback control did not restore original v4')
        if trial_side.exists():raise RuntimeError('actual SQLite did not consume copied hot journal')
        files=[case.journal,side]
        before={str(path):(path.read_bytes(),path.stat()) for path in files}
        def image():
            return {suffix:None if not os.path.lexists(str(case.journal)+suffix) else
                (Path(str(case.journal)+suffix).read_bytes(),Path(str(case.journal)+suffix).stat().st_dev,
                 Path(str(case.journal)+suffix).stat().st_ino)
                for suffix in ('','-journal','-wal','-shm')}
        aux_before=image()
        payload=case.home/'hot-pointer-read.json'
        payload.write_text(json.dumps(dict(home=str(case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
            project=str(case.project),plugin=str(case.plugin),proof=case.proof,journal=str(case.journal))),encoding='utf8')
        payload.chmod(0o600)
        result=subprocess.run([sys.executable,'-B','-I','-c',HOT_READER,str(ROOT),str(payload)],
            capture_output=True,encoding='utf8',timeout=25)
        if result.returncode:raise RuntimeError('fresh hot reader observer failed: '+result.stderr)
        observed=json.loads(result.stdout)
        unchanged=all(path.exists() and path.read_bytes()==before[str(path)][0] for path in files)
        print(json.dumps(dict(case='v4-hot-pointer-readonly',actualHotRollbackControl=True,observed=observed,
            journalAndAuxUnchanged=unchanged)),flush=True)
        self.assertIsNotNone(observed['error']);self.assertFalse(observed['issued'])
        with self.subTest(boundary='rw-open-zero'):self.assertEqual(observed['rwOpens'],[])
        with self.subTest(boundary='hot-bytes-and-nodes'):
            self.assertTrue(unchanged)
            for path in files:self.assertTrue(os.path.samestat(path.stat(),before[str(path)][1]))
        with self.subTest(boundary='all-journal-aux-preserved'):
            self.assertTrue(image()==aux_before,'resolver must preserve hot main/aux bytes and inode identities')

    def test_plain_v5_public_project_move_refuses_before_physical_successor_changes(self):
        seed=seed_module.MoveSealChecks('runTest');self.addCleanup(seed.doCleanups);seed.setUp()
        with seed.authority():seed.reader._evidence(seed.case.state,seed.case.project,seed.case.plugin)
        with seed.ro(seed.case.journal) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='move_read_seals'").fetchone())
        old=seed.case.auto;new=seed.mover.current_folder(seed.case.project)
        self.assertFalse(new.exists())
        def snap():
            return {str(path):(path.read_bytes(),path.stat().st_dev,path.stat().st_ino)
                for root in (seed.case.project,seed.case.state) for path in root.rglob('*')
                if path.is_file() and not path.is_symlink() and not path.name.endswith('.lock')}
        before=snap();folder_node=seed.reader._node(old);error,outcome=None,None
        try:
            with seed.authority():outcome=seed.mover.move(seed.case.project,seed.case.state)
        except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
        print(json.dumps(dict(case='plain-v5-public-successor-safety',actualVersion=5,retainedSealAbsent=True,
            error=error,result=outcome,filesUnchanged=snap()==before,legacyStillExists=old.is_dir(),newExists=new.exists())),flush=True)
        self.assertIsNotNone(error);self.assertIsNone(outcome)
        self.assertTrue(snap()==before,'plain v5 public move must preserve original project/state file bytes and inodes')
        self.assertTrue(old.is_dir());self.assertFalse(new.exists())
        self.assertEqual(seed.reader._node(old),folder_node)

    def test_reserved_move_target_cannot_publish_out_of_order_after_actual_rename(self):
        case=prefix.MovePrefixChecks('runTest');self.addCleanup(case.doCleanups)
        case.check_prefix('anchor-entry',mutation='reserved-generation-ahead')

    def test_missing_state_intent_cannot_make_retained_public_move_report_current(self):
        case=prefix.MovePrefixChecks('runTest');self.addCleanup(case.doCleanups)
        case.check_prefix('anchor-entry',mutation='missing-intent-public')


if __name__=='__main__':unittest.main(verbosity=2)
