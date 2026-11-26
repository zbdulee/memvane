"""Selected compile cannot ACK work still held by queues or workers."""
import os
from types import SimpleNamespace
from unittest.mock import patch

from memvane.wiki.card import api as card
from memvane.wiki.compile import selected_execution as execution
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project


class SelectedExecutionQueue(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.directory = self.root / '.memvane/compile'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.source = self.directory / 'source-queue.jsonl'
        self.verify = self.directory / 'verify-queue.jsonl'
        self.bound = ({'projectRoot': str(self.root), 'recallSettings': {'skipPaths': []}},
                      {'productRouteAllowed': True})

    def test_durable_worker_does_not_ack_pending_source(self):
        from tests.support.queue_files import pending_text, replace_queue
        replace_queue(self.source, [{'source': 'pending'}])
        child = SimpleNamespace(returncode=0, stdout='{"processed":0,"remaining":1}', stderr='')
        with patch.object(execution, 'selected', return_value=self.bound), \
             patch.object(execution, 'recheck'), patch.object(workspace, 'claim_lifecycle', return_value=None), \
             patch.object(execution.subprocess, 'run', return_value=child) as spawn, \
             patch.object(card, 'unresolved_pending_refreshes_strict', return_value=[]):
            result = execution.run(self.root)
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['processed'], 0)
        self.assertIn('wiki-compile-worker', spawn.call_args.args[0])
        self.assertIn('pending', pending_text(self.source))

    def test_running_compile_worker_is_not_idle(self):
        held = workspace.try_compile_run_lock(self.root)
        self.assertIsNotNone(held)
        try:
            with patch.object(card, 'unresolved_pending_refreshes_strict', return_value=[]):
                self.assertIs(execution._compile_idle(self.root, self.source, self.verify, self.bound), False)
        finally:
            os.close(held)
        with patch.object(card, 'unresolved_pending_refreshes_strict', return_value=[]):
            self.assertIs(execution._compile_idle(self.root, self.source, self.verify, self.bound), True)

    def test_orphan_claim_prevents_ack_until_native_consumer_finishes(self):
        from tests.support.queue_files import replace_queue
        from tests.support.wiki_handoff import claimed_jobs, hold_claimed
        pid = 4242421
        replace_queue(self.source, [{'source': 'orphan'}])
        hold_claimed(self.source, 1, pid)
        child = SimpleNamespace(returncode=0, stdout='{"processed":0,"remaining":1}', stderr='')
        with patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': str(pid)}), \
             patch.object(execution, 'selected', return_value=self.bound), \
             patch.object(execution, 'recheck'), patch.object(workspace, 'claim_lifecycle', return_value=None), \
             patch.object(execution.subprocess, 'run', return_value=child) as spawn, \
             patch.object(card, 'unresolved_pending_refreshes_strict', return_value=[]):
            result = execution.run(self.root)
        self.assertEqual(result['state'], 'pending')
        spawn.assert_called_once()
        self.assertEqual(claimed_jobs(self.source), [{'source': 'orphan'}])
        self.assertFalse(self.source.exists())
        with patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': str(pid)}):
            recovered = workspace.claim_queue(self.source, private=True)
        rows = workspace.read_queue(recovered)
        self.assertEqual([job for _, job in rows], [{'source': 'orphan'}])
        rows.finish(self.source, recovered, [])
        self.assertFalse(workspace.queue_busy(self.source))

    def test_live_verify_claim_prevents_idle(self):
        from memvane.wiki.pipeline import api as pipeline
        with pipeline.open_pipeline(self.root) as store:
            store.append_jobs('verify', [{'targetPath': 'card.md'}])
            store.claim_verify_batch(os.getppid(), 'live', budget=1)
        with patch.object(card, 'unresolved_pending_refreshes_strict', return_value=[]):
            self.assertIs(execution._compile_idle(self.root, self.source, self.verify, self.bound), False)
        with pipeline.open_pipeline(self.root) as store:
            self.assertEqual([row['state'] for row in store.open_verify_jobs()], ['claimed'])

    def test_claim_racing_idle_check_keeps_work_pending(self):
        from tests.support.queue_files import replace_queue
        replace_queue(self.source, [{'source': 'pending'}])
        def claim_while_checking(project):
            workspace.claim_queue(self.source)
            return []
        with patch.object(card, 'unresolved_pending_refreshes_strict', side_effect=claim_while_checking):
            self.assertIs(execution._compile_idle(self.root, self.source, self.verify, self.bound), False)

