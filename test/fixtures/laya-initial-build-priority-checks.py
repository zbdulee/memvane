#!/usr/bin/env python3
"""A new project's initial build gets the per-user sync owner promptly.

Every selected sync and every initial build share one per-user owner lock. An
initial build waits briefly for it, like a selected sync, instead of a
single non-blocking try; and a selected sync yields between rounds to another
project's initial build whose recent attempt found the owner busy, pulling
that build's wake to now. The real wake queue runs in a temporary directory;
the build record lookup, catalog steps and owner lock are replaced.
"""
from __future__ import annotations

from contextlib import contextmanager
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CORE))
from memvane.journal import initial_build as laya_initial_build  # noqa: E402
from memvane.sync import product_sync_worker as worker  # noqa: E402
from memvane.runtime import runtime_work  # noqa: E402
from memvane.kernel.resource_deferred import ResourceDeferred  # noqa: E402

BIG = '/projects/big'
NEW = '/projects/new'


def record(phase='building', reason='owner_busy', age=0.0):
    return {'phase': phase,
            'progress': {'state': 'deferred', 'reason': reason, 'lastAttemptClass': 'other',
                         'lastAttemptNs': time.time_ns() - int(age * 1_000_000_000)}}


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.root.chmod(0o700)
        self.queue = runtime_work.WakeQueue(self.root)
        self.controller = types.SimpleNamespace(root=self.root)

    def row(self, target):
        rows = [row for row in self.queue.pending() if row['target'] == target]
        return rows[0] if rows else None

    def waiting(self, records, project=BIG):
        def lookup(target):
            value = records.get(target)
            if isinstance(value, Exception):
                raise value
            return value
        with mock.patch.object(laya_initial_build, 'lookup', side_effect=lookup):
            return worker._initial_build_waiting(self.controller, project)


class Waiting(Base):
    def test_blocked_build_of_another_project_is_waited_for_and_pulled_first(self):
        self.queue.put('laya-sync', NEW, now=time.time() + 3600)
        self.queue.put('laya-sync', BIG, now=5)
        self.queue.put('laya-compile', '/projects/other', now=1)
        self.assertTrue(self.waiting({NEW: record()}))
        self.assertEqual(self.row(NEW)['due'], 0)
        self.assertEqual(self.queue.pending()[0]['target'], NEW)

    def test_a_vanished_row_is_never_recreated(self):
        ghost = {'id': 'x', 'kind': 'laya-sync', 'target': NEW, 'revision': 1,
                 'status': 'pending', 'due': 0, 'attempts': 0, 'reason': None}
        with mock.patch.object(runtime_work.WakeQueue, 'pending', return_value=[ghost]):
            self.assertTrue(self.waiting({NEW: record()}))
        self.assertIsNone(self.row(NEW))

    def test_pull_first_moves_only_an_existing_pending_row(self):
        self.assertFalse(self.queue.pull_first('laya-sync', NEW))
        self.assertIsNone(self.row(NEW))
        self.queue.put('laya-sync', NEW, now=time.time() + 60)
        before = self.row(NEW)
        self.assertTrue(self.queue.pull_first('laya-sync', NEW))
        after = self.row(NEW)
        self.assertEqual(after['due'], 0)
        self.assertEqual((after['attempts'], after['reason'], after['revision']),
                         (before['attempts'], before['reason'], before['revision']))
        self.assertFalse(self.queue.pull_first('laya-sync', NEW))

    def test_no_yield_otherwise(self):
        later = time.time() + 3600
        self.queue.put('laya-sync', NEW, now=later)
        cases = {
            'running project': ({BIG: record()}, NEW),
            'sealed build': ({NEW: record(phase='sealed')}, BIG),
            'blocked build': ({NEW: record(phase='blocked')}, BIG),
            'other reason': ({NEW: record(reason='provider_error')}, BIG),
            # A raising attempt keeps the earlier reason and adds lastAttemptReason.
            'raised after owner_busy': ({NEW: {**record(), 'progress': {
                **record()['progress'], 'lastAttemptClass': 'provider_error',
                'lastAttemptReason': 'provider_start_failed'}}}, BIG),
            'raised, same class': ({NEW: {**record(), 'progress': {
                **record()['progress'], 'lastAttemptReason': 'other_error'}}}, BIG),
            'stale attempt': ({NEW: record(age=worker.INITIAL_BUILD_YIELD_RECENT_SECONDS + 60)},
                              BIG),
            'no build': ({}, BIG),
            'unreadable pointer': ({NEW: ValueError('initial_build_pointer_invalid')}, BIG),
        }
        for name, (records, project) in cases.items():
            with self.subTest(name):
                self.assertFalse(self.waiting(records, project=project))
                self.assertEqual(self.row(NEW)['due'], later)
        # The project itself as the only build: a sync never yields to itself.
        self.assertFalse(self.waiting({NEW: record()}, project=NEW))

    def test_only_laya_sync_rows_count(self):
        self.queue.put('laya-compile', NEW, now=0)
        self.assertFalse(self.waiting({NEW: record()}))
        self.queue.put('laya-sync', NEW, now=0)
        self.assertTrue(self.waiting({NEW: record()}))

    def test_unavailable_queue_never_defers_the_sync(self):
        with mock.patch.object(runtime_work.WakeQueue, 'pending',
                               side_effect=OSError('unavailable')):
            self.assertFalse(worker._initial_build_waiting(self.controller, BIG))


