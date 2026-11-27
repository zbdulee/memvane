"""Selected wiki route and wake authorization through public boundaries."""
from unittest.mock import patch

from memvane.journal import api as journal
from memvane.runtime import api as runtime
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project


class SelectedWikiChecks(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.decision = {
            'state': 'candidate', 'projectRoot': str(self.project.root),
            'installId': 'a' * 32, 'scopeId': 'c' * 32,
            'storeIncarnation': 'd' * 32,
            'collections': [
                {'name': 'raw', 'role': 'raw', 'sourceRoot': str(self.project.root)},
                {'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.project.wiki)},
            ],
        }
        self.proof = {'productRouteAllowed': True}

    def test_selected_route_requires_installer_proof(self):
        with patch.object(journal, 'select_route', return_value=self.decision), \
             patch.object(journal, 'verify_selected_route_for_write', return_value=self.proof):
            bound = workspace.selected(self.project.root)
        self.assertEqual(bound, (self.decision, self.proof))
        self.assertEqual(workspace.route_identity(bound), {
            'installId': 'a' * 32, 'scopeId': 'c' * 32, 'storeIncarnation': 'd' * 32})
        with patch.object(journal, 'select_route', return_value={'state': 'blocked'}):
            with self.assertRaisesRegex(ValueError, 'route_unavailable'):
                workspace.selected(self.project.root)

    def test_route_change_prevents_selected_wake(self):
        with patch.object(journal, 'verify_selected_route_for_write',
                          return_value={'productRouteAllowed': False}), \
             patch.object(runtime, 'request') as request:
            with self.assertRaisesRegex(ValueError, 'proof_changed'):
                workspace.request_compile(self.project.root, (self.decision, self.proof))
            request.assert_not_called()

    def test_selected_wakes_use_correct_kind(self):
        with patch.object(journal, 'verify_selected_route_for_write', return_value=self.proof), \
             patch.object(runtime, 'request', return_value={'status': 'queued'}) as request:
            bound = (self.decision, self.proof)
            workspace.request_compile(self.project.root, bound)
            self.assertTrue(workspace.request_sync(self.project.root, bound))
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ['laya-compile', 'laya-sync'])

    def test_live_owner_claim_keeps_the_wake_pending(self):
        import os
        from memvane.wiki.pipeline import api as pipeline
        queue = self.project.root / '.memvane/compile/verify-queue.jsonl'
        queue.parent.mkdir(parents=True)
        with pipeline.open_pipeline(self.project.root) as store:
            store.append_jobs('verify', [{'targetPath': 'card.md'}])
            store.claim_verify_batch(os.getppid(), 'live', budget=1)
        self.assertTrue(workspace.queue_busy(queue))
        self.assertFalse(queue.exists())

    def test_a_dead_owners_row_stays_pending_until_the_next_verify_run_returns_it(self):
        from memvane.wiki.pipeline import api as pipeline
        queue = self.project.root / '.memvane/compile/verify-queue.jsonl'
        queue.parent.mkdir(parents=True)
        with pipeline.open_pipeline(self.project.root) as store:
            store.append_jobs('verify', [{'targetPath': 'card.md'}])
            store.claim_verify_batch(4242421, 'dead', budget=1)
        self.assertTrue(workspace.queue_busy(queue))
        with pipeline.open_pipeline(self.project.root) as store:
            self.assertEqual(store.recover_dead_claims('verify', lambda pid: False)['requeued'], 1)
            row = store.claim_verify_batch(1, 'next', budget=1)['claimed'][0]
            self.assertEqual((row['payload'], row['attempts']), ({'targetPath': 'card.md'}, 2))
            self.assertTrue(store.finish_verify_job(row['jobId'], 'next', 'done', 'verified'))
        self.assertFalse(workspace.queue_busy(queue))
        self.assertFalse(queue.exists())
