#!/usr/bin/env python3
"""Inactive normal-writer admission; actual warm identity proof, no RW opens."""
from contextlib import ExitStack
import gc
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('writer_scope_seed', ROOT/'test/fixtures/laya-writer-journal-proof-checks.py')
existing = importlib.util.module_from_spec(spec); spec.loader.exec_module(existing)

def emit(label, **values):
    print(json.dumps(dict(case=label, **values), sort_keys=True), flush=True)

class WriterScopeChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.WriterJournalProofChecks('runTest')
        self.addCleanup(self.fixture.doCleanups); self.fixture.setUp()
        self.F = self.fixture.fixture
        self.case, self.seed = self.fixture.case, self.fixture.seed
        self.P, self.W, self.J = self.fixture.P, self.fixture.W, self.fixture.J
        self.queue, self.sync = self.fixture.queue, self.fixture.sync
        self.plan = self.F.fixture.plan

    def api(self):
        if importlib.util.find_spec('laya_writer_scope') is None:
            emit('normal-writer-feature-absence', actualV5=True, actualManagedProof=True)
            self.fail('normal writer admission absent after owned journal proof baseline')
        module = importlib.import_module('laya_writer_scope')
        for name in ('normal_writer_scope', 'validate_normal_writer'):
            self.assertTrue(callable(getattr(module, name, None)), name)
        return module

    def scope(self, module, owner, deadline=None):
        return module.normal_writer_scope(self.plan, queue_owner=owner,
            deadline=time.monotonic()+20 if deadline is None else deadline)

    def test_worker_scope_survives_long_work_with_bounded_operation_deadline(self):
        with self.F.warm(), self.seed.authority(), self.sync.sync_owner(self.queue) as owner:
            module = self.api()
            scope = module.worker_writer_scope(self.plan, queue_owner=owner, operation_timeout=2)
            with scope as writer:
                later = time.monotonic() + 600
                with mock.patch.object(module, 'time', SimpleNamespace(monotonic=lambda: later)):
                    module.validate_normal_writer(writer)
                    self.assertEqual(scope.operation_deadline(), later + 2)
                    with self.sync.sync_owner(self.queue) as other:
                        self.assertIsNone(other)
            self.assertTrue(self.F.fixture.probe('reader-lifetime.lock', 'EX'))

    def test_stale_reader_head_does_not_block_separate_normal_admission(self):
        with self.F.warm(), self.seed.authority():
            # warm() commits an exclusion without a base revision receipt;
            # current reader sealing refuses it and cannot repair it.
            with self.assertRaisesRegex(ValueError, 'reader_base_revision_unproved'):
                self.seed.reader.seal_current_reader(self.case.state, self.case.project,
                    executing_root=self.case.plugin)
            module = self.api(); calls = []
            import laya_move_read_seal as mover
            before = len(mover._scopes)
            with self.sync.sync_owner(self.queue) as owner:
                context = self.scope(module, owner)
                with self.fixture.trace(calls): writer = context.__enter__()
                module.validate_normal_writer(writer)
                self.assertEqual(writer.install_id, self.plan.install_id)
                self.assertEqual(writer.queue_path, self.queue)
                self.assertEqual(len(mover._scopes), before)
                with self.assertRaises(ValueError): mover._owner()
                with self.assertRaises(ValueError): self.seed.reader._installer(writer)
                # Normal admission never gains preparation or mover authority.
                with self.assertRaises(ValueError): self.W.prepare_windows(writer)
                del context; gc.collect()
                module.validate_normal_writer(writer)
                self.assertFalse(self.F.fixture.probe('reader-lifetime.lock', 'EX'))
                with self.sync.sync_owner(self.queue) as busy: self.assertIsNone(busy)
                module._record(writer)['context'].__exit__(None, None, None)
                with self.assertRaises(ValueError): module.validate_normal_writer(writer)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(c['managed'] for c in calls))
            self.assertTrue(self.F.fixture.probe('reader-lifetime.lock', 'EX'))
            emit('normal-writer-separate-admission', contentConnects=calls,
                 readerStale=True, moverRegistered=False, actualWorkerConnected=False)

    def test_failed_proof_close_issues_nothing_and_retains_original_windows_until_drain(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            owner = owners.enter_context(self.sync.sync_owner(self.queue))
            module = self.api()
            readers = importlib.import_module('laya_writer_proof_reader')
            before = len(module._issued); blocked = [True]
            native = self.W._OwnedSQLite._native_close_locked
            def close(db):
                if blocked[0]: raise sqlite3.OperationalError('normal-admission-native-close-failure')
                return native(db)
            try:
                with mock.patch.object(self.W._OwnedSQLite, '_native_close_locked', new=close):
                    with self.assertRaises((ValueError, RuntimeError, sqlite3.Error)):
                        with self.scope(module, owner): self.fail('failed proof cannot issue writer')
                    owners.close(); gc.collect()
                    self.assertEqual(len(module._issued), before)
                    self.assertGreater(readers.pending_proof_readers(), 0)
                    self.assertFalse(self.F.fixture.probe('base-physical.lock', 'SH'))
                    with self.sync.sync_owner(self.queue) as busy: self.assertIsNone(busy)
                    blocked[0] = False
                    self.assertEqual(readers.drain_proof_readers(deadline=time.monotonic()+2), 0)
                with self.sync.sync_owner(self.queue) as successor: self.assertIsNotNone(successor)
                emit('normal-writer-failed-proof-close', issued=0, retainedUntilNativeDrain=True)
            finally:
                blocked[0] = False
                readers.drain_proof_readers(deadline=time.monotonic()+2)

    def test_foreign_exit_does_not_revoke_creator_and_expired_owner_rejects_authority(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            owner = owners.enter_context(self.sync.sync_owner(self.queue))
            module = self.api(); context = self.scope(module, owner)
            writer = context.__enter__(); errors = []
            try:
                def foreign():
                    try: context.__exit__(None, None, None)
                    except (ValueError, RuntimeError) as exc: errors.append(str(exc))
                thread = threading.Thread(target=foreign); thread.start(); thread.join(3)
                self.assertFalse(thread.is_alive()); self.assertEqual(len(errors), 1)
                module.validate_normal_writer(writer)
                owners.close()
                with self.assertRaises(ValueError): module.validate_normal_writer(writer)
                with self.sync.sync_owner(self.queue) as busy: self.assertIsNone(busy)
            finally: context.__exit__(None, None, None)
            with self.sync.sync_owner(self.queue) as successor: self.assertIsNotNone(successor)
            emit('normal-writer-creator-expiry', foreignExitRejected=True, successorAfterCreatorCleanup=True)

    def test_unapproved_or_wrong_executing_proof_cannot_issue(self):
        with self.F.warm(), self.seed.authority(), self.sync.sync_owner(self.queue) as owner:
            module = self.api(); original = self.J.verify_selected_route_for_write
            for field, value in (('productRouteAllowed', False), ('executingPluginRoot', None),
                                 ('selectionEpoch', -1)):
                calls = []; before = len(module._issued)
                def altered(*args, **kwargs):
                    result = original(*args, **kwargs)
                    result[field] = value
                    return result
                with self.subTest(field=field), self.fixture.trace(calls), \
                        mock.patch.object(self.J, 'verify_selected_route_for_write', side_effect=altered):
                    with self.assertRaises(ValueError):
                        with self.scope(module, owner): self.fail('unapproved proof issued writer')
                self.assertEqual(len(calls), 2, 'actual full proof must precede injected output corruption')
                self.assertEqual(len(module._issued), before)
            emit('normal-writer-proof-result-checks', actualProofBeforeFault=True, issued=0)

    def test_unissued_tokens_and_expired_deadline_fail_before_content_connect(self):
        with self.F.warm(), self.seed.authority(), self.sync.sync_owner(self.queue) as owner:
            module = self.api(); calls = []
            with self.fixture.trace(calls), self.assertRaises(ValueError):
                with self.scope(module, owner, deadline=time.monotonic()-1): pass
            self.assertEqual(calls, [])
            for bad in (True, {}, object.__new__(module.NormalWriter)):
                with self.assertRaises((ValueError, TypeError)): module.validate_normal_writer(bad)
            emit('normal-writer-invalid-admission', contentConnects=calls)

    def test_deadline_crossed_inside_final_metadata_check_cannot_return_admission(self):
        with self.F.warm(), self.seed.authority(), self.sync.sync_owner(self.queue) as owner:
            module = self.api(); calls = []; advanced = [False]
            context = self.scope(module, owner)
            original = self.P.validate_writer_plan
            clock = time.monotonic
            def now(): return clock() + (100 if advanced[0] else 0)
            def validate(plan):
                result = original(plan)
                if context.writer is not None and not self.P._proofs:
                    advanced[0] = True
                return result
            error = None; returned = False
            try:
                with self.fixture.trace(calls), mock.patch.object(time, 'monotonic', side_effect=now), \
                        mock.patch.object(self.P, 'validate_writer_plan', side_effect=validate):
                    try:
                        context.__enter__()
                        returned = True
                    except (ValueError, RuntimeError) as exc: error = str(exc)
                emit('normal-writer-final-deadline', actualContentConnects=len(calls),
                     advancedAfterNativeClose=advanced[0], returned=returned, error=error)
                self.assertTrue(advanced[0])
                self.assertEqual(len(calls), 2)
                self.assertFalse(returned, 'deadline crossed during final check must revoke admission')
                self.assertIn('normal_writer_deadline', error or '')
                self.assertNotIn(context.writer, module._issued)
                self.assertTrue(self.F.fixture.probe('reader-lifetime.lock', 'EX'))
            finally: context.__exit__(None, None, None)

if __name__ == '__main__': unittest.main()