class Runtime:
    def __init__(self, root):
        self.queue = types.SimpleNamespace(path=str(root / 'queue.sqlite'))
        self.scans = 0

    def request_scan(self):
        self.scans += 1
        return {'status': 'requested'}


class SelectedSync(Base):
    def run_sync(self, project, waiting):
        runtime = Runtime(self.root)
        steps = mock.Mock(return_value={'state': 'unchanged', 'generation': None,
                                        'claim': None})
        probe = mock.Mock(return_value=waiting)
        with mock.patch.object(worker, '_higher_priority_wake', return_value=False), \
                mock.patch.object(worker, '_initial_build_waiting', probe), \
                mock.patch.object(worker, 'sync_step', steps), \
                mock.patch('laya_vector_capacity.provider_profile_id', return_value='p'):
            try:
                result = worker._sync_capacity(runtime, object(), lambda *_a: None,
                                               self.controller, lambda: False,
                                               owner=object(), project=project)
            except ResourceDeferred as exc:
                result = exc
        return result, steps, probe

    def test_selected_sync_yields_to_a_waiting_initial_build(self):
        result, steps, probe = self.run_sync(BIG, True)
        self.assertIsInstance(result, ResourceDeferred)
        self.assertEqual(str(result), 'laya_sync_yield_priority')
        steps.assert_not_called()
        probe.assert_called_with(self.controller, BIG)

    def test_selected_sync_without_project_or_waiting_build_runs(self):
        for project, waiting in ((None, True), (BIG, False)):
            with self.subTest(project=project, waiting=waiting):
                result, steps, probe = self.run_sync(project, waiting)
                self.assertNotIsInstance(result, ResourceDeferred)
                steps.assert_called()
                if project is None:
                    probe.assert_not_called()


class BuildRounds(Base):
    def run_build(self, claim):
        runtime = Runtime(self.root)
        calls = []

        @contextmanager
        def owner(path, **kwargs):
            calls.append((path, kwargs))
            yield claim
        steps = mock.Mock(return_value={'state': 'unchanged', 'generation': None,
                                        'claim': None})
        with mock.patch.object(worker, 'sync_owner', owner), \
                mock.patch.object(worker, 'sync_step', steps), \
                mock.patch.object(worker, '_higher_priority_wake', return_value=False), \
                mock.patch('laya_vector_capacity.provider_profile_id', return_value='p'):
            result = worker._build_rounds(runtime, object(), lambda *_a: None,
                                          self.controller, lambda: False, lambda: True,
                                          deadline=math.inf, clock=lambda: 0)
        return runtime, result, steps, calls

    def test_build_waits_for_the_owner_and_passes_it_to_the_step(self):
        claim = object()
        runtime, result, steps, calls = self.run_build(claim)
        self.assertEqual(result['state'], 'unchanged')
        self.assertEqual(calls, [(Path(runtime.queue.path), {'wait': True})])
        self.assertIs(steps.call_args.kwargs['owner'], claim)

    def test_build_defers_when_the_owner_stays_busy(self):
        _runtime, result, steps, calls = self.run_build(None)
        self.assertEqual(result, {'state': 'deferred', 'reason': 'owner_busy'})
        steps.assert_not_called()
        self.assertEqual(len(calls), 1)

    def test_initial_builds_never_probe_for_other_builds(self):
        with mock.patch.object(worker, '_initial_build_waiting',
                               side_effect=AssertionError('probed')):
            _runtime, result, _steps, _calls = self.run_build(object())
        self.assertEqual(result['state'], 'unchanged')


if __name__ == '__main__':
    os.environ.setdefault('QMD_SANDBOX', '1')
    unittest.main()
