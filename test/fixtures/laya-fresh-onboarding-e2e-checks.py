#!/usr/bin/env python3
"""New-project onboarding end to end: minimal settings to a selected recall.

A temporary HOME holds a project with only ``{name, skipPaths}`` settings and
wiki cards. The real installer path runs in order: ``plan_fresh_wiki_origin``,
``prepare_fresh_wiki_origin`` (stores, reviewed route, attestation and a real
sqlite install journal), the bounded initial build, ``finalize_initial_build``
twice (freshness, then the fresh-wiki bind), ``activate_origin_selection``,
the route selector, the read-only route proof and the selected recall serve.

Only what needs a host install or a model is replaced: the provider review,
the host-loaded bundle proof and its attestation checks, the per-file
provider identity, the build provider (one fixed vector per card) and the
query provider. Nothing outside the temporary HOME is written.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
BUILD = runpy.run_path(str(HERE / 'laya-initial-build-checks.py'))

import config  # noqa: E402
import laya_fresh_wiki_origin as fresh  # noqa: E402
import laya_host_response as response  # noqa: E402
import laya_index_host_route as host_route  # noqa: E402
import laya_initial_build as initial_build  # noqa: E402
import laya_install_journal as journal_mod  # noqa: E402
import laya_product_install as product  # noqa: E402
import laya_product_sync_worker as worker  # noqa: E402

PROFILE_ID = BUILD['PROFILE_ID']
PROFILE = {'profileId': PROFILE_ID, 'modelId': BUILD['PROFILE']['model_id'],
           'embeddingFingerprint': BUILD['PROFILE']['embedding_fingerprint'],
           'dimension': BUILD['PROFILE']['dimension'], 'metric': 'cosine',
           'approvedProviderId': 'synthetic-provider',
           'providerArtifactSha256': 'b' * 64,
           'providerRelativePath': 'core/laya_local_embed_provider.py'}
COMMIT = 'c' * 40


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class QueryProvider:
    """The warm query owner: one fixed vector in the build's single dimension."""

    def __init__(self, manifest):
        self.manifest = manifest

    def __call__(self, prompt, profile):
        return {'profileId': profile['profileId'],
                'approvedProviderId': self.manifest['approvedProviderId'],
                'providerArtifactSha256': self.manifest['providerArtifactSha256'],
                'vector': [1.0, 0.0]}


