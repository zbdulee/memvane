"""Codex's sparse marketplace and complete cache, under disposable homes only."""
from __future__ import annotations

from contextlib import ExitStack
import copy
import errno
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
FIXTURE = runpy.run_path(str(HERE / 'laya-product-bundle-checks.py'))
import event_log  # noqa: E402
import laya_active_upgrade as active  # noqa: E402
import laya_bundle_upgrade as auto  # noqa: E402
import laya_product_bundle as bundle  # noqa: E402
import laya_product_install as product  # noqa: E402

git = FIXTURE['_git']


class CodexSparseChecks(unittest.TestCase):
    def setUp(self):
        # Host cache fixtures live below the caller's isolated test HOME. The
        # shared install/runtime fixture uses a short /tmp path for its socket.
        tmp = tempfile.TemporaryDirectory(prefix='codex-sparse-', dir=os.environ['HOME'])
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        fixture = FIXTURE['ProductBundleChecks']()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.state, self.project = fixture.install, fixture.project
        env = mock.patch.dict(os.environ, {'HOME': str(self.home),
                                           'GIT_CONFIG_GLOBAL': os.devnull,
                                           'GIT_CONFIG_NOSYSTEM': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.old_commit = fixture.commit1
        marketplace = fixture.source / '.agents/plugins/marketplace.json'
        marketplace.parent.mkdir(parents=True)
        marketplace.write_text('{"name":"memvane-marketplace","plugins":[]}')
        git(fixture.source, 'add', '.')
        git(fixture.source, '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'marketplace')
        self.commit = git(fixture.source, 'rev-parse', 'HEAD')
        self.source = self.home / '.codex/.tmp/marketplaces/memvane-marketplace'
        self.installed = self.home / '.codex/plugins/cache/memvane-marketplace/memvane/0.43.22'
        for path in (self.source, self.installed):
            path.parent.mkdir(parents=True, exist_ok=True)
            git(path.parent, 'clone', '-q', str(fixture.source), str(path))
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch('platform.system', return_value='Darwin'))
        stack.enter_context(mock.patch('platform.machine', return_value='arm64'))
        fixture.laya_runtime()
        self.baseline = self.review()
        git(self.source, 'sparse-checkout', 'init', '--cone')
        git(self.source, 'sparse-checkout', 'set', '.agents/plugins')
        self.assertFalse((self.source / 'core').exists())
        self.assertEqual(git(self.source, 'diff', 'HEAD'), '')

    def review(self):
        return product.review_local_provider(
            source_root=self.source, plugin_root=self.installed,
            state_root=self.state, commit_id=self.commit, dimension=3)

    def adopt(self):
        return bundle.adopt_host_loaded_plugin(
            self.source, self.installed, commit_id=self.commit)

    def test_sparse_source_provider_review_matches_full_checkout(self):
        self.assertEqual(auto.select_source(self.installed, self.home),
                         (self.source, self.commit))
        self.assertEqual(self.review(), self.baseline)
        self.assertFalse((self.source / 'core').exists())

    def test_sparse_source_seals_every_installed_blob_without_expanding_checkout(self):
        proof = self.adopt()
        self.assertEqual(proof['sourceCommit'], self.commit)
        self.assertIn('core/config.py', proof['includedFiles'])
        self.assertEqual(bundle.inspect_full_plugin_candidate(self.installed), proof)
        self.assertFalse((self.source / 'core').exists())
        self.assertEqual(git(self.source, 'diff', 'HEAD'), '')

    def test_sparse_source_materialized_dirty_blob_is_rejected(self):
        path = self.source / 'core/config.py'
        path.parent.mkdir()
        path.write_text('# changed materialized source\n')
        with self.assertRaisesRegex(bundle.BundleEvidenceError, 'source_not_exact_commit'):
            self.adopt()
        self.assertFalse((self.installed / '.laya-product').exists())

    def test_sparse_source_materialized_wrong_exec_mode_is_rejected(self):
        path = self.source / 'core/config.py'
        path.parent.mkdir()
        shutil.copyfile(self.installed / 'core/config.py', path)
        path.chmod(0o700)
        with self.assertRaisesRegex(bundle.BundleEvidenceError, 'source_not_exact_commit'):
            self.adopt()

    def test_sparse_source_symlink_parent_is_rejected(self):
        (self.source / 'core').symlink_to(self.installed / 'core', target_is_directory=True)
        with self.assertRaisesRegex(bundle.BundleEvidenceError, 'unsafe_source_parent'):
            self.adopt()

    def test_unmarked_source_deletion_retains_path_and_errno(self):
        path = self.source / 'README.md'
        path.unlink()
        with self.assertRaisesRegex(bundle.BundleEvidenceError, 'bundle_file_unavailable') as ctx:
            self.adopt()
        self.assertEqual(ctx.exception.detail, {'path': str(path), 'errno': errno.ENOENT})
        self.assertFalse((self.installed / '.laya-product').exists())

    def test_existing_directory_is_not_a_missing_installed_file(self):
        path = self.installed / 'LICENSE'
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'installed_file_set_not_exact_commit') as ctx:
            self.adopt()
        reason, detail = auto._bundle_failure(ctx.exception, self.installed)
        self.assertEqual(reason, 'installed_file_set_not_exact_commit')
        self.assertNotEqual(detail.get('file', {}).get('errno'), errno.ENOENT)

    @unittest.skipIf(os.geteuid() == 0, 'root bypasses directory access permissions')
    def test_unreadable_directory_preserves_the_access_error(self):
        directory = self.installed / '.codex-plugin'
        original_mode = directory.stat().st_mode & 0o777
        directory.chmod(0)
        try:
            with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                        'installed_file_set_not_exact_commit') as ctx:
                self.adopt()
            reason, detail = auto._bundle_failure(ctx.exception, self.installed)
            self.assertEqual(reason, 'installed_file_set_not_exact_commit')
            self.assertEqual(detail.get('file', {}).get('errno'), errno.EACCES)
        finally:
            directory.chmod(original_mode)

    def test_partial_codex_cache_defers_visibly_and_recovers_after_file_returns(self):
        path = self.installed / 'core/laya_local_embed_provider.py'
        path.unlink()
        old = copy.deepcopy(self.baseline['reviewedProvider'])
        old['providerManifest']['providerPath'] = '/old/core/laya_local_embed_provider.py'
        old['providerManifest']['workerPath'] = '/old/core/laya_local_embed_worker.mjs'
        identity = {'pluginRoot': '/old', 'sourceCommit': self.old_commit}
        with (mock.patch.object(auto, '_project_states', return_value=[self.state]),
              mock.patch.object(auto, 'classify', return_value=('active', identity)),
              mock.patch.object(auto, '_read_attestation', return_value=old),
              mock.patch.object(auto, '_collect_shared'),
              mock.patch.object(auto, '_learning_upkeep', return_value={}),
              mock.patch.object(product, '_request_build_wake', return_value={'status': 'queued'}),
              mock.patch.object(active, 'upgrade', side_effect=lambda **kw: {
                  'status': 'active_upgrade_complete',
                  'proof': bundle.adopt_host_loaded_plugin(
                      kw['source_root'], kw['plugin_root'], commit_id=kw['commit_id'])}) as switch):
            result = auto.run(self.project, root=self.installed, home=self.home)[0]
            self.assertEqual((result['status'], result['reason']),
                             ('deferred', 'bundle_file_missing'))
            status = auto.read_status(self.state)
            self.assertEqual(status['attempts'], 0)
            self.assertEqual(status['file'], {'path': str(path), 'errno': errno.ENOENT})
            switch.assert_not_called()
            self.assertFalse((self.state / active.INTENT).exists())
            line = auto.notice_line(status, 'installed_bundle_changed')
            self.assertIn('bundle_file_missing', line)
            self.assertIn('logs', line)
            self.assertNotIn('busy lane', line)
            events = [r for r in event_log.read(self.state) if r['event'] == 'bundle.upgrade']
            self.assertEqual(events[-1]['outcome'], 'deferred')
            self.assertEqual(events[-1]['file'], status['file'])
            self.assertTrue(auto._due(status, str(self.installed),
                                     status['updatedNs'] + 121 * 10**9))
            shutil.copyfile(self.fixture.source / 'core/laya_local_embed_provider.py', path)
            result = auto.run(self.project, root=self.installed, home=self.home)[0]
            self.assertEqual(result['status'], 'active_upgrade_complete', result)
            self.assertEqual(auto.read_status(self.state)['status'], 'complete')
            self.assertNotIn('file', auto.read_status(self.state))


if __name__ == '__main__':
    unittest.main()
