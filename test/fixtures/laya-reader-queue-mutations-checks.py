#!/usr/bin/env python3
"""Managed WAL queue mutations and ACK binding; no full cohort claim."""
from contextlib import contextmanager
import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock
import uuid

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('queue_real_writer',ROOT/'test/fixtures/laya-reader-write-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)
def emit(case,**values):print(json.dumps(dict(case=case,**values),sort_keys=True),flush=True)
def image(path):
    return {suffix:None if not os.path.lexists(str(path)+suffix) else
        (None if suffix=='-shm' else Path(str(path)+suffix).read_bytes(),
         tuple(getattr(Path(str(path)+suffix).lstat(),key) for key in ('st_dev','st_ino','st_mode','st_uid','st_nlink')))
        for suffix in ('','-wal','-shm','-journal')}

class QueueMutationChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.WriteChecks('runTest');self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.case,self.seed,self.W=self.fixture.case,self.fixture.seed,self.fixture.W
        self.Q=importlib.import_module('laya_index_claim_queue')
    def probe(self,name,shared=False):
        fd=os.open(self.fixture.lock(name),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            try:fcntl.flock(fd,(fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB)
            except BlockingIOError:return False
            return True
        finally:os.close(fd)
    @contextmanager
    def opened(self):
        with self.fixture.writer() as (base,_component):
            component=self.W.component_for(self.seed.installer,'queue')
            queue=self.Q.ClaimQueue.open(self.case.queue,coordination=component)
            try:yield base,queue,component
            finally:queue.close(deadline=time.monotonic()+1);self.W.drain_pending(deadline=time.monotonic()+1)
    def prep(self,base,queue):
        import laya_index_a as A
        scope=self.case.route['scopeId'];old=base.db.execute('SELECT gen FROM head WHERE scope=?',(scope,)).fetchone()[0]
        body='synthetic actual queue receipt';docs=[('wiki/queue-receipt.md',body,True)]
        manifest=A._digest(scope,[(docs[0][0],hashlib.sha256(body.encode()).hexdigest(),1)])
        ack=queue.enqueue_next(scope,'queue-receipt-generation',manifest,old)
        proof=dict(source_manifest=manifest,claim_digest=ack['claim_digest'],claim_epoch=ack['claim_epoch'])
        self.assertEqual(base.stage(ack['gen'],scope,docs,base=old,**proof),'prepared')
        self.assertEqual(base.publish(old,ack['gen'],ack['claim'],**proof),'published')
        leased_ack=queue.lease(ack['claim'])
        pending=queue.enqueue_next(scope,'queue-pending-generation','b'*64,ack['gen'])
        replan=queue.enqueue_next(scope,'queue-replan-generation','c'*64,ack['gen']);replan=queue.lease(replan['claim'])
        retry=queue.enqueue_next(scope,'queue-retry-generation','d'*64,ack['gen']);retry=queue.lease(retry['claim'])
        fresh=dict(claim=uuid.uuid4().hex,scope=scope,gen='queue-explicit-generation',source_manifest='e'*64,
                   claim_epoch=10000,base_gen=ack['gen'])
        fresh['claim_digest']=self.Q.payload_digest(scope,fresh['gen'],fresh['source_manifest'],fresh['claim_epoch'],fresh['base_gen'])
        actions=[('enqueue_next',lambda:queue.enqueue_next(scope,'queue-next-generation','f'*64,ack['gen'])),
                 ('enqueue',lambda:queue.enqueue(fresh)),('lease',lambda:queue.lease(pending['claim'])),
                 ('conditional_ack',lambda:queue.conditional_ack(leased_ack,leased_ack['lease_token'],base)),
                 ('mark_replan',lambda:queue.mark_replan(replan,replan['lease_token'],'index_stale')),
                 ('release_for_retry',lambda:queue.release_for_retry(retry,retry['lease_token']))]
        return dict(ack=leased_ack,pending=pending,replan=replan,retry=retry,fresh=fresh,actions=actions)
    @staticmethod
    def jobs(queue):return queue.db.execute('SELECT * FROM jobs ORDER BY claim').fetchall()

    def test_six_valid_queue_mutators_wait_before_begin_and_preserve_full_jobs(self):
        with self.opened() as (base,queue,_component):
            prepared=self.prep(base,queue)
            with mock.patch.object(existing,'LOCK_CHILD',existing.LOCK_CHILD.replace('signal.alarm(20)','signal.alarm(60)')), \
                    self.fixture.held('queue-mutation.lock','EX'):
                for name,action in prepared['actions']:
                    with self.subTest(api=name):
                        before=self.jobs(queue);sql=[];error=None;busy=False;queue.db.set_trace_callback(sql.append)
                        try:action()
                        except Exception as exc:error=type(exc).__name__+':'+str(exc);busy=isinstance(exc,self.W.ReaderWindowBusy)
                        finally:queue.db.set_trace_callback(None)
                        after=self.jobs(queue);begins=[s for s in sql if s.strip().upper().startswith('BEGIN')]
                        emit('queue-six-admission',api=name,error=error,busy=busy,begin=begins,fullJobsUnchanged=before==after)
                        with self.subTest(boundary='busy'):self.assertTrue(busy)
                        with self.subTest(boundary='no-begin'):self.assertEqual(begins,[])
                        with self.subTest(boundary='all-job-state-token-outcome-preserved'):self.assertTrue(before==after)
    def test_actual_queue_lifecycle_ack_recovery_replan_retry_and_nonvisible_dml(self):
        with self.opened() as (base,queue,_component):
            p=self.prep(base,queue);cls=type(queue.db);actual=cls.execute;label=[None];observed={};result={}
            def execute(db,sql,*args,**kwargs):
                value=actual(db,sql,*args,**kwargs)
                if db is queue.db and label[0] and label[0] not in observed and sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')):
                    observed[label[0]]=dict(mutationEX=self.probe('queue-mutation.lock'),cutSH=self.probe('publish-cut.lock',True))
                return value
            with mock.patch.object(cls,'execute',new=execute):
                for name,action in p['actions']:label[0]=name;result[name]=action()
            label[0]=None
            lease=result['lease'];recovered=queue.lease(lease['claim'],recover=True)
            self.assertNotEqual(recovered['lease_token'],lease['lease_token'])
            sql=[];queue.db.set_trace_callback(sql.append)
            try:
                lost=queue.conditional_ack(p['ack'],'not-the-current-token',base)
                replay=queue.enqueue(p['fresh'])
            finally:queue.db.set_trace_callback(None)
            emit('actual-queue-lifecycle',observations=observed,ack=result['conditional_ack'],lostLease=lost,
                 replay=replay,earlyCommits=[s for s in sql if s.strip().upper()=='COMMIT'])
            self.assertEqual(set(observed),{name for name,_ in p['actions']})
            for name,item in observed.items():
                with self.subTest(api=name):self.assertFalse(item['mutationEX']);self.assertTrue(item['cutSH'])
            self.assertEqual(result['conditional_ack'],self.Q.ACK);self.assertTrue(result['mark_replan']);self.assertTrue(result['release_for_retry'])
            self.assertEqual(lost,self.Q.LEASE_LOST);self.assertEqual(replay,'pending')
            self.assertFalse(any(s.strip().upper()=='COMMIT' for s in sql))
            self.assertFalse(queue.release_for_retry(lease,lease['lease_token']))
            self.assertTrue(queue.release_for_retry(recovered,recovered['lease_token']))

    def test_replaced_actual_queue_main_refuses_write_before_sql_and_preserves_replacement(self):
        with self.opened() as (base,queue,_component):
            p=self.prep(base,queue);replacement=self.case.state/'queue-valid-replacement.sqlite';saved=self.case.state/'queue-held-original.sqlite'
            copy=sqlite3.connect(replacement)
            try:
                queue.db.backup(copy);copy.execute('PRAGMA journal_mode=DELETE')
                self.assertEqual(self.Q.ClaimQueue._check_database(copy),self.case.route['dbIncarnation'])
            finally:copy.close()
            replacement.chmod(0o600);os.rename(self.case.queue,saved);os.replace(replacement,self.case.queue)
            before=image(self.case.queue);sql=[];error=None
            try:
                queue.db.set_trace_callback(sql.append)
                try:queue.enqueue(p['fresh'])
                except (ValueError,OSError,RuntimeError) as exc:error=type(exc).__name__+':'+str(exc)
                finally:queue.db.set_trace_callback(None)
                after=image(self.case.queue)
                emit('actual-queue-main-binding-swap',error=error,sql=sql,replacementPreserved=before==after)
                self.assertIsNotNone(error);self.assertEqual(sql,[])
                self.assertTrue(before==after,'replacement main/WAL/aux identities and bytes must remain untouched')
            finally:
                self.case.queue.unlink();os.rename(saved,self.case.queue)

    def test_actual_ack_receipt_decision_cannot_authorize_swapped_base_path(self):
        with self.opened() as (base,queue,_component):
            p=self.prep(base,queue);job=p['ack'];before=self.jobs(queue)
            replacement=self.case.state/'base-valid-replacement.sqlite';saved=self.case.state/'base-held-original.sqlite'
            copy=sqlite3.connect(replacement)
            try:base.db.backup(copy);copy.execute('PRAGMA journal_mode=DELETE')
            finally:copy.close()
            replacement.chmod(0o600);actual=self.Q.ClaimQueue.decide;decisions=[];swapped=False
            def decide(index,current_job):
                nonlocal swapped
                result=actual(index,current_job);decisions.append(result)
                if result==self.Q.ACK and not swapped:
                    os.rename(self.case.db,saved);os.replace(replacement,self.case.db);swapped=True
                return result
            try:
                with mock.patch.object(self.Q.ClaimQueue,'decide',new=staticmethod(decide)):
                    result=queue.conditional_ack(job,job['lease_token'],base)
                after=self.jobs(queue)
                emit('actual-ack-base-swap-after-decision',actualDecisions=decisions,result=result,swapped=swapped,jobsUnchanged=before==after)
                if not swapped:raise RuntimeError('actual matching receipt/ACK decision prerequisite missing')
                self.assertEqual(result,self.Q.DEFER);self.assertTrue(before==after)
            finally:
                if swapped:self.case.db.unlink();os.rename(saved,self.case.db)

    def test_managed_queue_refuses_unmanaged_index_even_with_matching_actual_receipt(self):
        from laya_index_a import LayaIndexA
        with self.opened() as (base,queue,_component):
            p=self.prep(base,queue);before=self.jobs(queue);unmanaged=LayaIndexA(self.case.db)
            try:result=queue.conditional_ack(p['ack'],p['ack']['lease_token'],unmanaged)
            finally:unmanaged.close()
            emit('managed-ack-unmanaged-index',result=result,jobsUnchanged=self.jobs(queue)==before,
                 foreignActualInstallCoverage='MISSING: separate actual coordinated install remains required')
            self.assertEqual(result,self.Q.DEFER);self.assertTrue(self.jobs(queue)==before)

    def test_managed_delete_binding_write_is_explicitly_unsupported_before_begin(self):
        from laya_index_capacity import GenerationBindingStore
        with self.fixture.writer() as (_base,_component):
            component=self.W.component_for(self.seed.installer,'binding')
            store=GenerationBindingStore(self.case.store,scope=self.case.route['scopeId'],
                db_incarnation=self.case.route['dbIncarnation'],roots={self.case.WIKI:str(self.case.auto/'wiki')},coordination=component)
            try:
                self.assertEqual(store.db.execute('PRAGMA journal_mode').fetchone(),('delete',))
                sql=[];entered=False;error=None;store.db.set_trace_callback(sql.append)
                try:
                    with self.W.write_transaction(store._connection_owner,visible=False,deadline=time.monotonic()+.05):entered=True
                except ValueError as exc:error=str(exc)
                finally:store.db.set_trace_callback(None)
                begins=[s for s in sql if s.strip().upper().startswith('BEGIN')]
                emit('managed-delete-explicit-refusal',error=error,entered=entered,begin=begins)
                self.assertIsNotNone(error);self.assertTrue('unsupported' in error or 'journal' in error)
                self.assertFalse(entered);self.assertEqual(begins,[])
            finally:store.close(deadline=time.monotonic()+1)

if __name__=='__main__':unittest.main(verbosity=2)
