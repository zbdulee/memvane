"""Runtime and learning monitor DTOs over a real selected install and the real wake queue writer.

The install is built and registered the way setup does it; wake rows are
written by ``runtime.api.wake_queue`` (the hook's writer). Every read is
checked against a tree digest: the monitor writes nothing.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading
from unittest import mock

from memvane.kernel import bundle_paths
from memvane.provider import api as provider
from memvane.registry import api as registry
from memvane.resident import api as resident
from memvane.runtime import api as runtime
from memvane.ui import read_adapters, runtime_adapters
from tests.support.case import FixtureCase

RUNTIME_KEYS = {'sharedWorkers', 'proofs', 'wakes', 'disk'}
LEARNING_KEYS = {'installId', 'checkpoint', 'gates', 'caseRows', 'caseRowsCap', 'metrics', 'teacher'}
WAKE_KEYS = {'kind', 'label', 'installId', 'projectName', 'state', 'attempt', 'dueAt', 'deferrals'}


def tree_digest(*roots):
    digest = hashlib.sha256()
    for root in roots:
        for path in sorted(Path(root).rglob('*')):
            if path.is_file() and not path.is_symlink():
                digest.update(str(path).encode() + path.read_bytes())
            else:
                digest.update(str(path).encode())
    return digest.hexdigest()


# Hang guard for event waits and joins; every assertion is on counts and thread state.
WAIT_SECONDS = 30


class LockArrivals:
    """Counts threads that reached a home's disk lock (after taking their entry time)."""

    def __init__(self):
        self.count = 0
        self.changed = threading.Condition()

    def wrap(self, lock):
        arrivals = self

        class Arriving:
            def __enter__(self):
                with arrivals.changed:
                    arrivals.count += 1
                    arrivals.changed.notify_all()
                lock.acquire()
                return self

            def __exit__(self, *exc):
                lock.release()
                return False

        return Arriving()

    def wait_for(self, count):
        with self.changed:
            return self.changed.wait_for(lambda: self.count >= count, timeout=WAIT_SECONDS)


class DiskCacheConcurrencyTests(FixtureCase):
    def run_in_thread(self, results, name, **kwargs):
        def call():
            results[name] = runtime_adapters.disk_overview(**kwargs)
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        return thread

    def test_a_refresh_that_waited_for_an_in_flight_scan_reuses_it(self):
        home = self.home / 'disk-single-flight'
        started, release = threading.Event(), threading.Event()

        def scan(_home):
            started.set()
            release.wait(WAIT_SECONDS)
            return [{'label': 'x', 'bytes': 7}]

        arrivals = LockArrivals()
        real_lock = runtime_adapters._disk_lock
        results = {}
        with mock.patch.object(runtime_adapters, 'disk_usage', side_effect=scan) as walk, \
                mock.patch.object(runtime_adapters, '_disk_lock', side_effect=lambda key: arrivals.wrap(real_lock(key))):
            try:
                first = self.run_in_thread(results, 'first', home=home, refresh=True)
                self.assertTrue(started.wait(WAIT_SECONDS))
                second = self.run_in_thread(results, 'second', home=home, refresh=True)
                self.assertTrue(arrivals.wait_for(2))
                self.assertEqual(walk.call_count, 1)
            finally:
                release.set()
            first.join(WAIT_SECONDS)
            second.join(WAIT_SECONDS)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(walk.call_count, 1)
            self.assertEqual(results['first'], {'disk': [{'label': 'x', 'bytes': 7}]})
            self.assertEqual(results['second'], results['first'])
            # A refresh that arrives after the stored scan walks again.
            runtime_adapters.disk_overview(home=home, refresh=True)
            self.assertEqual(walk.call_count, 2)

    def test_scans_of_different_homes_do_not_block_each_other(self):
        slow_home, fast_home = self.home / 'disk-slow', self.home / 'disk-fast'
        started, release = threading.Event(), threading.Event()

        def scan(home):
            if home == slow_home:
                started.set()
                release.wait(WAIT_SECONDS)
                return [{'label': 'slow', 'bytes': 1}]
            return [{'label': 'fast', 'bytes': 2}]

        results = {}
        with mock.patch.object(runtime_adapters, 'disk_usage', side_effect=scan) as walk:
            try:
                slow = self.run_in_thread(results, 'slow', home=slow_home, refresh=True)
                self.assertTrue(started.wait(WAIT_SECONDS))
                fast = self.run_in_thread(results, 'fast', home=fast_home, refresh=True)
                fast.join(WAIT_SECONDS)
                self.assertFalse(fast.is_alive())
                self.assertTrue(slow.is_alive())
                self.assertNotIn('slow', results)
                self.assertEqual(results['fast'], {'disk': [{'label': 'fast', 'bytes': 2}]})
            finally:
                release.set()
            slow.join(WAIT_SECONDS)
            self.assertFalse(slow.is_alive())
        self.assertEqual(results['slow'], {'disk': [{'label': 'slow', 'bytes': 1}]})
        self.assertEqual(walk.call_count, 2)
        slow_key, fast_key = os.fspath(slow_home), os.fspath(fast_home)
        self.assertIs(runtime_adapters._disk_lock(slow_key), runtime_adapters._disk_lock(slow_key))
        self.assertIsNot(runtime_adapters._disk_lock(slow_key), runtime_adapters._disk_lock(fast_key))


