#!/usr/bin/env python3
"""Automatic bundle upgrade worker: source rule, provider rule, hosts, status.

Synthetic HOME, git repositories and state roots only. No model, host plugin
cache or real laya-db install is read or written.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_bundle_upgrade as auto  # noqa: E402
import laya_install_journal as journal  # noqa: E402

PROFILE = {'profileId': 'f' * 64, 'providerArtifactSha256': 'b' * 64,
           'providerRelativePath': 'core/laya_local_embed_provider.py'}


def git(root, *args):
    return subprocess.run(('git', '-C', str(root), *args), check=True,
                          capture_output=True, text=True).stdout.strip()


def repo(path, files):
    path.mkdir(parents=True)
    git(path, 'init', '-q')
    git(path, 'config', 'user.email', 'fixture@example.invalid')
    git(path, 'config', 'user.name', 'fixture')
    return commit(path, files)


def commit(path, files):
    for name, body in files.items():
        (path / name).write_text(body)
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'fixture')
    return git(path, 'rev-parse', 'HEAD')


def manifest(root, **changes):
    value = {'approvedProviderId': 'p', 'providerArtifactSha256': 'b' * 64,
             'providerRelativePath': 'core/laya_local_embed_provider.py',
             'providerPath': root + '/core/laya_local_embed_provider.py',
             'workerPath': root + '/core/laya_local_embed_worker.mjs',
             'workerSha256': 'c' * 64, 'serviceSocketPath': '/s/runtime/laya-embed.sock',
             'nodePath': '/n/node', 'nodeSha256': 'd' * 64, 'modelPath': '/m/model.gguf',
             'modelSha256': 'e' * 64, 'dimension': 768}
    value.update(changes)
    return value


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='laya-bundle-auto-')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.state = self.base / 'state'
        self.state.mkdir(mode=0o700)
        self.project = self.base / 'project'
        self.project.mkdir()
        env = mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': os.devnull,
                                           'GIT_CONFIG_NOSYSTEM': '1'})
        env.start()
        self.addCleanup(env.stop)

    def claude_root(self, version='0.37.0'):
        root = (self.home / '.claude' / 'plugins' / 'cache' / 'mkt' /
                'qmd-auto-context' / version)
        root.mkdir(parents=True)
        return root

    def installed(self, root, sha):
        record = self.home / '.claude' / 'plugins' / 'installed_plugins.json'
        record.write_text(json.dumps({'version': 2, 'plugins': {
            'qmd-auto-context@mkt': [{'installPath': str(root), 'gitCommitSha': sha}]}}))


class SourceRuleChecks(Base):
    def test_claude_marketplace_clone_is_used_only_at_installed_commit_and_clean(self):
        source = self.home / '.claude' / 'plugins' / 'marketplaces' / 'mkt'
        head = repo(source, {'a.txt': 'one\n'})
        root = self.claude_root()
        self.installed(root, head)
        self.assertEqual(auto.select_source(root, self.home), (source, head))
        (source / 'untracked.json').write_text('{}')  # host install receipts
        self.assertEqual(auto.select_source(root, self.home), (source, head))
        (source / 'a.txt').write_text('edited\n')
        with self.assertRaisesRegex(auto.UpgradeRefused, 'marketplace_source_dirty'):
            auto.select_source(root, self.home)
        git(source, 'checkout', '-q', '--', 'a.txt')
        self.installed(root, '0' * 40)
        with self.assertRaisesRegex(auto.UpgradeRefused,
                                    'marketplace_source_commit_mismatch'):
            auto.select_source(root, self.home)

    def test_codex_cache_commit_selects_codex_marketplace_clone(self):
        source = self.home / '.codex' / '.tmp' / 'marketplaces' / 'mkt'
        head = repo(source, {'a.txt': 'one\n'})
        root = (self.home / '.codex' / 'plugins' / 'cache' / 'mkt' /
                'qmd-auto-context' / '0.37.0')
        root.parent.mkdir(parents=True)
        git(source.parent, 'clone', '-q', str(source), str(root))
        # Codex keeps the installed copy as a clone: its HEAD is the record.
        self.assertEqual(auto.installed_commit(root, self.home), head)
        self.assertEqual(auto.select_source(root, self.home), (source, head))
        commit(source, {'b.txt': 'newer\n'})  # marketplace moved past the install
        with self.assertRaisesRegex(auto.UpgradeRefused,
                                    'marketplace_source_commit_mismatch'):
            auto.select_source(root, self.home)

    def test_unknown_layout_and_missing_clone_are_refused(self):
        with self.assertRaisesRegex(auto.UpgradeRefused, 'host_layout_unknown'):
            auto.select_source(self.base / 'checkout', self.home)
        with self.assertRaisesRegex(auto.UpgradeRefused, 'marketplace_source_unavailable'):
            auto.select_source(self.claude_root(), self.home)

    def test_older_host_bundle_never_downgrades_the_install(self):
        source = self.base / 'source'
        older = repo(source, {'a.txt': '1\n'})
        newer = commit(source, {'a.txt': '2\n'})
        auto._not_older(source, older, newer)
        auto._not_older(source, newer, newer)
        with self.assertRaisesRegex(auto.UpgradeRefused, 'older_host_bundle'):
            auto._not_older(source, newer, older)


class ProviderRuleChecks(Base):
    def setUp(self):
        super().setUp()
        self.old = {'providerManifest': manifest('/old'), 'vectorProfile': PROFILE,
                    'dependencyClosure': [{'path': '/n/x.node', 'sha256': 'a' * 64}]}

    def review(self, root='/new', **changes):
        return {'proposalSha256': '9' * 64, 'reviewedProvider': {
            'vectorProfile': changes.pop('profile', PROFILE),
            'dependencyClosure': changes.pop('closure', self.old['dependencyClosure']),
            'providerManifest': manifest(root, **changes)}}

    def test_only_bundle_file_paths_may_differ(self):
        self.assertTrue(auto.provider_equivalent(self.old, self.review(), '/new'))
        for changed in ({'modelSha256': '0' * 64}, {'nodeSha256': '0' * 64},
                        {'workerSha256': '0' * 64}, {'dimension': 1024},
                        {'serviceSocketPath': '/other.sock'},
                        {'workerPath': '/elsewhere/worker.mjs'},
                        {'profile': {**PROFILE, 'providerArtifactSha256': '0' * 64}},
                        {'closure': []}):
            with self.subTest(changed=changed):
                self.assertFalse(auto.provider_equivalent(
                    self.old, self.review(**dict(changed)), '/new'))


class WorkerChecks(Base):
    def setUp(self):
        super().setUp()
        self.root = self.claude_root()
        self.old_identity = {'pluginRoot': '/old', 'sourceCommit': '1' * 40,
                             'bundleManifestSha256': 'a' * 64, 'vectorProfile': PROFILE}
        self.attestation = {'providerManifest': manifest('/old'), 'vectorProfile': PROFILE,
                            'dependencyClosure': []}
        self.reviewed = {'proposalSha256': '9' * 64, 'reviewedProvider': {
            'vectorProfile': PROFILE, 'dependencyClosure': [],
            'providerManifest': manifest(str(self.root))}}

    def run_worker(self, kind='active', reviewed=None, installed=None):
        import laya_active_upgrade
        import laya_bound_upgrade
        import laya_inactive_upgrade
        import laya_product_install as product
        calls = {}

        def record(name, result):
            def call(**kwargs):
                calls[name] = kwargs
                return dict(result)
            return call
        with mock.patch.object(auto, 'installs', return_value=[self.state]), \
             mock.patch.object(auto, 'classify', return_value=(kind, self.old_identity)), \
             mock.patch.object(auto, 'installed_commit', return_value=installed), \
             mock.patch.object(auto, 'select_source',
                               return_value=(self.base / 'source', '2' * 40)), \
             mock.patch.object(auto, '_not_older'), \
             mock.patch.object(auto, '_read_attestation', return_value=self.attestation), \
             mock.patch.object(auto, '_mark_sealed_for_rescan') as rescan, \
             mock.patch.object(product, 'review_local_provider',
                               return_value=reviewed or self.reviewed), \
             mock.patch.object(product, '_request_build_wake',
                               return_value={'status': 'queued'}) as wake, \
             mock.patch.object(laya_active_upgrade, 'upgrade',
                               side_effect=record('active', {'status': 'active_upgrade_complete'})), \
             mock.patch.object(laya_bound_upgrade, 'upgrade',
                               side_effect=record('bound', {'status': 'bound_upgrade_complete'})), \
             mock.patch.object(laya_inactive_upgrade, 'upgrade',
                               side_effect=record('inactive', {'status': 'inactive_upgrade_complete'})):
            results = auto.run(self.project, root=self.root, home=self.home)
        return results, calls, rescan, wake

    def test_equivalent_provider_upgrades_selected_route_and_wakes_sync(self):
        results, calls, _rescan, wake = self.run_worker('active')
        self.assertEqual(results[0]['status'], 'active_upgrade_complete')
        self.assertEqual(calls['active']['accepted_provider_sha256'], '9' * 64)
        self.assertEqual(calls['active']['plugin_root'], str(self.root))
        self.assertEqual(calls['active']['model_path'], '/m/model.gguf')
        wake.assert_called_once_with(str(self.project))
        self.assertEqual(auto.read_status(self.state)['status'], 'complete')

    def test_bound_and_inactive_builds_use_their_own_paths(self):
        _results, calls, rescan, _wake = self.run_worker('bound')
        self.assertIn('bound', calls)
        rescan.assert_not_called()
        _results, calls, rescan, _wake = self.run_worker('inactive')
        self.assertIn('inactive', calls)
        rescan.assert_called_once_with(self.state)

    def test_changed_provider_refuses_and_leaves_a_notice(self):
        changed = {**self.reviewed, 'reviewedProvider': {
            **self.reviewed['reviewedProvider'],
            'providerManifest': manifest(str(self.root), modelSha256='0' * 64)}}
        results, calls, _rescan, wake = self.run_worker('active', reviewed=changed)
        self.assertEqual(results[0], {'stateRoot': str(self.state), 'status': 'refused',
                                      'reason': 'provider_changed_rebuild_required'})
        self.assertEqual(calls, {})
        wake.assert_not_called()
        status = auto.read_status(self.state)
        self.assertEqual((status['status'], status['reason']),
                         ('refused', 'provider_changed_rebuild_required'))
        spawned = []
        with mock.patch.object(auto, 'executing_root', return_value=self.root):
            state = auto.request(str(self.project), self.state / 'install-journal.sqlite',
                                 'executing_code_outside_bundle', spawn=spawned.append)
        self.assertEqual(spawned, [])  # a refusal is not retried every session
        line = auto.notice_line(state, 'executing_code_outside_bundle')
        self.assertIn('provider_changed_rebuild_required', line)
        self.assertIn('read-only', line)
        self.assertNotIn('\n', line)

    def test_same_commit_in_other_host_cache_is_sealed_as_companion(self):
        import laya_product_bundle as bundle
        owner = {'pluginRoot': '/old', 'sourceCommit': '1' * 40,
                 'bundleManifestSha256': 'a' * 64, 'vectorProfile': PROFILE}
        with mock.patch.object(bundle, 'inspect_full_plugin_candidate', return_value=owner), \
             mock.patch.object(bundle, 'seal_companion_host_copy') as seal:
            results, calls, _rescan, _wake = self.run_worker('active', installed='1' * 40)
        self.assertEqual(results[0]['status'], 'companion_sealed')
        seal.assert_called_once_with('/old', self.root)
        self.assertEqual(calls, {})


class RequestChecks(Base):
    def test_request_spawns_once_per_bundle_and_backs_off(self):
        root = self.claude_root()
        jpath = self.state / 'install-journal.sqlite'
        spawned = []
        with mock.patch.object(auto, 'executing_root', return_value=root):
            self.assertEqual(auto.request('/p', jpath, 'x', spawn=spawned.append)['status'],
                             'queued')
            auto._write_status(self.state, status='running', executingRoot=str(root),
                               attempts=1, reason='')
            self.assertEqual(auto.request('/p', jpath, 'x', spawn=spawned.append)['status'],
                             'running')
            auto._write_status(self.state, status='failed', executingRoot=str(root),
                               attempts=1, reason='initial_build_busy')
            now = auto.read_status(self.state)['updatedNs']
            self.assertEqual(auto.request('/p', jpath, 'x', spawn=spawned.append,
                                          now=now)['status'], 'failed')
            auto.request('/p', jpath, 'x', spawn=spawned.append,
                         now=now + (auto.RETRY_SECONDS + 1) * 10**9)
            auto._write_status(self.state, status='refused', executingRoot='/other',
                               attempts=1, reason='older_host_bundle')
            auto.request('/p', jpath, 'x', spawn=spawned.append)  # new bundle: retry
        self.assertEqual(spawned, ['/p', '/p', '/p'])
        with mock.patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '1'}), \
             mock.patch.object(auto, 'executing_root', return_value=self.base / 'x'), \
             mock.patch.object(auto, '_spawn') as real_spawn:
            self.assertEqual(auto.request('/p', jpath, 'x')['reason'], 'fixture_no_spawn')
        real_spawn.assert_not_called()


class ReadOnlyProofChecks(Base):
    def row(self, att_sha):
        selected = {'pluginRoot': '/old', 'installId': '4' * 32, 'generation': 'g-' + '9' * 32,
                    'sourceCommit': '1' * 40, 'bundleManifestSha256': 'a' * 64,
                    'ownershipManifestSha256': 'b' * 64, 'bundleKind': 'full_plugin',
                    'vectorProfile': PROFILE}
        return {'epoch': 7, 'selected': selected, 'provider_attestation_sha': att_sha,
                'provider_file_identity': [['/old/x', 1, 2, 3, 4, 5]]}

    def test_read_only_proof_uses_sealed_attestation_not_bundle_files(self):
        route = {'installId': '4' * 32, 'generation': 'g-' + '9' * 32,
                 'projectRoot': '/p', 'scopeId': '6' * 32, 'dbIncarnation': 'a' * 32}
        attestation = {'sourceCommit': '1' * 40, 'bundleManifestSha256': 'a' * 64,
                       'ownershipManifestSha256': 'b' * 64, 'vectorProfile': PROFILE,
                       'providerManifest': {'approvedProviderId': 'p'}}
        proof = {key: self.row('c' * 64)['selected'][key] for key in
                 ('sourceCommit', 'bundleManifestSha256', 'ownershipManifestSha256',
                  'vectorProfile')}
        with mock.patch.object(journal, '_attestation', return_value=(attestation, 'c' * 64)):
            result = journal._read_only_proof(Path('/s/install-journal.sqlite'),
                                              self.row('c' * 64), route, proof, True)
            self.assertEqual((result['readOnlyAllowed'], result['productRouteAllowed']),
                             (True, False))
            self.assertEqual(result['providerManifest'], {'approvedProviderId': 'p'})
            with self.assertRaisesRegex(journal.JournalEvidenceError,
                                        'production_attestation_changed'):
                journal._read_only_proof(Path('/s/install-journal.sqlite'),
                                         self.row('d' * 64), route, proof, True)
        with mock.patch.object(journal, '_attestation',
                               return_value=({**attestation, 'sourceCommit': '2' * 40},
                                             'c' * 64)):
            with self.assertRaisesRegex(journal.JournalEvidenceError,
                                        'production_provider_identity_changed'):
                journal._read_only_proof(Path('/s/install-journal.sqlite'),
                                         self.row('c' * 64), route, proof, True)

    def test_only_bundle_identity_failures_count_as_drift(self):
        from laya_product_bundle import BundleEvidenceError
        self.assertTrue(journal.is_bundle_drift(
            journal.JournalEvidenceError('executing_code_outside_bundle')))
        self.assertTrue(journal.is_bundle_drift(BundleEvidenceError('bundle_root_unavailable')))
        for code in ('journal_unavailable', 'index_incarnation_mismatch',
                     'production_attestation_changed', 'route_bundle_mismatch'):
            self.assertFalse(journal.is_bundle_drift(journal.JournalEvidenceError(code)))
        self.assertFalse(journal.is_bundle_drift(ValueError('executing_code_outside_bundle')))


if __name__ == '__main__':
    unittest.main()
