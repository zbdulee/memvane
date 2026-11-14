"""A deferred compile owner retains work and exposes bounded reasons."""
from contextlib import redirect_stdout
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.wiki.compile import selected_execution as execution, wiki_compile_worker as worker
from memvane.wiki.intake import api as intake
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project


class OwnerBusy(FixtureCase):
    def setUp(self):
        super().setUp()
        self.root = make_project(self.home, git=False).root
        (self.root / '.memvane/compile').mkdir()
        self.bound = ({'projectRoot': str(self.root)}, {})

    def test_worker_main_reports_owner_busy(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['worker', '--cwd', str(self.root), '--json']), \
             patch.object(workspace, 'selected', side_effect=ResourceDeferred('owner_busy')), \
             redirect_stdout(output):
            self.assertEqual(worker.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {'state': 'deferred', 'reason': 'owner_busy'})

    def test_post_lock_proof_deferral_is_not_swallowed(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['worker', '--cwd', str(self.root), '--json']), \
             patch.object(workspace, 'selected', side_effect=[self.bound, ResourceDeferred('owner_busy')]), \
             redirect_stdout(output):
            self.assertEqual(worker.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {'state': 'deferred', 'reason': 'owner_busy'})

    def test_parent_accepts_only_bounded_worker_deferrals(self):
        for reason in ('owner_busy', 'untrusted', ['owner_busy'], None):
            child = SimpleNamespace(returncode=0, stdout=json.dumps({'state': 'deferred',
                                     'reason': reason, 'processed': 0}), stderr='')
            with self.subTest(reason=reason), patch.object(execution, 'selected', return_value=self.bound), \
                 patch.object(execution, 'recheck'), patch.object(workspace, 'claim_lifecycle', return_value=None), \
                 patch.object(execution.subprocess, 'run', return_value=child):
                result = execution.run(self.root)
            expected = reason if reason == 'owner_busy' else 'compile_worker_result_invalid'
            self.assertEqual(result, {'state': 'deferred', 'reason': expected})

    def test_worker_error_and_unknown_deferral_do_not_disclose_details(self):
        for stderr, expected in (('ValueError: /private/sensitive', 'worker_value_error'),
                                 ('RuntimeError: secret-token', 'worker_runtime_error')):
            child = SimpleNamespace(returncode=1, stdout='', stderr=stderr)
            with self.subTest(stderr=stderr), patch.object(execution, 'selected', return_value=self.bound), \
                 patch.object(execution, 'recheck'), patch.object(workspace, 'claim_lifecycle', return_value=None), \
                 patch.object(execution.subprocess, 'run', return_value=child):
                result = execution.run(self.root)
            self.assertEqual(result, {'state': 'deferred', 'reason': 'compile_worker_failed', 'detail': expected})
        with patch.object(execution, '_run', side_effect=ResourceDeferred('/private/secret')):
            self.assertEqual(execution.run(self.root), {'state': 'deferred', 'reason': 'resource_deferred'})

    def test_claimed_job_is_requeued_when_owner_becomes_busy(self):
        queue = self.root / 'queue.jsonl'
        raw = json.dumps({'sourcePath': 'synthetic.md'})
        queue.write_text(raw + '\n')
        with patch.object(worker, 'recover_pending_refreshes'), patch.object(intake, 'reconcile_discards'), \
             patch.object(worker, 'dedup_jobs', side_effect=lambda rows: (rows, [])), \
             patch.object(worker, 'batch_ready', return_value=True), \
             patch.object(worker, 'process_job', side_effect=ResourceDeferred('owner_busy')):
            with self.assertRaisesRegex(ResourceDeferred, '^owner_busy$'):
                worker._main_locked(SimpleNamespace(json=True, flush_all=True), self.root, {}, {}, queue, None)
        self.assertEqual(queue.read_text().splitlines(), [raw])
        self.assertFalse(list(self.root.glob('queue.jsonl.claimed.*')))