class RuntimeMonitorTests(FixtureCase):
    def setUp(self):
        super().setUp()
        from tests.support.install_builder import build_install
        from tests.support.project import make_project
        self.project = make_project(self.home, name='rtproj', dirname='rtproj')
        self.project.write_note('notes.md', 'Runtime notes.\n')
        self.project.commit()
        self.install = build_install(self, self.project, install_id='d' * 32)
        outcome = registry.after_setup('activate-index-origin', str(self.project.root), str(self.install.state),
                                       {'status': 'selected', 'phase': 'selected'})
        self.assertIn(outcome['status'], ('registered', 'unchanged'), outcome)
        self.id = self.install.install_id

    def test_runtime_overview_matches_the_contract_and_writes_nothing(self):
        before = tree_digest(self.home)
        view = runtime_adapters.runtime_overview(home=self.home)
        self.assertEqual(tree_digest(self.home), before)
        self.assertEqual(set(view), RUNTIME_KEYS)
        self.assertEqual(view['wakes'], [])
        self.assertEqual([proof['installId'] for proof in view['proofs']], [self.id])
        self.assertIn(view['proofs'][0]['state'], ('proved', 'attestation_invalid', 'unavailable'))
        installs = [item for item in view['disk'] if item['label'].startswith('설치별 상태')]
        self.assertEqual(len(installs), 1)
        self.assertGreater(installs[0]['bytes'], 0)
        for item in view['disk']:
            self.assertEqual(set(item), {'label', 'bytes'})

    def test_runtime_overview_without_disk_never_walks_the_disk(self):
        with mock.patch.object(runtime_adapters, 'disk_usage', side_effect=AssertionError('disk walked')) as walk:
            view = runtime_adapters.runtime_overview(home=self.home, include_disk=False)
        self.assertEqual(walk.call_count, 0)
        self.assertEqual(set(view), RUNTIME_KEYS)
        self.assertEqual(view['disk'], [])
        self.assertEqual([proof['installId'] for proof in view['proofs']], [self.id])

    def test_disk_overview_reuses_the_cache_until_refresh_or_expiry(self):
        before = tree_digest(self.home)
        with mock.patch.object(runtime_adapters, 'disk_usage', wraps=runtime_adapters.disk_usage) as walk:
            first = runtime_adapters.disk_overview(home=self.home, refresh=True)
            self.assertEqual(walk.call_count, 1)
            self.assertEqual(set(first), {'disk'})
            installs = [item for item in first['disk'] if item['label'].startswith('설치별 상태')]
            self.assertEqual(len(installs), 1)
            self.assertGreater(installs[0]['bytes'], 0)
            first['disk'].append({'label': 'caller mutation', 'bytes': 1})
            again = runtime_adapters.disk_overview(home=self.home)
            self.assertEqual(walk.call_count, 1)
            self.assertNotIn('caller mutation', [item['label'] for item in again['disk']])
            self.assertEqual(again['disk'], first['disk'][:-1])
            runtime_adapters.disk_overview(home=self.home, refresh=True)
            self.assertEqual(walk.call_count, 2)
            runtime_adapters.disk_overview(home=self.home, ttl_seconds=0)
            self.assertEqual(walk.call_count, 3)
            self.assertEqual(tree_digest(self.home), before)
            other = self.home / 'other-home'
            other.mkdir(mode=0o700)
            elsewhere = runtime_adapters.disk_overview(home=other)['disk']
            self.assertEqual([item['bytes'] for item in elsewhere if item['label'].startswith('설치별 상태')], [0])
            self.assertEqual(walk.call_count, 4)
            runtime_adapters.disk_overview(home=self.home)
            self.assertEqual(walk.call_count, 4)
        self.assertEqual(walk.call_args_list[-1], mock.call(other))

    def test_a_disk_scan_that_raises_is_not_cached(self):
        with mock.patch.object(runtime_adapters, 'disk_usage', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                runtime_adapters.disk_overview(home=self.home, refresh=True)
        with mock.patch.object(runtime_adapters, 'disk_usage', return_value=[{'label': 'x', 'bytes': 3}]) as walk:
            self.assertEqual(runtime_adapters.disk_overview(home=self.home), {'disk': [{'label': 'x', 'bytes': 3}]})
        self.assertEqual(walk.call_count, 1)

    def test_wakes_written_by_the_real_writer_read_in_user_language(self):
        queue = runtime.wake_queue()
        self.assertEqual(queue.offer('laya-sync', str(self.project.root), now=10), 'queued')
        self.assertEqual(queue.offer('learning', '/synthetic/elsewhere', now=20, delay=30), 'queued')
        root = runtime.service_root()
        before = tree_digest(root)
        wakes = runtime_adapters.runtime_overview(home=self.home)['wakes']
        self.assertEqual(tree_digest(root), before)
        self.assertEqual(len(wakes), 2)
        for item in wakes:
            self.assertEqual(set(item), WAKE_KEYS)
            self.assertEqual(item['state'], 'waiting')
            self.assertEqual((item['attempt'], item['deferrals']), (1, 0))
        by_kind = {item['kind']: item for item in wakes}
        self.assertEqual(by_kind['laya-sync']['label'], '색인 동기화 (laya-sync)')
        self.assertEqual(by_kind['laya-sync']['installId'], self.id)
        self.assertEqual(by_kind['laya-sync']['projectName'], 'rtproj')
        self.assertEqual(by_kind['learning']['label'], '학습 사이클 (learning)')
        self.assertIsNone(by_kind['learning']['installId'])
        self.assertTrue(by_kind['learning']['dueAt'].startswith('1970-01-01T00:00:50'))

    def test_wake_states_name_running_and_resource_deferrals(self):
        snapshot = {'status': 'read', 'wakes': [
            {'kind': 'laya-compile', 'target': '/x', 'status': 'running', 'due': 0, 'attempts': 0, 'reason': None},
            {'kind': 'auto', 'target': '/y', 'status': 'pending', 'due': 100, 'attempts': 3,
             'reason': 'ResourceDeferred'}], 'overflowRows': [{'kind': 'laya-sync', 'target': '/z', 'at': 5}]}
        items = runtime_adapters.wake_items(snapshot)
        self.assertEqual([(item['kind'], item['state'], item['attempt'], item['deferrals']) for item in items],
                         [('laya-compile', 'running', 1, 0), ('auto', 'deferred_for_resources', 4, 3),
                          ('laya-sync', 'waiting', 1, 0)])
        self.assertIsNone(items[0]['dueAt'])
        self.assertEqual(runtime_adapters.wake_items({'status': 'fixture_blocked'}), [])

    def test_owner_records_absent_read_as_unavailable(self):
        project = self.home / 'p'
        project.mkdir(mode=0o700)
        worker = bundle_paths.bundle_file(bundle_paths.WORKER_RELATIVE_PATH)
        artifact = bundle_paths.bundle_file(bundle_paths.PROVIDER_RELATIVE_PATH)
        manifest = {'approvedProviderId': 'synthetic', 'embeddingFingerprint': 'a' * 64,
                    'workerPath': str(worker), 'workerSha256': provider.hash_file(str(worker)),
                    'providerPath': str(artifact), 'providerArtifactSha256': provider.hash_file(str(artifact)),
                    'dimension': 4, 'modelId': 'fixture', 'modelSha256': 'c' * 64,
                    'serviceSocketPath': str(project / 'e.sock')}
        proof, worker_row = runtime_adapters.owner_views(self.id, resident.inspect_owner_state(manifest))
        self.assertEqual(proof, {'installId': self.id, 'state': 'unavailable', 'reproofInSeconds': None})
        if worker_row is not None:
            self.assertFalse(worker_row['running'])

    def test_owner_views_map_attestation_retry_and_ready(self):
        invalid = {'socket': {'status': 'absent'}, 'sharedWorker': {'status': 'absent'},
                   'installation': {'status': 'present', 'reason': 'attestation_invalid', 'terminal': True,
                                    'retryInSeconds': 1799.5}}
        self.assertEqual(runtime_adapters.owner_views(self.id, invalid)[0],
                         {'installId': self.id, 'state': 'attestation_invalid', 'reproofInSeconds': 1799})
        ready = {'socket': {'status': 'ready'}, 'installation': {'status': 'absent'},
                 'sharedWorker': {'status': 'present', 'socket': 'w-' + 'a' * 24 + '.sock', 'reason': 'ready'}}
        proof, worker = runtime_adapters.owner_views(self.id, ready)
        self.assertEqual(proof['state'], 'proved')
        self.assertEqual(worker, {'socket': 'w-' + 'a' * 24 + '.sock', 'running': True, 'pid': None,
                                  'bundleVersion': None, 'accelerator': None})

    def test_tree_size_never_follows_a_link(self):
        base = self.tmp / 'sized'
        (base / 'a').mkdir(parents=True)
        (base / 'a' / 'f').write_bytes(b'x' * 10)
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / 'big').write_bytes(b'y' * 1000)
        os.symlink(outside, base / 'link')
        self.assertEqual(runtime_adapters.tree_size(base), (10, True))
        self.assertEqual(runtime_adapters.tree_size(base, limit=1), (0, False))
        self.assertEqual(runtime_adapters.tree_size(base / 'link'), (0, True))


