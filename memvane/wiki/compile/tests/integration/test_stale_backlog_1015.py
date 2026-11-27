"""Stale installs cannot starve current compile work or consume retry history."""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.wiki.compile import selected_execution as execution
from memvane.wiki.compile import wiki_compile_worker as worker
from memvane.wiki.engines import api as engines
from memvane.wiki.intake import api as intake
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config
from tests.support.queue_files import pending_text, replace_queue


class StaleSourceBacklog(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        self.paths = ['docs/current-%02d.md' % n for n in range(11)]
        for path in self.paths:
            self.project.write_note(path, 'Synthetic source for compile scheduling.\n')
        self.project.git('add', '--', '.memvane/settings.json', 'docs')
        self.project.git('commit', '-qm', 'fixture')
        build_install(self, self.project)
        self.config = expanded(self.project, compile={
            'budget': {'extractorPerRun': 10},
            'extractor': {'builtins': ['claude']}, 'verify': {'enabled': False}})
        use_config(self, self.project, self.config)
        self.bound = workspace.selected(self.project.root)
        self.identity = workspace.route_identity(self.bound)
        self.queue = self.project.root / '.memvane/compile/source-queue.jsonl'
        self.queue.parent.mkdir(parents=True, exist_ok=True)

    def job(self, path, *, stale=False, ts='2026-01-01T00:00:00Z'):
        identity = dict(self.identity)
        if stale:
            identity['installId'] = '0' * 32
        return {'cwd': str(self.project.root), 'engine': 'claude', 'ts': ts,
                'trigger': 'post_tool_source', 'selectedRoute': identity,
                'source': {'kind': 'file', 'path': path,
                           'collection': self.project.raw_name}}

    def write_jobs(self, jobs):
        replace_queue(self.queue, ''.join(json.dumps(job) + '\n' for job in jobs))

    def remaining_jobs(self):
        # Queued rows and rows a failed return left claimed.
        return [json.loads(line) for line in pending_text(self.queue).splitlines() if line.strip()]

    def run_pass(self):
        calls = []

        def extract(argv, payload, timeout, root):
            calls.append(payload['source']['path'])
            return {'candidates': []}, None, 0

        output = io.StringIO()
        with patch.object(engines, 'run_extractor', side_effect=extract), \
             patch.object(worker, 'gather_similar_pages', return_value=[]), \
             patch.object(worker, '_run_verify_pass'), \
             patch.object(workspace, 'request_compile'), redirect_stdout(output):
            worker._main_locked(argparse.Namespace(json=True, flush_all=True), self.project.root,
                                self.config, self.config['compile'], self.queue, self.bound)
        return json.loads(output.getvalue()), calls

    def test_old_install_prefix_does_not_consume_current_extractor_budget(self):
        old = [self.job('docs/old-%04d.md' % n, stale=True) for n in range(2281)]
        for job in old:
            job['selectedRoute'].pop('storeIncarnation')  # persisted 0.x queue format
        self.write_jobs(old + [self.job(path) for path in self.paths])
        report, calls = self.run_pass()
        self.assertEqual(calls, self.paths[:10])
        self.assertEqual([row['source']['path'] for row in self.remaining_jobs()], self.paths[10:])
        self.assertEqual((report['completed'], report['staleDropped']), (10, 2281))
        from memvane.wiki.pipeline import api as pipeline
        audit = pipeline.log_rows(self.project.root, 'candidate')
        stale = [row for row in audit if row.get('reason') == 'selected_route_identity_changed']
        self.assertEqual(len(stale), 2281)
        self.assertTrue(all(row['action'] == 'skipped' for row in stale))

    def test_newer_stale_duplicate_cannot_replace_current_job_before_filtering(self):
        current = self.job(self.paths[0])
        stale = self.job(self.paths[0], stale=True, ts='2026-01-02T00:00:00Z')
        self.write_jobs([current, stale])
        report, calls = self.run_pass()
        self.assertEqual(calls, self.paths[:1])
        self.assertEqual(report['staleDropped'], 1)
        self.assertEqual(self.remaining_jobs(), [])

    def test_same_install_scope_drift_is_retained_not_declared_stale_progress(self):
        drift = self.job(self.paths[0])
        drift['selectedRoute']['scopeId'] = '1' * 32
        self.write_jobs([drift])
        report, calls = self.run_pass()
        self.assertEqual(calls, [])
        self.assertEqual(self.remaining_jobs(), [drift])
        self.assertEqual(report.get('completed', 0) + report.get('staleDropped', 0), 0)

    def test_identity_absent_job_is_retained(self):
        job = self.job(self.paths[0])
        job.pop('selectedRoute')
        self.write_jobs([job])
        report, calls = self.run_pass()
        self.assertEqual(calls, [])
        self.assertEqual(self.remaining_jobs(), [job])
        self.assertEqual(report.get('completed', 0) + report.get('staleDropped', 0), 0)

    def test_selected_proof_failure_returns_jobs_to_normal_queue(self):
        old = self.job('docs/old.md', stale=True)
        self.write_jobs([old])
        with patch.object(workspace, 'recheck', side_effect=ValueError('synthetic route drift')):
            try:
                self.run_pass()
            except ValueError:
                pass
        self.assertEqual(self.remaining_jobs(), [old])
        self.assertEqual(list(self.queue.parent.glob('source-queue.jsonl.claimed.*')), [])

    def test_repeated_partition_owner_busy_never_consumes_orphan_retry_count(self):
        jobs = [self.job('docs/old.md', stale=True), self.job(self.paths[0])]
        self.write_jobs(jobs)
        with patch.object(worker, 'recover_pending_refreshes'), \
             patch.object(intake, 'reconcile_discards'), \
             patch.object(workspace, 'recheck', side_effect=ResourceDeferred('owner_busy')):
            for _ in range(4):
                with self.assertRaisesRegex(ResourceDeferred, 'owner_busy'):
                    self.run_pass()
        self.assertEqual(self.remaining_jobs(), jobs)
        self.assertEqual(list(self.queue.parent.glob('source-queue.jsonl.claimed.*')), [])
        self.assertEqual([row.get('_requeue_count', 0) for row in self.remaining_jobs()], [0, 0])

    def test_final_owner_busy_requeues_stale_and_remaining_without_completed_job(self):
        old = self.job('docs/old.md', stale=True)
        completed, remaining = self.job(self.paths[0]), self.job(self.paths[1])
        self.config['compile']['budget']['extractorPerRun'] = 1
        self.write_jobs([old, completed, remaining])
        recheck, settle = workspace.recheck, intake.settle_job
        settled = []

        def record_settled(bound, job):
            result = settle(bound, job)
            settled.append(job['source']['path'])
            return result

        def busy_after_completion(bound, **kwargs):
            if settled:
                raise ResourceDeferred('owner_busy')
            return recheck(bound, **kwargs)

        with patch.object(intake, 'settle_job', side_effect=record_settled), \
             patch.object(workspace, 'recheck', side_effect=busy_after_completion):
            with self.assertRaisesRegex(ResourceDeferred, 'owner_busy'):
                self.run_pass()
        self.assertEqual(settled, self.paths[:1])
        self.assertCountEqual(self.remaining_jobs(), [old, remaining])
        self.assertEqual(list(self.queue.parent.glob('source-queue.jsonl.claimed.*')), [])
        self.assertEqual(len(self.remaining_jobs()), 2)
        report, calls = self.run_pass()
        self.assertEqual(calls, self.paths[1:2])
        self.assertEqual((report['completed'], report['staleDropped']), (1, 1))
        self.assertEqual(self.remaining_jobs(), [])

    def test_stale_backfill_does_not_ack_current_cohort(self):
        old = self.job('docs/old.md', stale=True)
        old['initialBackfill'] = 'old-cohort'
        self.write_jobs([old])
        with patch.object(intake, 'settle_job', side_effect=AssertionError('foreign cohort ACK')):
            report, calls = self.run_pass()
        self.assertEqual(calls, [])
        self.assertEqual(report['staleDropped'], 1)
        self.assertEqual(self.remaining_jobs(), [])
