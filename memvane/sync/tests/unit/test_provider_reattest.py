"""The sync owner, never the hook, publishes provider execution evidence."""
from contextlib import contextmanager
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from memvane.sync import product_sync_worker as worker
from memvane.kernel.errors import IndexContractError


class ReattestOwner(TestCase):
    def setUp(self):
        self.decision = {'state':'candidate', 'storePath':'/private/tmp/synthetic-store',
                         'journalPath':'/private/tmp/synthetic-state/journal.sqlite'}
        self.manifest = {'workerSha256':'a'*64,'providerArtifactSha256':'b'*64,
                         'workerPath':'/tmp/worker','providerPath':'/tmp/provider'}
        self.proof = {'journalPath':self.decision['journalPath'], 'writerFloor':'1.0.0',
                      'providerManifest':self.manifest, 'providerAvailable':False}
        self.order = []

    @contextmanager
    def owner(self, path, wait):
        self.order.append('owner_enter')
        try:
            yield object()
        finally:
            self.order.append('owner_exit')

    def patches(self):
        from contextlib import ExitStack
        from memvane.provider import api as provider
        from memvane.journal import api as journal
        from memvane.diagnostics import api as diagnostics
        from memvane.kernel import memvane_execution_guard as guard
        from memvane.kernel import memvane_version
        stack = ExitStack()
        stack.enter_context(patch.object(provider, 'fixture_port', return_value=None))
        stack.enter_context(patch.object(worker, 'select_route', return_value=self.decision))
        stack.enter_context(patch.object(worker, 'sync_owner', self.owner))
        stack.enter_context(patch.object(journal, 'verify_selected_route', return_value=self.proof))
        stack.enter_context(patch.object(guard, 'require_cached_plugin_for_live_write'))
        stack.enter_context(patch.object(memvane_version, 'writer_floor_allows', return_value=True))
        stack.enter_context(patch.object(provider, 'reattestation_current', return_value=({'worker':'c'*64,'provider':'b'*64}, {})))
        stack.enter_context(patch.object(provider, 'reattestation_valid', return_value=False))
        self.update = stack.enter_context(patch.object(provider, 'reattestation_update', side_effect=lambda *_: self.order.append('write')))
        self.emit = stack.enter_context(patch.object(diagnostics, 'emit'))
        return stack

    def test_write_under_owner_and_preserve_original_pair(self):
        with self.patches():
            worker._prepare_provider_reattest(Path('/private/tmp/project'))
        self.assertEqual(self.order, ['owner_enter','write','owner_exit'])
        self.assertEqual(self.update.call_args.args[1]['workerSha256'], 'a'*64)
        self.assertEqual(self.emit.call_args.kwargs['outcome'], 'accepted')

    def test_identity_refusal_keeps_original_path_blocked(self):
        with self.patches():
            self.update.side_effect = ValueError('reattest_identity_changed')
            with self.assertRaisesRegex(IndexContractError, 'provider_identity_changed'):
                worker._prepare_provider_reattest(Path('/private/tmp/project'))
        self.assertEqual(self.emit.call_args.kwargs['outcome'], 'rejected')