class LearningMonitorTests(FixtureCase):
    def setUp(self):
        super().setUp()
        from tests.support.install_builder import build_install
        from tests.support.project import make_project
        self.project = make_project(self.home, name='lnproj', dirname='lnproj')
        self.project.write_note('notes.md', 'Learning notes.\n')
        self.project.commit()
        self.install = build_install(self, self.project, install_id='e' * 32)
        registry.after_setup('activate-index-origin', str(self.project.root), str(self.install.state),
                             {'status': 'selected', 'phase': 'selected'})
        self.id = self.install.install_id

    def test_a_project_without_learning_reads_as_empty_gates(self):
        before = tree_digest(self.home)
        view = runtime_adapters.learning_overview(self.id, home=self.home)
        self.assertEqual(tree_digest(self.home), before)
        self.assertEqual(set(view), LEARNING_KEYS)
        self.assertEqual([(gate['code'], gate['current'], gate['required']) for gate in view['gates']],
                         [('candidateTrain', 0, 100), ('candidateEval', 0, 50),
                          ('candidateEvalNonEmpty', 0, 10), ('candidateEvalEmpty', 0, 10)])
        self.assertEqual((view['caseRows'], view['caseRowsCap']), (0, 1800))
        self.assertEqual(view['metrics'], {'exactRate': None, 'microF1': None, 'rankExactRate': None})
        self.assertNotIn('invariants', view)

    def test_invariants_appear_only_on_request(self):
        view = runtime_adapters.learning_overview(self.id, check_invariants=True, home=self.home)
        self.assertEqual(set(view), LEARNING_KEYS | {'invariants'})

    def test_an_unknown_or_invalid_install_is_refused(self):
        with self.assertRaises(read_adapters.TargetError) as invalid:
            runtime_adapters.learning_overview('nothex', home=self.home)
        self.assertEqual((str(invalid.exception), invalid.exception.status), ('install_id_invalid', 400))
        with self.assertRaises(read_adapters.TargetError) as unknown:
            runtime_adapters.learning_overview('f' * 32, home=self.home)
        self.assertEqual(unknown.exception.status, 404)

    def test_inspection_maps_to_gates_metrics_and_teacher(self):
        inspection = {
            'status': 'read', 'caseRows': 420, 'caseLimit': 1800,
            'promotion': {'gold': 90, 'candidateTrain': 64, 'candidateEval': 26, 'candidateEvalNonEmpty': 20,
                          'candidateEvalEmpty': 6, 'needs': dict(runtime_adapters.GATE_NEEDS)},
            'pointers': {'active': {'cycle_id': 'c-7'}, 'pending': None, 'journalPhase': None},
            'scores': {'c-7': {'cases': 30, 'exactRate': 0.5, 'microF1': 0.7, 'rankExactRate': 0.4},
                       None: {'cases': 90, 'exactRate': 0.1}},
            'teacher': {'off': False, 'hosts': {
                'claude': {'queued': 3, 'reason': None}, 'codex': {'queued': 0, 'reason': 'cli_unavailable'}}},
        }
        view = runtime_adapters.learning_view(self.id, inspection)
        self.assertEqual(view['checkpoint'], 'c-7')
        self.assertEqual([gate['current'] for gate in view['gates']], [64, 26, 20, 6])
        self.assertEqual(view['metrics'], {'exactRate': 0.5, 'microF1': 0.7, 'rankExactRate': 0.4})
        self.assertEqual(view['teacher'], [{'host': 'claude', 'callsToday': 3, 'waitingReason': None},
                                           {'host': 'codex', 'callsToday': 0, 'waitingReason': 'cli_unavailable'}])