class FreshOnboarding(BUILD['BuildCase']):
    def setUp(self):
        # The provider socket path of a real install is at most 100 bytes:
        # a short HOME directly under /tmp keeps it within that bound.
        tmp = tempfile.TemporaryDirectory(prefix='lo-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'QMD_SANDBOX': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.project = self.home / 'project'
        self.project.mkdir()
        cursor = self.home
        for name in ('.local', 'state', 'laya', BUILD['INSTALL']):
            cursor = cursor / name
            cursor.mkdir(mode=0o700)
        self.state = cursor
        BUILD['FakeProvider'].calls = []
        BUILD['FakeProvider'].budgets = []
        self.coordinator = BUILD['Coordinator']()
        self.service_log = []
        for name, value in (('ReviewedLocalQmdProvider', BUILD['FakeProvider']),
                            ('resource_report', lambda *_a, **_k: {'synthetic': True}),
                            ('_higher_priority_wake', lambda _controller: False)):
            patcher = mock.patch.object(worker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.auto = self.project / '.auto-context'
        self.wiki = self.auto / 'wiki'
        (self.wiki / 'concepts').mkdir(parents=True)
        self.settings = self.auto / 'settings.json'
        self.settings.write_text(json.dumps({'name': 'onboard', 'skipPaths': []}) + '\n')
        note = self.project / 'notes.md'
        note.write_text('The heron and the lantern walk.\n', encoding='utf-8')
        raw_name = config.minimal_collection_names('onboard')[0]
        revision = ('  - {kind: "file", path: "notes.md", collection: "%s", '
                    'sha256: "%s", size: %d, mtimeNs: %d}' % (
                        raw_name, _sha(note.read_bytes()), note.stat().st_size,
                        note.stat().st_mtime_ns))
        for name, title, body in (
                ('heron', 'Emerald heron', 'The emerald heron nests by the north pond.'),
                ('lantern', 'Paper lantern', 'Paper lanterns line the river walk.')):
            (self.wiki / 'concepts' / (name + '.md')).write_text(
                '---\ntitle: "%s"\nstatus: verified\ncreatedBy: qmd-auto-context\n'
                'sourceRevisions:\n%s\n---\n# %s\n%s\n' % (title, revision, title, body),
                encoding='utf-8')
        self.plugin = self.home / 'plugin-cache' / 'qmd-auto-context' / '0.0.0'
        self.plugin.mkdir(parents=True)
        self.source = self.home / 'marketplace'
        self.source.mkdir()
        self.user_qmd = self.home / 'user-qmd'
        self.user_qmd.mkdir()
        self.journal_path = self.state / 'install-journal.sqlite'
        self.proof = {'status': 'exact_commit_full_plugin_candidate_consistent',
                      'pluginRoot': str(self.plugin), 'sourceCommit': COMMIT,
                      'bundleManifestSha256': 'd' * 64,
                      'ownershipManifestSha256': 'e' * 64, 'vectorProfile': PROFILE}
        manifest = {'approvedProviderId': 'synthetic-provider',
                    'providerArtifactSha256': 'b' * 64,
                    'providerRelativePath': 'core/laya_local_embed_provider.py',
                    'providerPath': str(self.plugin / 'core/laya_local_embed_provider.py'),
                    'workerPath': str(self.plugin / 'core/laya_local_embed_worker.mjs'),
                    'serviceSocketPath': str(self.state / 'runtime' / 'laya-embed.sock'),
                    'modelPath': '/models/m.gguf', 'modelSha256': 'f' * 64,
                    'dimension': 2}
        self.review = {'status': 'review_required', 'proposalSha256': '9' * 64,
                       'reviewedProvider': {'vectorProfile': PROFILE,
                                            'providerManifest': manifest,
                                            'dependencyClosure': []}}
        stack = ExitStack()
        self.addCleanup(stack.close)
        enter = stack.enter_context
        proof = lambda *_a, **_k: dict(self.proof)
        # Bundle identity and attestation reads of a host install.
        for target, name, value in (
                (product, 'review_local_provider', lambda **_kw: self.review),
                (product, 'adopt_host_loaded_plugin', proof),
                (product, 'validate_production_provider_attestation', lambda *_a: None),
                (product, '_request_build_wake',
                 lambda _project: {'status': 'wake_skipped_in_fixture'}),
                (journal_mod, 'inspect_full_plugin_candidate', proof),
                (journal_mod, '_inspect_identity', proof),
                (journal_mod, 'validate_production_provider_attestation',
                 lambda *_a: None),
                (journal_mod, 'production_provider_approved', lambda *_a, **_k: True),
                (journal_mod, '_provider_file_identity',
                 lambda _att: [[str(self.plugin / 'core/p.py'), 1, 2, 3, 4, 5]]),
                # The build worker proves the real journal, not this host's bundle.
                (worker, 'inspect_journal', journal_mod.inspect_journal),
                (worker, 'preflight_product_candidate',
                 lambda path, **_kw: journal_mod.preflight_product_candidate(path))):
            enter(mock.patch.object(target, name, value))

    def finalize(self):
        return product.finalize_initial_build(project_root=self.project,
                                              state_root=self.state)

    def test_minimal_settings_project_is_built_selected_and_recalled(self):
        # 1. Plan from the minimal settings file alone.
        plan = product.plan_fresh_wiki_origin(self.project)
        self.assertEqual((plan['status'], plan['mode']), ('review_required', 'fresh_wiki_only'))
        raw, wiki = config.minimal_collection_names('onboard')
        self.assertEqual(plan['collections'], [
            {'name': raw, 'role': 'raw', 'sourceRoot': str(self.project)},
            {'name': wiki, 'role': 'wiki', 'sourceRoot': str(self.wiki)}])
        self.assertNotIn('sourceDb', plan)
        settings_before = self.settings.read_bytes()

        # 2. Prepare: stores, reviewed route, attestation, journal, build record.
        prepared = product.prepare_fresh_wiki_origin(
            project_root=self.project, source_root=str(self.source),
            plugin_root=str(self.plugin), state_root=str(self.state), commit_id=COMMIT,
            expected_proposal_sha256=plan['proposalSha256'],
            install_id=BUILD['INSTALL'],
            accepted_provider_sha256=self.review['proposalSha256'],
            model_path='/models/m.gguf',
            dimension=2)
        self.assertEqual(prepared['status'], 'initial_build_registered', prepared)
        route_bytes = (self.state / 'reviewed-route.json').read_bytes()
        route = json.loads(route_bytes)
        self.assertEqual(route['settingsSha256'],
                         host_route.settings_identity_digest(self.project,
                                                             plan['collections']))
        self.assertEqual(journal_mod.inspect_journal(self.journal_path)['state'],
                         'candidate_ready')
        self.assertEqual(self.record()['identity']['originMode'], 'fresh_wiki_only')
        self.assertFalse((self.auto / fresh.ROUTE).exists())
        self.assertFalse((self.auto / fresh.MARKER).exists())

        # 3. Bounded background build, then the two-call finalize.
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        self.assertEqual(self.finalize()['status'], 'freshness_pending')
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        bound = self.finalize()
        self.assertEqual(bound['status'], 'cutover_prepared', bound)
        self.assertEqual(self.record()['phase'], 'bound')
        self.assertEqual(host_route.select_route(str(self.project))['state'], 'legacy')

        # 4. Explicit activation publishes the reviewed route.
        with product._install_lock(self.state):
            active = product.activate_origin_selection(self.project, self.journal_path,
                                                       route_bytes)
        self.assertEqual(active['phase'], 'active', active)
        self.assertEqual((self.auto / fresh.ROUTE).read_bytes(), route_bytes)
        self.assertEqual(journal_mod.inspect_journal(self.journal_path)['state'], 'active')

        # 5. The selector binds the route; the read-only proof serves recall.
        decision = host_route.select_route(str(self.project), action='recall')
        self.assertEqual(decision['state'], 'candidate', decision)
        self.assertEqual(decision['projectRoot'], str(self.project))
        proof = journal_mod.verify_selected_route_read_only(decision)
        self.assertTrue(proof['readOnlyAllowed'])
        with mock.patch('laya_local_embed_provider.ReviewedLocalQmdProvider',
                        QueryProvider), \
                mock.patch('laya_embed_resident.admit_query', lambda **_kw: None):
            served = response._serve_recall(
                'claude', decision, {'prompt': 'emerald heron'}, proof,
                journal_mod.verify_selected_route_read_only)
        context = served['hookSpecificOutput']['additionalContext']
        self.assertIn('concepts/heron.md', context)
        self.assertIn('emerald heron nests', context)

        # 6. A skipPaths edit keeps the identity-bound route selected.
        self.settings.write_text(json.dumps({'name': 'onboard',
                                             'skipPaths': ['drafts']}) + '\n')
        self.assertEqual(host_route.select_route(str(self.project))['state'], 'candidate')
        self.settings.write_bytes(settings_before)

        # 7. Renaming the project changes the identity and blocks the route.
        self.settings.write_text(json.dumps({'name': 'renamed', 'skipPaths': []}) + '\n')
        renamed = host_route.select_route(str(self.project))
        self.assertEqual((renamed['state'], renamed['reason']),
                         ('blocked', 'route_settings_changed'))
        self.settings.write_bytes(settings_before)

    def test_qmd_origin_marker_is_refused_with_a_clear_reason(self):
        from laya_ledger_common import _write
        _write(self.auto / fresh.MARKER, {'schema': 'laya-qmd-selection-marker-v1',
                                          'projectRoot': str(self.project)},
               replace=False)
        decision = host_route.select_route(str(self.project))
        self.assertEqual((decision['state'], decision['reason']),
                         ('blocked', 'qmd_origin_detach_required'))
        (self.state / 'qmd-cutover.json').write_text('{}')
        with self.assertRaisesRegex(Exception, '^qmd_origin_detach_required$'):
            product.restore_origin_selection(project_root=self.project,
                                             state_root=self.state)


if __name__ == '__main__':
    unittest.main()
