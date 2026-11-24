"""A selected compile wake hands pre-upgrade work to any 1.x consumer."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from memvane.wiki.compile import selected_execution as execution
from memvane.wiki.compile.tests.integration import test_pending_handoff as handoff
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase


class UpgradeQueue(FixtureCase):
    prepare = handoff.PendingHandoff.prepare
    write = handoff.PendingHandoff.write
    run_tag = handoff.PendingHandoff.run_tag

    def seed(self, bundle):
        repo = Path(__file__).resolve().parents[5]
        child = repo / 'memvane/wiki/compile/tests/tagged_writer.py'
        subprocess.run([sys.executable, '-B', str(child), str(bundle), str(self.root),
                        json.dumps(self.config), json.dumps(self.bound)], cwd=bundle,
                       env={**os.environ, 'PYTHONPATH': str(bundle)}, capture_output=True, check=True)
        jobs = [json.loads(line) for line in self.queue.read_text().splitlines()]
        self.assertEqual(len(jobs), 2)
        self.assertNotEqual(jobs[0]['pendingPath'], jobs[1]['pendingPath'])
        self.assertGreater(jobs[0]['ts'], jobs[1]['ts'])
        return jobs

    def archive_writer(self):
        repo = Path(__file__).resolve().parents[5]
        bundle = self.tmp / 'legacy-writer'
        bundle.mkdir()
        archive = subprocess.check_output(['git', '-C', str(repo), 'archive', 'v1.1.0', 'memvane', 'package.json'])
        subprocess.run(['tar', '-x', '-C', str(bundle)], input=archive, check=True)
        return bundle

    def wake(self):
        worker = SimpleNamespace(returncode=0, stdout='{"processed":0}', stderr='')
        with patch.object(execution, 'selected', return_value=self.bound), patch.object(execution, 'recheck'), \
             patch.object(workspace, 'recheck'), \
             patch.object(workspace, 'claim_lifecycle', return_value=None), \
             patch.object(execution.subprocess, 'run', return_value=worker):
            return execution.run(self.root)

    def test_legacy_queue_first_compile_wake_protects_current_revision_for_every_tag(self):
        writer = self.archive_writer()
        for tag in ('v1.0.22', 'v1.0.23', 'v1.0.24', 'v1.1.0'):
            with self.subTest(tag=tag):
                self.prepare(tag)
                old, current = self.seed(writer)
                old_path = self.root / old['pendingPath']
                old_bytes = old_path.read_bytes()  # The legacy writer left no full-image ownership proof.
                self.wake()
                self.wake()  # repeated wakes keep the same work available
                self.assertEqual([json.loads(row) for row in self.queue.read_text().splitlines()], [current])
                self.assertEqual(old_path.read_bytes(), old_bytes)
                self.assertIn('pending_ownership_unproved', self.queue.with_name('verify-log.jsonl').read_text())
                result = self.run_tag(tag)
                self.assertEqual(result, {'remaining': 0, 'calls': 1})
                self.assertIn(current['sourceRevisions'][0]['sha256'], self.target.read_text())
                self.assertNotIn(old['sourceRevisions'][0]['sha256'], self.target.read_text())

    def test_active_claim_is_preserved_and_a_returned_batch_is_normalized(self):
        self.prepare('claimed')
        _, current = self.seed(self.archive_writer())
        claim = workspace.claim_queue(self.queue)
        original = claim.read_bytes()
        self.wake()
        self.assertEqual(claim.read_bytes(), original)
        self.assertEqual(self.queue.read_bytes(), b'')
        workspace.requeue_lines(self.queue, original.decode().splitlines())
        claim.unlink()
        self.wake()
        self.assertEqual(self.run_tag('v1.1.0'), {'remaining': 0, 'calls': 1})
        self.assertIn(current['sourceRevisions'][0]['sha256'], self.target.read_text())

    def test_wake_retries_the_last_audit_after_an_old_verifier_drains_the_queue(self):
        self.prepare('audit-after-drain')
        self.assertEqual(self.write('Older source.'), 0)
        previous = self.root / json.loads(self.queue.read_text())['pendingPath']
        ledger = self.queue.with_name('verify-deleted.jsonl')
        ledger.mkdir()
        self.assertEqual(self.write('Newest source.'), 0)
        # The previous verifier must have a writable audit ledger to start.
        ledger.rmdir()
        self.assertEqual(self.run_tag('v1.1.0'), {'remaining': 0, 'calls': 1})
        ledger.mkdir()
        self.assertEqual(self.wake()['state'], 'deferred')
        self.assertTrue(previous.exists())
        ledger.rmdir()
        self.assertEqual(self.wake()['state'], 'drained')
        self.assertFalse(previous.exists())
        self.assertEqual(len(ledger.read_text().splitlines()), 1)
