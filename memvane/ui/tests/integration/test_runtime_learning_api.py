"""Runtime and learning monitor DTOs over a real selected install and the real wake queue writer.

The install is built and registered the way setup does it; wake rows are
written by ``runtime.api.wake_queue`` (the hook's writer). Every read is
checked against a tree digest: the monitor writes nothing.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

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
