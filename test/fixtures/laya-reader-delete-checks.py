#!/usr/bin/env python3
"""Actual managed binding INSERT journal lifetime and conflict recovery."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('delete_queue_helpers',ROOT/'test/fixtures/laya-reader-queue-mutations-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

class DeleteChecks(existing.QueueMutationChecks):
    # Reuse fixture plumbing without inheriting the queue test cases below.
    @contextmanager
    def binding(self):
        import laya_index_capacity as C
        with self.fixture.writer() as (base,_component):
            scope=self.case.route['scopeId']
            component=self.W.component_for(self.seed.installer,'binding')
            kwargs=dict(scope=scope,db_incarnation=self.case.route['dbIncarnation'],
                roots={self.case.WIKI:str(self.case.auto/'wiki')},coordination=component)
            store=C.GenerationBindingStore(self.case.store,**kwargs)
            try:
                gen=base.db.execute('SELECT gen FROM head WHERE scope=?',(scope,)).fetchone()[0]
                original=store.get(scope,gen,self.case.route['dbIncarnation'])
                self.assertIsNotNone(original)
                yield C,store,C.bind_generation(original,nonce='a'*32),kwargs
            finally:
                if store._connection_owner.db is not None:store.close(deadline=time.monotonic()+1)
                self.W.drain_pending(deadline=time.monotonic()+1)

    def test_insert_and_replay_prove_real_journal_creation_deletion_and_all_locks(self):
        with self.binding() as (C,store,binding,_kwargs):
            journal=Path(str(store.path)+'-journal');events=[];cls=type(store.db)
            execute,commit=cls.execute,cls.commit
            def observe(db,sql,*args,**kwargs):
                cursor=execute(db,sql,*args,**kwargs)
                if db is store.db and sql==C._INSERT_SQL:
                    events.append(dict(journal=journal.exists(),active=db.in_transaction,
                        mutationEX=self.probe('binding-mutation.lock'),
                        physicalSH=self.probe('binding-physical.lock',True),
                        cutSH=self.probe('publish-cut.lock',True)))
                return cursor
            commits=[]
            def finish(db):
                if db is store.db:commits.append((db.in_transaction,journal.exists()))
                result=commit(db)
                if db is store.db:commits.append((db.in_transaction,journal.exists()))
                return result
            with mock.patch.object(cls,'execute',observe),mock.patch.object(cls,'commit',finish):
                try:result=store.put(binding)
                except Exception as exc:self.fail('actual managed DELETE put failed: '+str(exc))
            replay=store.put(binding)
            existing.emit('delete-insert-replay',result=result['status'],replay=replay['status'],events=events,commits=commits)
            self.assertEqual(result['status'],'stored');self.assertEqual(replay['status'],'exists')
            self.assertEqual(events,[dict(journal=True,active=True,mutationEX=False,physicalSH=False,cutSH=False)])
            self.assertEqual(commits,[(True,True),(False,False)])
            self.assertEqual(store.get(binding.scope,binding.generation,binding.db_incarnation),binding)
            self.assertFalse(journal.exists());self.assertTrue(self.probe('binding-physical.lock'))

    def test_before_commit_fault_rolls_back_actual_row_and_deletes_owned_journal(self):
        with self.binding() as (C,store,binding,_kwargs):
            error=None
            try:store.put(binding,fault_at='before_commit')
            except Exception as exc:error=str(exc)
            row=store.get(binding.scope,binding.generation,binding.db_incarnation)
            existing.emit('delete-before-commit-fault',error=error,absent=row is None,active=store.db.in_transaction)
            self.assertIn('binding_before_commit',error or '')
            self.assertIsNone(row);self.assertFalse(store.db.in_transaction)
            self.assertFalse(Path(str(store.path)+'-journal').exists())

    def test_observed_journal_swap_never_commits_or_rolls_back_until_original_restored(self):
        with self.binding() as (C,store,binding,kwargs):
            journal=Path(str(store.path)+'-journal');saved=journal.with_name(journal.name+'.held')
            cls=type(store.db);execute,commit,rollback=cls.execute,cls.commit,cls.rollback
            calls=[];swapped=[];error=None
            def swap(db,sql,*args,**kw):
                cursor=execute(db,sql,*args,**kw)
                if db is store.db and sql==C._INSERT_SQL:
                    journal.rename(saved);journal.write_bytes(b'foreign synthetic journal');journal.chmod(0o600);swapped.append(True)
                return cursor
            def counted_commit(db):calls.append('commit');return commit(db)
            def counted_rollback(db):calls.append('rollback');return rollback(db)
            try:
                with mock.patch.object(cls,'execute',swap),mock.patch.object(cls,'commit',counted_commit),mock.patch.object(cls,'rollback',counted_rollback):
                    try:store.put(binding)
                    except Exception as exc:error=str(exc)
                    if swapped:
                        self.assertTrue(store.db.in_transaction);self.assertEqual(calls,[])
                        self.assertEqual(journal.read_bytes(),b'foreign synthetic journal')
                        self.assertFalse(self.probe('binding-physical.lock',True))
                        with self.assertRaises(Exception):self.W.drain_pending(deadline=time.monotonic()+1)
                        self.assertEqual(calls,[])
                        journal.unlink();saved.rename(journal)
                        self.W.drain_pending(deadline=time.monotonic()+1)
                existing.emit('delete-journal-swap',swapped=swapped,error=error,calls=calls,pending=self.W.pending_count())
                self.assertEqual(swapped,[True]);self.assertIsNotNone(error);self.assertEqual(calls,['rollback'])
                self.assertIsNone(store._connection_owner.db)
                reopened=C.GenerationBindingStore(self.case.store,**kwargs)
                try:self.assertIsNone(reopened.get(binding.scope,binding.generation,binding.db_incarnation))
                finally:reopened.close(deadline=time.monotonic()+1)
            finally:
                if saved.exists():
                    if journal.exists():journal.unlink()
                    saved.rename(journal)

    def test_external_private_journal_after_begin_is_never_adopted_or_deleted(self):
        with self.binding() as (C,store,binding,_kwargs):
            journal=Path(str(store.path)+'-journal');cls=type(store.db)
            execute,rollback,commit=cls.execute,cls.rollback,cls.commit;events=[];created=[];error=None
            def foreign(db,sql,*args,**kwargs):
                cursor=execute(db,sql,*args,**kwargs)
                if db is store.db and sql=='BEGIN IMMEDIATE':
                    journal.write_bytes(b'pre-insert foreign journal');journal.chmod(0o600);created.append(True)
                if db is store.db and sql==C._INSERT_SQL:events.append('insert')
                return cursor
            def rb(db):events.append('rollback');return rollback(db)
            def cm(db):events.append('commit');return commit(db)
            try:
                with mock.patch.object(cls,'execute',foreign),mock.patch.object(cls,'rollback',rb),mock.patch.object(cls,'commit',cm):
                    try:store.put(binding)
                    except Exception as exc:error=str(exc)
                    if created:
                        self.assertEqual(journal.read_bytes(),b'pre-insert foreign journal');self.assertEqual(events,[])
                        self.assertTrue(store.db.in_transaction)
                        journal.unlink()
                        self.W.drain_pending(deadline=time.monotonic()+1)
                existing.emit('delete-pre-insert-foreign',created=created,error=error,events=events)
                self.assertEqual(created,[True]);self.assertIsNotNone(error);self.assertEqual(events,['rollback'])
            finally:
                if journal.exists() and journal.read_bytes()==b'pre-insert foreign journal':journal.unlink()

    def test_physical_reader_or_publish_reader_blocks_binding_before_first_begin(self):
        with self.binding() as (_C,store,binding,_kwargs):
            for name in ('binding-physical.lock','publish-cut.lock'):
                with self.subTest(lock=name),self.fixture.held(name,'SH'):
                    sql=[];error=None;store.db.set_trace_callback(sql.append)
                    try:store.put(binding)
                    except Exception as exc:error=str(exc)
                    finally:store.db.set_trace_callback(None)
                    begins=[s for s in sql if s.startswith('BEGIN')]
                    existing.emit('delete-lock-admission',lock=name,error=error,begin=begins)
                    self.assertIsNotNone(error);self.assertEqual(begins,[])

    def test_post_native_commit_conflict_reports_error_without_claiming_rollback(self):
        with self.binding() as (C,store,binding,kwargs):
            journal=Path(str(store.path)+'-journal');cls=type(store.db)
            commit,rollback=cls.commit,cls.rollback;events=[];error=None
            def completed(db):
                result=commit(db)
                if db is store.db:
                    events.append(('committed',db.in_transaction))
                    journal.write_bytes(b'post-commit foreign journal');journal.chmod(0o600)
                return result
            def rb(db):events.append(('rollback',db.in_transaction));return rollback(db)
            try:
                with mock.patch.object(cls,'commit',completed),mock.patch.object(cls,'rollback',rb):
                    try:store.put(binding)
                    except Exception as exc:error=str(exc)
                    if events:
                        self.assertFalse(store.db.in_transaction)
                        self.assertEqual(journal.read_bytes(),b'post-commit foreign journal')
                        with self.assertRaises(Exception):self.W.drain_pending(deadline=time.monotonic()+1)
                        journal.unlink()
                        self.W.drain_pending(deadline=time.monotonic()+1)
                self.assertIsNotNone(error);self.assertEqual(events,[('committed',False)])
                self.assertIsNone(store._connection_owner.db)
                reopened=C.GenerationBindingStore(self.case.store,**kwargs)
                try:self.assertEqual(reopened.get(binding.scope,binding.generation,binding.db_incarnation),binding)
                finally:reopened.close(deadline=time.monotonic()+1)
                existing.emit('delete-post-commit-error',error=error,events=events,durable=True)
            finally:
                if journal.exists() and journal.read_bytes()==b'post-commit foreign journal':journal.unlink()

# Only the DELETE contracts; inherited queue checks have their own wrapper.
if __name__=='__main__':
    names=[name for name in DeleteChecks.__dict__ if name.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(DeleteChecks(name) for name in names))
    raise SystemExit(not result.wasSuccessful())
