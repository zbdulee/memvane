#!/usr/bin/env python3
"""Managed catalog mutation admission and short visible state transitions."""
from contextlib import contextmanager
import importlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('catalog_queue_helpers',ROOT/'test/fixtures/laya-reader-queue-mutations-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

CATALOG_CHILD=r'''
from contextlib import ExitStack,nullcontext,contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import importlib.util,json,os,signal,sys,time
root,payload,mode=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
p=json.loads(payload.read_text());sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime'];signal.alarm(30)
spec=importlib.util.spec_from_file_location('catalog_crash_seed',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as R,laya_reader_lease as L,laya_reader_windows as W
import laya_project_dir_move as mover,laya_state_move
from laya_source_catalog import LayaSourceCatalog
helper=base.JournalReaderChecks('runTest');case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
project,state=Path(p['project']),Path(p['state'])
assert len(R._issued)==0
with helper.full_authority(case,R,L,W,mover),mock.patch.object(laya_state_move,'_provider_lane',lambda:nullcontext()):
    installer=R.seal_current_reader(state,project,executing_root=case.plugin)
    with ExitStack() as locks:
        mover._locks(locks,project,state,mover._queue_path(project,None))
        W.prepare_windows(installer);component=W.component_for(installer,'catalog')
        catalog=LayaSourceCatalog(Path(p['catalog']),p['roots'],p['scope'],wiki_only=True,coordination=component)
        try:
            if mode=='crash':
                assert catalog.request_scan()['status']=='started'
                actual=W.write_transaction
                @contextmanager
                def stop(owner,*,visible,deadline):
                    if visible:
                        scan=catalog._scan_row(catalog._pointer('active'))
                        print(json.dumps(dict(event='terminal-private-commit',phase=scan['phase'],current=catalog._pointer('current'),
                            active=scan['seq'],cursor=scan['cursor'],nativeSQLActive=catalog.db.in_transaction)),flush=True)
                        os.kill(os.getpid(),signal.SIGKILL)
                    with actual(owner,visible=visible,deadline=deadline) as tx:yield tx
                with mock.patch.object(W,'write_transaction',stop):catalog.step(max_files=100,max_stats=100)
                raise RuntimeError('actual terminal private commit was not reached')
            result=catalog.step(max_files=100,max_stats=100)
            print(json.dumps(dict(event='fresh-resume',result=result,scan_id=catalog.require_complete().scan_id)),flush=True)
        finally:catalog.close(deadline=time.monotonic()+1);W.drain_pending(deadline=time.monotonic()+1)
'''

READ_CATALOG=r'''
import json,sqlite3,sys
from pathlib import Path
db=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True,isolation_level=None)
try:
    db.execute('BEGIN')
    current=db.execute('SELECT seq FROM current WHERE id=1').fetchone()[0]
    active=db.execute('SELECT seq FROM active WHERE id=1').fetchone()[0]
    row=db.execute('SELECT phase,cursor,raw_digest FROM scans WHERE seq=?',(active,)).fetchone()
    complete=db.execute('SELECT scan_id,raw_digest,files FROM scans WHERE seq=?',(current,)).fetchone()
    print(json.dumps(dict(current=current,active=active,scan=row,complete=complete)))
finally:db.close()
'''

class CatalogChecks(existing.QueueMutationChecks):
    @contextmanager
    def catalog(self):
        with self.fixture.writer() as (_base,_component):
            self.C=importlib.import_module('laya_source_catalog')
            component=self.W.component_for(self.seed.installer,'catalog')
            catalog=self.C.LayaSourceCatalog(self.case.catalog,{self.case.WIKI:str(self.case.auto/'wiki')},
                self.case.route['scopeId'],wiki_only=True,coordination=component)
            try:yield catalog,component
            finally:catalog.close(deadline=time.monotonic()+1);self.W.drain_pending(deadline=time.monotonic()+1)
    @staticmethod
    def rows(catalog):
        return {name:catalog.db.execute('SELECT * FROM '+name+' ORDER BY 1,2').fetchall()
                for (name,) in catalog.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    def test_four_catalog_writes_wait_on_mutation_before_sql_and_preserve_rows(self):
        with self.catalog() as (catalog,_component):
            snapshot=catalog.require_complete();self.assertEqual(catalog.request_scan()['status'],'started')
            proof,_rows=catalog.pending_hashed((frozenset(),frozenset()))
            self.assertIsNotNone(proof)
            actions=[('request_scan',catalog.request_scan),('step',lambda:catalog.step(max_files=1)),
                ('pending_fill',lambda:catalog.set_pending_fill_progress(proof,'a'*64,0)),
                ('vector_fill',lambda:catalog.set_vector_fill(snapshot,'a'*64,'',False))]
            with self.fixture.held('catalog-mutation.lock','EX'):
                for name,action in actions:
                    before=self.rows(catalog);sql=[];catalog.db.set_trace_callback(sql.append)
                    try:result=action()
                    finally:catalog.db.set_trace_callback(None)
                    unchanged=before==self.rows(catalog);begin=[s for s in sql if s.startswith('BEGIN')]
                    existing.emit('catalog-four-mutation-admission',api=name,result=result,begin=begin,unchanged=unchanged)
                    with self.subTest(api=name):
                        self.assertEqual(begin,[]);self.assertTrue(unchanged)
                        if isinstance(result,dict):self.assertEqual(result['status'],'busy')
                        else:self.assertFalse(result)

    def test_request_scan_cannot_change_visible_active_while_cut_reader_is_present(self):
        with self.catalog() as (catalog,_component):
            before=(catalog._pointer('current'),catalog._pointer('active'))
            with self.fixture.held('publish-cut.lock','SH'):
                result=catalog.request_scan()
                after=(catalog._pointer('current'),catalog._pointer('active'))
                try:servable=catalog.require_servable()
                except Exception as exc:servable=str(exc)
            existing.emit('catalog-request-visible-cut',result=result,before=before,after=after)
            self.assertEqual(result['status'],'busy');self.assertEqual(before,after)
            self.assertEqual(servable,catalog.require_servable())
            self.assertEqual(catalog.request_scan()['status'],'started')

    def test_publish_aggregation_and_stale_cleanup_stay_outside_cut(self):
        with self.catalog() as (catalog,_component):
            original=catalog.require_complete();events=[];changes=catalog._changes;drop=catalog._drop_scan
            def counted(*args):events.append(('aggregate',self.probe('publish-cut.lock',True)));return changes(*args)
            def dropped(*args):events.append(('cleanup',self.probe('publish-cut.lock',True)));return drop(*args)
            with mock.patch.object(catalog,'_changes',counted),mock.patch.object(catalog,'_drop_scan',dropped):
                self.assertEqual(catalog.request_scan()['status'],'started')
                result=catalog.step(max_files=100,max_stats=100)
                self.assertEqual(result['status'],'complete')
                self.assertNotEqual(catalog.require_complete().scan_id,original.scan_id)
                self.assertEqual(catalog.request_scan()['status'],'started')
            existing.emit('catalog-expensive-work-outside-cut',events=events)
            self.assertTrue(any(name=='aggregate' for name,_ok in events))
            self.assertTrue(any(name=='cleanup' for name,_ok in events))
            self.assertTrue(all(ok for _name,ok in events))

    def test_blocked_transition_waits_for_cut_and_keeps_previous_complete_until_then(self):
        with self.catalog() as (catalog,_component):
            original=catalog.require_complete()
            path=self.case.auto/'wiki'/'invalid-utf8.md';path.write_bytes(b'\xff\xfe')
            self.assertEqual(catalog.request_scan()['status'],'started')
            with self.fixture.held('publish-cut.lock','SH'):
                result=catalog.step(max_files=100,max_stats=100)
                phase=catalog._scan_row(catalog._pointer('active'))['phase']
                try:complete=catalog.require_complete()
                except Exception as exc:complete=str(exc)
            existing.emit('catalog-blocked-cut',result=result,phase=phase,previousComplete=complete==original)
            self.assertEqual(result['status'],'busy');self.assertIn(phase,self.C.PENDING_PHASES)
            self.assertEqual(complete,original)
            self.assertEqual(catalog.step(max_files=100,max_stats=100)['status'],'blocked')
            with self.assertRaises(Exception):catalog.require_complete()

    def test_external_coalesce_between_private_commit_and_cut_is_not_lost(self):
        with self.catalog() as (catalog,component):
            old=catalog.require_complete();self.assertEqual(catalog.request_scan()['status'],'started')
            other=self.C.LayaSourceCatalog(catalog.path,catalog.roots,catalog.scope,wiki_only=True,coordination=component)
            actual=self.W.write_transaction;injections=[]
            @contextmanager
            def between(owner,*,visible,deadline):
                if owner is catalog._connection_owner and visible and not injections:
                    injections.append(other.request_scan())
                with actual(owner,visible=visible,deadline=deadline) as tx:yield tx
            try:
                with mock.patch.object(self.W,'write_transaction',between):result=catalog.step(max_files=100,max_stats=100)
                pending=catalog._scan_row(catalog._pointer('active'))
                self.assertEqual(catalog.require_complete(),old)
                self.assertEqual(result['status'],'busy');self.assertEqual(injections[0]['status'],'coalesced')
                self.assertEqual(pending['rerequested'],1)
                completed=catalog.step(max_files=100,max_stats=100)
                self.assertEqual(completed['status'],'complete');self.assertIn('next_scan_id',completed)
                active=catalog._scan_row(catalog._pointer('active'))
                self.assertEqual(active['scan_id'],completed['next_scan_id'])
                with self.assertRaises(Exception):catalog.require_servable()
                self.assertEqual(catalog.step(max_files=100,max_stats=100)['status'],'complete')
                catalog.require_servable()
                existing.emit('catalog-coalesce-gap',first=result['status'],injections=injections,nextScan=completed['next_scan_id'])
            finally:other.close(deadline=time.monotonic()+1)

    def test_sigkill_after_terminal_private_commit_preserves_old_snapshot_then_fresh_resume(self):
        with self.seed.prepared():
            self.W.prepare_windows(self.seed.installer)
        path=self.case.auto/'wiki'/'terminal-crash.md';path.write_text('# Terminal crash\nsynthetic complete catalog boundary\n',encoding='utf8')
        payload=self.case.state/'catalog-crash-payload.json'
        payload.write_text(json.dumps(dict(home=str(self.case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
            project=str(self.case.project),state=str(self.case.state),plugin=str(self.case.plugin),proof=self.case.proof,
            catalog=str(self.case.catalog),roots={self.case.WIKI:str(self.case.auto/'wiki')},scope=self.case.route['scopeId'])))
        payload.chmod(0o600)
        def read():
            result=subprocess.run([sys.executable,'-B','-I','-c',READ_CATALOG,str(self.case.catalog)],
                capture_output=True,encoding='utf8',timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        before=read()
        child=subprocess.run([sys.executable,'-B','-I','-c',CATALOG_CHILD,str(ROOT),str(payload),'crash'],
            capture_output=True,encoding='utf8',timeout=35)
        self.assertEqual(child.returncode,-signal.SIGKILL,child.stdout+child.stderr)
        checkpoint=json.loads(child.stdout.splitlines()[-1]);after=read()
        self.assertEqual(checkpoint['event'],'terminal-private-commit');self.assertFalse(checkpoint['nativeSQLActive'])
        self.assertEqual(after['complete'],before['complete']);self.assertNotEqual(after['current'],after['active'])
        self.assertEqual(after['scan'][0],'verify_files');self.assertIsNone(after['scan'][2])
        resumed=subprocess.run([sys.executable,'-B','-I','-c',CATALOG_CHILD,str(ROOT),str(payload),'resume'],
            capture_output=True,encoding='utf8',timeout=35)
        self.assertEqual(resumed.returncode,0,resumed.stdout+resumed.stderr)
        result=json.loads(resumed.stdout.splitlines()[-1]);final=read()
        self.assertEqual(result['result']['status'],'complete');self.assertEqual(final['current'],after['active'])
        self.assertNotEqual(final['complete'][1],before['complete'][1])
        existing.emit('catalog-terminal-real-sigkill',actualExit=child.returncode,checkpoint=checkpoint,
            oldCompleteRetained=True,freshResume=result['result']['status'],newDigest=True,coldCapabilityClaim=False)

    def test_repeated_settled_steps_do_not_repeat_rolled_back_stale_deletes(self):
        with self.catalog() as (catalog,_component):
            old=catalog._pointer('current');self.assertEqual(catalog.request_scan()['status'],'started')
            self.assertEqual(catalog.step(max_files=100,max_stats=100)['status'],'complete')
            self.assertNotEqual(catalog._pointer('current'),old)
            traces=[];results=[]
            for _ in range(2):
                sql=[];catalog.db.set_trace_callback(sql.append)
                try:results.append(catalog.step()['status'])
                finally:catalog.db.set_trace_callback(None)
                traces.append([s for s in sql if s.startswith('DELETE FROM')])
            existing.emit('catalog-settled-no-repeat-deletes',results=results,deletes=traces)
            self.assertEqual(traces[1],[])

    def test_visible_begin_deadline_returns_busy_after_rollback(self):
        self.deadline_case(False)

    def test_visible_deadline_with_failed_rollback_remains_error_and_pending(self):
        self.deadline_case(True)

    def deadline_case(self,fail_rollback):
        with self.catalog() as (catalog,_component):
            before=catalog._pointer('current');catalog.request_scan()
            cls=type(catalog.db);execute,rollback=cls.execute,cls.rollback;actual=self.W.write_transaction
            visible=[False];delays=[];attempts=[];error=None;result=None
            @contextmanager
            def observed(owner,*,visible: bool,deadline):
                active=owner is catalog._connection_owner and visible
                self_visible[0]=active
                try:
                    with actual(owner,visible=visible,deadline=deadline) as tx:yield tx
                finally:self_visible[0]=False
            self_visible=visible
            def delayed(db,sql,*args,**kwargs):
                cursor=execute(db,sql,*args,**kwargs)
                if db is catalog.db and visible[0] and sql=='BEGIN IMMEDIATE':
                    delays.append(db.in_transaction);time.sleep(.07)
                return cursor
            def rollback_once(db):
                if db is catalog.db and visible[0]:
                    attempts.append(True)
                    if fail_rollback and len(attempts)==1:raise RuntimeError('synthetic_catalog_rollback_failure')
                return rollback(db)
            with mock.patch.object(self.W,'write_transaction',observed),mock.patch.object(cls,'execute',delayed),mock.patch.object(cls,'rollback',rollback_once):
                try:result=catalog.step(max_files=100,max_stats=100)
                except Exception as exc:error=str(exc)
            state=dict(error=error,result=result,delays=delays,pending=self.W.pending_count(),active=catalog.db.in_transaction,
                pointerUnchanged=catalog._pointer('current')==before,cutSH=self.probe('publish-cut.lock',True))
            if fail_rollback:self.W.drain_pending(deadline=time.monotonic()+1)
            existing.emit('catalog-visible-native-deadline',failedRollback=fail_rollback,**state)
            self.assertEqual(delays,[True]);self.assertTrue(state['pointerUnchanged'])
            if fail_rollback:
                self.assertIsNotNone(error);self.assertIsNone(result);self.assertEqual(state['pending'],1)
                self.assertTrue(state['active']);self.assertFalse(state['cutSH'])
            else:
                self.assertIsNone(error);self.assertEqual(result['status'],'busy')
                self.assertEqual(state['pending'],0);self.assertFalse(state['active']);self.assertTrue(state['cutSH'])

if __name__=='__main__':
    names=[name for name in CatalogChecks.__dict__ if name.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(CatalogChecks(name) for name in names))
    raise SystemExit(not result.wasSuccessful())
