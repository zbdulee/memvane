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
        self.source.write_text('{"source":"pending"}\n')
        child = SimpleNamespace(returncode=0, stdout='{"processed":0,"remaining":1}', stderr='')
        with patch.object(execution, 'selected', return_value=self.bound), \
             patch.object(execution, 'recheck'), patch.object(workspace, 'claim_lifecycle', return_value=None), \
             patch.object(execution.subprocess, 'run', return_value=child) as spawn, \
             patch.object(card, 'unresolved_pending_refreshes_strict', return_value=[]):
            result = execution.run(self.root)
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['processed'], 0)
        self.assertIn('wiki-compile-worker', spawn.call_args.args[0])
        self.assertIn('pending', self.source.read_text())

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

    def test_claim_racing_idle_check_keeps_work_pending(self):
        self.source.write_text('{"source":"pending"}\n')
        claim = self.source.with_name(self.source.name + '.claimed.%s.%s' % (os.getpid(), 'f' * 32))
        def claim_while_checking(project):
            os.replace(self.source, claim)
            return []
        with patch.object(card, 'unresolved_pending_refreshes_strict', side_effect=claim_while_checking):
            self.assertIs(execution._compile_idle(self.root, self.source, self.verify, self.bound), False)
        self.assertTrue(claim.exists())
