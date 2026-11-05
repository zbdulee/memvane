#!/usr/bin/env python3
"""Metadata-only writer bootstrap; real v5, synthetic bundle/provider authority."""
from contextlib import closing, contextmanager
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import shutil
import sys
import threading
import unittest
from unittest import mock
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('writer_plan_seed', ROOT / 'test/fixtures/laya-reader-move-seal-checks.py')
existing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(existing)


def emit(label, **values):
    print(json.dumps(dict(case=label, **values), sort_keys=True), flush=True)


class WriterOpenPlanChecks(unittest.TestCase):
    def setUp(self):
        self.seed = existing.MoveSealChecks('runTest')
        self.addCleanup(self.seed.doCleanups)
        if self._testMethodName == 'test_unprepared_manifest_refuses_without_content_open_or_registry_changes':
            windows = importlib.import_module('laya_reader_windows')
            with mock.patch.object(windows, '_prepare_conversion_windows'):
                self.seed.setUp()
        else:
            self.seed.setUp()
        self.case = self.seed.case
        self.W = importlib.import_module('laya_reader_windows')
        self.J = importlib.import_module('laya_install_journal')
        self.route = importlib.import_module('laya_index_host_route')
        self.content = {
            Path(p) for p in (self.case.db, self.case.catalog, self.case.store,
                self.seed.sidecar, self.seed.mover._queue_path(self.case.project, None))}

    def api(self):
        if importlib.util.find_spec('laya_writer_open_plan') is None:
            emit('writer-plan-feature-absence', actualJournalVersion=5,
                 publishedCards=3, featureAbsent=True)
            self.fail('metadata-only writer plan feature absent after actual v5 seed')
        module = importlib.import_module('laya_writer_open_plan')
        self.assertTrue(callable(getattr(module, 'capture_writer_plan', None)))
        self.assertTrue(callable(getattr(module, 'validate_writer_plan', None)))
        return module

    @contextmanager
    def traced(self):
        calls = []
        actual = sqlite3.connect
        def connect(database, *args, **kwargs):
            raw = str(database)
            path = Path(unquote(urlparse(raw).path)) if raw.startswith('file:') else Path(raw)
            if path in self.content:
                calls.append(dict(path=str(path), uri=raw))
            return actual(database, *args, **kwargs)
        with mock.patch.object(sqlite3, 'connect', side_effect=connect):
            yield calls

    @staticmethod
    def snapshot(parent):
        return {str(p.relative_to(parent)): (p.read_bytes(), p.stat().st_dev,
            p.stat().st_ino, p.stat().st_mode, p.stat().st_uid, p.stat().st_nlink)
            for p in parent.rglob('*') if p.is_file()}

    def test_prepared_metadata_capture_validation_and_capability_rejection(self):
        with self.seed.prepared():
            self.W.prepare_windows(self.seed.installer)
            module = self.api()
            with self.traced() as calls:
                plan = module.capture_writer_plan(self.case.project, executing_root=self.case.plugin)
                module.validate_writer_plan(plan)
                self.assertEqual(Path(plan.queue_path), Path(self.seed.mover._queue_path(self.case.project, None)))
                self.assertEqual(plan.install_id, self.seed.install)
                for bad in (True, {}, object.__new__(type(plan))):
                    with self.subTest(claim=type(bad).__name__):
                        with self.assertRaises((ValueError, TypeError, RuntimeError)):
                            module.validate_writer_plan(bad)
                with self.assertRaises((ValueError, TypeError)):
                    type(plan)()
                with self.assertRaises((AttributeError, TypeError)):
                    plan.install_id = 'caller-value'
                with self.assertRaises((ValueError, TypeError, RuntimeError)):
                    self.W.component_for(plan, 'queue')
                with self.assertRaises((ValueError, TypeError, RuntimeError)):
                    self.seed.reader._recheck(plan)
                result = []
                def other_thread():
                    try: module.validate_writer_plan(plan)
                    except (ValueError, TypeError, RuntimeError) as exc: result.append(str(exc))
                worker = threading.Thread(target=other_thread)
                worker.start(); worker.join(3)
                if worker.is_alive(): raise RuntimeError('plan validation thread did not terminate')
                self.assertEqual(len(result), 1)
                queue = Path(plan.queue_path)
                alias = queue.with_name('writer-plan-private-alias')
                os.link(queue, alias)
                try:
                    with self.assertRaises((ValueError, OSError, RuntimeError)):
                        module.validate_writer_plan(plan)
                finally: alias.unlink()
                module.validate_writer_plan(plan)
            emit('prepared-metadata-plan', contentConnects=calls,
                 installId=plan.install_id, wrongThreadRejected=bool(result))
            self.assertEqual(calls, [])

    def test_unprepared_manifest_refuses_without_content_open_or_registry_changes(self):
        with self.seed.prepared():
            parent = self.seed.reader._lock_dir(self.seed.install, create=False)
            self.assertFalse((parent / 'component-windows-v1.json').exists())
            before = self.snapshot(parent)
            module = self.api()
            error = None
            with self.traced() as calls:
                try: module.capture_writer_plan(self.case.project, executing_root=self.case.plugin)
                except (ValueError, OSError, RuntimeError) as exc: error = str(exc)
            unchanged = self.snapshot(parent) == before
            emit('unprepared-plan', reason=error, contentConnects=calls, registryUnchanged=unchanged)
            self.assertIsNotNone(error)
            self.assertIn('writer_windows_not_prepared', error)
            self.assertEqual(calls, [])
            self.assertTrue(unchanged, 'unprepared capture must not prepare/create/repair UID files')

    def test_writer_plan_does_not_require_reader_published_exclusion_freshness(self):
        with self.seed.prepared():
            self.W.prepare_windows(self.seed.installer)
            decision = self.route.select_route(str(self.case.project), action='update')
            self.assertEqual(decision['state'], 'candidate')
            from laya_index_a import LayaIndexA
            with closing(LayaIndexA(self.case.db)) as index:
                self.assertEqual(index.exclude(decision['scopeId'], path='writer-plan-excluded.md'), 'excluded')
            proof = self.J.verify_selected_route_for_write(decision, action='update')
            self.assertIsInstance(proof, dict)
            reader_error = None
            try:
                self.seed.reader.seal_current_reader(self.case.state, self.case.project,
                    executing_root=self.case.plugin)
            except (ValueError, RuntimeError) as exc: reader_error = str(exc)
            emit('writer-vs-reader-proof-baseline', writerProofAccepted=True, readerReason=reader_error)
            self.assertIsNotNone(reader_error)
            self.assertIn('reader_exclusions_changed', reader_error)
            module = self.api()
            with self.traced() as calls:
                plan = module.capture_writer_plan(self.case.project, executing_root=self.case.plugin)
                module.validate_writer_plan(plan)
            emit('writer-plan-after-exclusion', installId=plan.install_id, contentConnects=calls)
            self.assertEqual(calls, [])


    def test_independent_reader_base_selection_does_not_rebind_the_canonical_writer(self):
        with self.seed.prepared():
            self.W.prepare_windows(self.seed.installer)
            reader=self.seed.reader
            alternate=self.seed.helper.alternate(self.case,reader,self.seed.installer,'writer-plan-reader-copy.sqlite')
            self.content.add(Path(alternate.record['db_path']))
            current=reader.read_selection(self.case.journal)
            selected=reader.cas_reader(self.case.journal,expected_epoch=current.epoch,
                expected_endpoint=current.preferred_endpoint,target=alternate,transition_id='d'*32)
            decision=self.route.select_route(str(self.case.project),action='update')
            proof=self.J.verify_selected_route_for_write(decision)
            self.assertTrue(proof['productRouteAllowed'])
            self.assertEqual(decision['dbPath'],str(self.case.db))
            module=self.api();error=None
            with self.traced() as calls:
                try:
                    plan=module.capture_writer_plan(self.case.project,executing_root=self.case.plugin)
                    module.validate_writer_plan(plan)
                except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
            emit('separate-reader-and-writer-base',readerEpoch=selected.epoch,
                readerBase=alternate.record['db_path'],writerBase=decision['dbPath'],
                writerProofAccepted=True,error=error,contentConnects=calls)
            self.assertIsNone(error,'legal reader base CAS must not block canonical writer planning')
            self.assertEqual(calls,[])

    def test_same_path_execution_root_replacement_invalidates_plan_and_restore_validates(self):
        with self.seed.prepared():
            self.W.prepare_windows(self.seed.installer);module=self.api()
            plan=module.capture_writer_plan(self.case.project,executing_root=self.case.plugin)
            saved=self.case.plugin.with_name(self.case.plugin.name+'-saved-writer-plan')
            original=self.case.plugin.stat();os.rename(self.case.plugin,saved)
            try:
                shutil.copytree(saved,self.case.plugin)
                self.assertFalse(os.path.samestat(original,self.case.plugin.stat()))
                error=None
                with self.traced() as calls:
                    try:module.validate_writer_plan(plan)
                    except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
                emit('execution-root-node-replacement',error=error,contentConnects=calls)
                self.assertIsNotNone(error)
                self.assertIn('writer_plan_changed',error)
                self.assertEqual(calls,[])
            finally:
                if self.case.plugin.exists():shutil.rmtree(self.case.plugin)
                os.rename(saved,self.case.plugin)
            module.validate_writer_plan(plan)


if __name__ == '__main__': unittest.main()
