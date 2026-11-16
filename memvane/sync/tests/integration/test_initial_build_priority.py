"""A new project's initial build gets the per-user sync owner promptly.

Every selected sync and every initial build share one per-user owner. An
initial build waits briefly for it (``sync_owner(wait=True)``) instead of a
single try; a selected sync yields at its provider-call boundaries to another
project's initial build whose recent attempt found the owner busy, pulling
that build's wake to now (the yield itself: ``test_schema3_vectors``). The
real wake queue runs in the case's temporary directory; the build record
lookup is the install api's.
"""
from contextlib import contextmanager
import os
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from memvane.runtime import api as runtime
from memvane.sync import product_sync_worker as worker
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project

BIG = '/projects/big'
NEW = '/projects/new'


def record(phase='building', reason='owner_busy', age=0.0, **progress):
    return {'phase': phase,
            'progress': {'state': 'deferred', 'reason': reason, 'lastAttemptClass': 'other',
                         'lastAttemptNs': time.time_ns() - int(age * 1_000_000_000), **progress}}


class _Ghost:
    """A queue whose listing names a row that is gone when it is pulled."""

    def __init__(self, queue, rows):
        self._queue, self._rows = queue, rows

    def pending(self):
        return self._rows

    def __getattr__(self, name):
        return getattr(self._queue, name)


