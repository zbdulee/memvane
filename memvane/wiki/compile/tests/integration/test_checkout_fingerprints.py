"""Compile fingerprints describe the same source read and checkout observation."""
import hashlib
import io
import json
import sqlite3
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from memvane.store import api as store
from memvane.wiki.card import api as card
from memvane.wiki.compile import wiki_compile as compiler, wiki_compile_worker as worker
from memvane.wiki.engines import api as engines
from memvane.wiki.intake import api as intake
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class CheckoutFingerprints(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        self.source = self.project.write_note('docs/source.md', 'synthetic source\n')
        self.project.git('add', '--', '.memvane/settings.json')
        self.project.git('commit', '-qm', 'fixture')
        self.config = expanded(self.project, compile={'mode': 'auto-wiki',
                               'extractor': {'builtins': ['claude']}})
        use_config(self, self.project, self.config)
        decision = {'state': 'candidate', 'projectRoot': str(self.project.root),
                    'installId': 'a' * 32, 'scopeId': 'b' * 32, 'storeIncarnation': 'c' * 32,
                    'recallSettings': {'skipPaths': []}}
        self.bound = (decision, {'productRouteAllowed': True})
        binding = store.observe_binding(self.tmp / 'checkout.sqlite', scope_id='b' * 32,
                                        create_root=str(self.project.root))
        repo, worktree = binding['repository'], binding['worktree']
        self.checkout = SimpleNamespace(
            capture=lambda: store.capture_view(str(self.project.root), repo, worktree, fresh=True),
            matches=lambda capture: store.capture_matches(capture, repo, worktree, fresh=True))
        self.calls, self.candidates = [], []
        def extract(argv, payload, timeout, root):
            self.calls.append(payload)
            return {'candidates': [{'title': 'Source', 'summary': 'Synthetic fact',
                                    'sourceFingerprints': {'schema': 'model-decoy'}}]}, None, 0
        def compile_candidate(root, candidate, **kwargs):
            self.candidates.append(candidate)
            return {'action': 'created', 'sourceRevisions': candidate['sourceRevisions']}
        for item in (patch.object(workspace, 'selected', return_value=self.bound),
                     patch.object(workspace, 'recheck'), patch.object(engines, 'run_extractor', side_effect=extract),
                     patch.object(worker, 'compile_candidate', side_effect=compile_candidate),
                     patch.object(worker, 'gather_similar_pages', return_value=[])):
            item.start()
            self.addCleanup(item.stop)

    def job(self):
        return {'trigger': 'post_tool_source', 'engine': 'claude', 'cwd': str(self.project.root),
                'selectedRoute': workspace.route_identity(self.bound),
                'source': {'kind': 'file', 'path': 'docs/source.md', 'collection': self.project.raw_name}}

    def process(self, checkout=None):
        return worker.process_job(self.project.root, self.config, self.config['compile'], self.job(),
                                  checkout=self.checkout if checkout is None else checkout)

    def test_untracked_staged_committed_and_detached_sources_are_observed(self):
        for state in ('untracked', 'staged', 'committed', 'detached'):
            if state == 'staged':
                self.project.git('add', '--', 'docs/source.md')
            elif state == 'committed':
                self.project.git('commit', '-qm', 'source')
            elif state == 'detached':
                self.project.git('checkout', '-q', '--detach')
            with self.subTest(state=state):
                self.assertEqual(self.process(), (True, False, []))
                candidate = self.candidates[-1]
                spec = candidate['sourceFingerprints']
                row, revision = spec['fingerprints'][0], candidate['sourceRevisions'][0]
                self.assertEqual((row['path'], row['source_sha'], row['byte_length']),
                                 (revision['path'], revision['sha256'], revision['size']))
                self.assertEqual(row['source_sha'], hashlib.sha256(self.source.read_bytes()).hexdigest())
                self.assertEqual(row['tracked_state'], 'untracked' if state == 'untracked' else 'tracked')
                self.assertEqual(row['binding_state'], 'observed')
                self.assertEqual(row['branch_key'].startswith('detached:'), state == 'detached')
                self.assertNotEqual(spec['schema'], 'model-decoy')
        self.assertEqual(len(self.calls), 4)

    def test_changed_checkout_during_source_read_never_calls_extractor(self):
        original = card.snapshot_bytes
        def snapshot(path):
            result = original(path)
            self.project.git('add', '--', 'docs/source.md')
            return result
        with patch.object(card, 'snapshot_bytes', side_effect=snapshot):
            self.assertEqual(self.process(), (False, True, []))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.candidates, [])

    def test_unobservable_checkout_preserves_job(self):
        def unavailable():
            raise OSError('synthetic checkout unavailable')
        checkout = SimpleNamespace(capture=unavailable, matches=lambda value: False)
        self.assertEqual(self.process(checkout), (False, True, []))
        self.assertEqual(self.calls, [])

    def test_git_tracking_failure_stays_unknown_but_source_is_compiled(self):
        from memvane.wiki.lifecycle import api as lifecycle
        with patch.object(lifecycle, 'tracked_states', return_value={'docs/source.md': 'unknown'}):
            self.assertEqual(self.process(), (True, False, []))
        row = self.candidates[0]['sourceFingerprints']['fingerprints'][0]
        self.assertEqual(row['tracked_state'], 'unknown')
        self.assertEqual(len(self.calls), 1)

    def test_scan_id_changes_per_observation_of_the_same_bytes(self):
        self.assertEqual(self.process(), (True, False, []))
        self.assertEqual(self.process(), (True, False, []))
        first, second = [candidate['sourceFingerprints']['fingerprints'][0]
                         for candidate in self.candidates]
        self.assertNotEqual(first['scan_id'], second['scan_id'])
        self.assertEqual(first['source_sha'], second['source_sha'])

    def test_model_fingerprints_are_dropped_without_checkout(self):
        self.assertEqual(worker.process_job(self.project.root, self.config, self.config['compile'],
                                            self.job(), checkout=None), (True, False, []))
        self.assertNotIn('sourceFingerprints', self.candidates[0])

    def test_batch_supplies_selected_checkout_to_real_job(self):
        queue = self.project.root / '.memvane/compile/source-queue.jsonl'
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text(json.dumps(self.job()) + '\n')
        with patch.object(workspace, 'schema3_checkout', return_value=self.checkout), \
             patch.object(intake, 'reconcile_discards'), patch.object(intake, 'settle_job'), \
             patch.object(worker, 'recover_pending_refreshes'), patch.object(worker, '_run_verify_pass'):
            self.assertEqual(worker._main_locked(SimpleNamespace(json=False, flush_all=True),
                             self.project.root, self.config, self.config['compile'], queue, self.bound), 0)
        row = self.candidates[0]['sourceFingerprints']['fingerprints'][0]
        self.assertEqual((row['path'], row['source_sha']),
                         ('docs/source.md', self.candidates[0]['sourceRevisions'][0]['sha256']))

    def test_batch_reader_error_preserves_queue_without_extraction(self):
        queue = self.project.root / '.memvane/compile/source-queue.jsonl'
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text(json.dumps(self.job()) + '\n')
        with patch.object(workspace, 'schema3_checkout', side_effect=sqlite3.OperationalError('fixture busy')), \
             patch.object(intake, 'reconcile_discards'), patch.object(worker, 'recover_pending_refreshes'), \
             patch.object(worker, '_run_verify_pass'):
            self.assertEqual(worker._main_locked(SimpleNamespace(json=False, flush_all=True),
                             self.project.root, self.config, self.config['compile'], queue, self.bound), 0)
        self.assertEqual(self.calls, [])
        self.assertTrue(queue.read_text().strip())
        audit = self.project.root / '.memvane/compile/candidates.jsonl'
        self.assertEqual(json.loads(audit.read_text().splitlines()[-1])['reason'], 'checkout_unavailable')

    def test_writer_places_observed_fingerprint_in_verify_job(self):
        self.assertEqual(self.process(), (True, False, []))
        candidate = self.candidates[0]
        with patch.object(workspace, 'request_sync'), patch.object(workspace, 'append_activity'), \
             patch.object(compiler, 'judge_new_page_duplicate',
                          return_value=(None, None, {'outcome': 'unavailable', 'reason': 'fixture'})), \
             patch.object(compiler, 'find_wiki_semantic_match', return_value=(None, None)), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(compiler._compile_locked(self.project.root, self.config,
                             self.config['compile'], 'auto-wiki', self.project.wiki, candidate, False), 0)
        queue = self.project.root / '.memvane/compile/verify-queue.jsonl'
        job = json.loads(queue.read_text().splitlines()[0])
        observed, stored = candidate['sourceFingerprints'], job['sourceFingerprints']
        self.assertEqual(stored['viewId'], observed['viewId'])
        self.assertEqual(stored['fingerprints'], observed['fingerprints'])
        self.assertEqual(stored['scanId'], observed['fingerprints'][0]['scan_id'])
        self.assertEqual(stored['observedNs'], observed['fingerprints'][0]['observed_ns'])

    def test_non_git_and_unborn_checkout_have_no_head_and_untracked_source(self):
        for case in ('non-git', 'unborn'):
            with self.subTest(case=case):
                project = make_project(self.home, name=case, dirname=case, git=False)
                source = project.write_note('docs/note.md', 'synthetic source\n')
                if case == 'unborn':
                    project.git('init', '-q', '-b', 'main')
                binding = store.observe_binding(self.tmp / (case + '.sqlite'), scope_id='d' * 32,
                                                create_root=str(project.root))
                repo, worktree = binding['repository'], binding['worktree']
                capture = store.capture_view(str(project.root), repo, worktree, fresh=True)
                checkout = SimpleNamespace(matches=lambda value: store.capture_matches(value, repo, worktree,
                                                                                       fresh=True))
                revision = {'path': 'docs/note.md', **card.snapshot_bytes(source)[0]}
                spec = worker.source_fingerprints(project.root, checkout, capture, [revision], 1)
                row = spec['fingerprints'][0]
                self.assertEqual((row['head_oid'], row['tracked_state']), (None, 'untracked'))
                self.assertEqual(row['branch_key'], 'non-git' if case == 'non-git' else 'refs/heads/main')