class Waiting(FixtureCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / 'wakes'
        self.root.mkdir(mode=0o700)
        self.queue = runtime.wake_queue(self.root)
        self.controller = SimpleNamespace(root=self.root)

    def row(self, target):
        rows = [row for row in self.queue.pending() if row['target'] == target]
        return rows[0] if rows else None

    def waiting(self, records, project=BIG):
        def lookup(target):
            value = records.get(target)
            if isinstance(value, Exception):
                raise value
            return value
        with mock.patch('memvane.install.api.build_lookup', side_effect=lookup):
            return worker._initial_build_waiting(self.controller, project)

    def test_a_blocked_build_of_another_project_is_waited_for_and_pulled_first(self):
        self.queue.put('laya-sync', NEW, now=time.time() + 3600)
        self.queue.put('laya-sync', BIG, now=5)
        self.queue.put('laya-compile', '/projects/other', now=1)
        self.assertTrue(self.waiting({NEW: record()}))
        self.assertEqual(self.row(NEW)['due'], 0)
        self.assertEqual(self.queue.pending()[0]['target'], NEW)

    def test_a_vanished_row_is_never_recreated(self):
        ghost = {'id': 'x', 'kind': 'laya-sync', 'target': NEW, 'revision': 1,
                 'status': 'pending', 'due': 0, 'attempts': 0, 'reason': None}
        with mock.patch('memvane.runtime.api.wake_queue', lambda root=None: _Ghost(self.queue, [ghost])):
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
            'raised after owner_busy': ({NEW: record(lastAttemptClass='provider_error',
                                                     lastAttemptReason='provider_start_failed')}, BIG),
            'raised, same class': ({NEW: record(lastAttemptReason='other_error')}, BIG),
            'stale attempt': ({NEW: record(age=worker.INITIAL_BUILD_YIELD_RECENT_SECONDS + 60)}, BIG),
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

    def test_an_unavailable_queue_never_defers_the_sync(self):
        class Unavailable:
            def pending(self):
                raise OSError('unavailable')
        with mock.patch('memvane.runtime.api.wake_queue', lambda root=None: Unavailable()):
            self.assertFalse(worker._initial_build_waiting(self.controller, BIG))


class BuildOwner(FixtureCase):
    def setUp(self):
        super().setUp()
        self.fixture = make_project(self.home)
        note = self.fixture.write_note('notes/heron.md', 'The emerald heron nests by the pond.\n')
        self.fixture.write_card('heron.md', title='Emerald heron', body='The emerald heron.\n', sources=[note])
        self.fixture.commit('fixture')
        self.target = build_install(self, self.fixture, upto='prepared')

    def owner(self, claim=None):
        calls = []
        real = worker.sync_owner

        @contextmanager
        def recorded(path, **kwargs):
            calls.append((os.path.basename(str(path)), kwargs))
            if claim is None:
                with real(path, **kwargs) as held:
                    yield held
            else:
                yield claim()
        return calls, mock.patch.object(worker, 'sync_owner', recorded)

    def test_the_build_defers_when_the_owner_stays_busy(self):
        calls, busy = self.owner(claim=lambda: None)
        with busy:
            result = self.target.run_sync()
        self.assertEqual(result, {'state': 'deferred', 'reason': 'owner_busy'})
        self.assertEqual(calls, [('store.sqlite', {'wait': True})])
        from memvane.install.api import build_lookup
        self.assertEqual(build_lookup(str(self.project_root()))['phase'], 'registered')

    def test_the_build_waits_for_the_owner_and_reaches_ready(self):
        calls, owned = self.owner()
        with owned:
            results = self.target.build()
        self.assertEqual(results[-1].get('buildPhase'), 'ready', results)
        self.assertTrue(calls)
        self.assertTrue(all(kwargs == {'wait': True} for _name, kwargs in calls), calls)

    def test_lexical_build_activates_before_vectors_and_then_seals_them(self):
        from memvane.journal.api import inspect_journal
        from memvane.sync import api as sync
        from memvane.sync.tests.support import Coordinator

        for name in ('egret', 'tern'):
            note = self.fixture.write_note('notes/' + name + '.md', name + ' nests by the pond.\n')
            self.fixture.write_card(name + '.md', title=name, body=name + ' nests by the pond.\n', sources=[note])
        self.fixture.commit('three cards')
        # The provider lane cannot start; the first complete lexical head
        # must still make this install ready to finalize and activate.
        result = sync.run(self.fixture.root, coordinator=Coordinator(refuse='cpu_pressure'),
                          report={}, start_service=self.target.provider.start_service)
        self.assertEqual((result.get('buildPhase'), result.get('vector', {}).get('state')),
                         ('ready', 'off'), result)
        self.assertEqual(result['ingested'], 3)
        self.assertEqual(self.target.finalize()['status'], 'initial_build_finalized')
        self.assertEqual(self.target.activate()['status'], 'selected')
        reader = inspect_journal(self.target.journal)['reader']
        self.assertEqual((reader['vector_profile'], reader['vector_recipe']), (None, None))
        self.target.provider.admit_queries()
        self.assertIn('heron.md', self.target.recall('emerald heron'))
        event = [row for row in self.target.events() if row.get('event') == 'recall'][-1]
        self.assertEqual(event.get('vector_fallback'), 'selected_view_vectors_pending')

        result = self.target.run_sync()
        self.assertEqual(result['vector']['state'], 'published', result)
        reader = inspect_journal(self.target.journal)['reader']
        self.assertTrue(reader['vector_profile'])
        self.assertTrue(reader['vector_recipe'])
        self.assertIn('heron.md', self.target.recall('emerald heron'))
        event = [row for row in self.target.events() if row.get('event') == 'recall'][-1]
        self.assertEqual(event.get('semantic'), 'complete')

    def test_a_build_with_an_existing_lexical_head_resumes_to_ready(self):
        from memvane.install.api import build_lookup
        real_publish = worker._publish_schema3_view

        def old_build_result(*args, **kwargs):
            result = real_publish(*args, **kwargs)
            return {**result, 'state': 'pending', 'reason': 'schema3_vector_deadline'}

        # Model a 1.0.0 pass which published a lexical head but kept the
        # build in progress while its vector fill was incomplete.
        with mock.patch.object(worker, '_publish_schema3_view', side_effect=old_build_result):
            result = self.target.run_sync()
        self.assertIsNotNone(result['generation'])
        self.assertEqual(build_lookup(str(self.fixture.root))['phase'], 'building')
        resumed = self.target.run_sync()
        self.assertEqual((resumed['generation'], resumed['buildPhase']), (result['generation'], 'ready'))
        from memvane.install.api import initial_build_status
        self.assertEqual(initial_build_status(str(self.target.state))['progress']['ingest'],
                         {'done': 1, 'total': 1})
        self.assertEqual(self.target.finalize()['status'], 'initial_build_finalized')
        self.assertEqual(self.target.activate()['status'], 'selected')
        self.assertIn('heron.md', self.target.recall('emerald heron'))

    def test_build_status_reports_bounded_ingest_and_vector_progress(self):
        from memvane.install.api import initial_build_status
        for name in ('egret', 'tern'):
            note = self.fixture.write_note('notes/' + name + '.md', name + ' nests by the pond.\n')
            self.fixture.write_card(name + '.md', title=name, body=name + ' nests by the pond.\n', sources=[note])
        self.fixture.commit('three cards')
        with mock.patch.object(worker, 'SCHEMA3_MAX_INGEST', 2):
            pending = self.target.run_sync()
        self.assertEqual(pending['state'], 'pending')
        status = initial_build_status(str(self.target.state))
        self.assertIn('progress', status)
        self.assertEqual(status['progress']['ingest'], {'done': 2, 'total': 3})
        self.assertEqual(status['progress']['stage'], 'ingest')
        self.target.run_sync()
        status = initial_build_status(str(self.target.state))
        self.assertEqual(status['progress']['ingest'], {'done': 3, 'total': 3})
        self.assertEqual(self.target.finalize()['status'], 'initial_build_finalized')
        self.assertEqual(self.target.activate()['status'], 'selected')
        self.target.run_sync()
        before = (self.target.state / 'events.jsonl').read_bytes()
        status = initial_build_status(str(self.target.state))
        self.assertEqual(status['progress']['vector'], {'done': 3, 'total': 3})
        self.assertEqual(status['progress']['stage'], 'complete')
        self.assertEqual((self.target.state / 'events.jsonl').read_bytes(), before)

    # Product regression candidate (reported to the lead): the cold build shares
    # the selected sync's vector context, whose provider calls probe
    # ``_initial_build_waiting``; its docstring says initial builds never yield.
    def test_initial_builds_never_probe_for_other_builds(self):
        with mock.patch.object(worker, '_initial_build_waiting',
                               side_effect=AssertionError('an initial build probed')):
            results = self.target.build()
        self.assertEqual(results[-1].get('buildPhase'), 'ready', results)

    def project_root(self):
        return self.fixture.root


if __name__ == '__main__':
    unittest.main()
