#!/usr/bin/env python3
"""Minimal-settings migration of a selected Laya route, with crash recovery.

A real-shaped selected install in a temp HOME: a v2 route record, the private
reviewed route, an install journal (sqlite) activated on that route, a sealed
production attestation, a fresh-wiki selection ledger and marker, and real
Laya index/claim-queue databases. Only bundle identity reads are patched (no
plugin cache is built) and the per-user provider lane is replaced so no file
outside the temp HOME is touched.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager, nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import config  # noqa: E402
import laya_active_upgrade as active  # noqa: E402
import laya_fresh_wiki_origin as fresh  # noqa: E402
import laya_host_response as response  # noqa: E402
import laya_inactive_upgrade as inactive  # noqa: E402
import laya_index_host_route as host_route  # noqa: E402
import laya_install_journal as journal_mod  # noqa: E402
import laya_settings_rebind as rebind  # noqa: E402
import laya_wiki_discovery as discovery  # noqa: E402
import resolve_paths  # noqa: E402
import settings_migration  # noqa: E402
from laya_index_a import LayaIndexA  # noqa: E402
from laya_index_claim_queue import ClaimQueue  # noqa: E402
from laya_product_bundle import (BundleEvidenceError,  # noqa: E402
                                 PRODUCTION_ATTESTATION_SCHEMA)


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def vector_profile():
    model, fingerprint, dimension = 'synthetic-model', 'a' * 64, 3
    identity = json.dumps([model, fingerprint, dimension, 'cosine'], sort_keys=True,
                          ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {'profileId': sha(identity), 'modelId': model,
            'embeddingFingerprint': fingerprint, 'dimension': dimension,
            'metric': 'cosine', 'approvedProviderId': 'synthetic-provider',
            'providerArtifactSha256': 'b' * 64,
            'providerRelativePath': 'core/synthetic_provider.py'}


def legacy_settings(raw_name, wiki_name, raw_path='.', name='project'):
    """The live legacy shape whose effective identity minimal settings derive."""
    return {
        'indexing': True, 'name': name,
        'collections': [raw_name, wiki_name],
        'collectionPaths': {raw_name: raw_path, wiki_name: '.auto-context/wiki'},
        'collectionRoles': {raw_name: 'raw', wiki_name: 'wiki'},
        'skipPaths': ['build'], 'recallStrategy': 'hierarchical',
        'compile': {'mode': 'auto-wiki', 'triggers': ['post_tool_source', 'manual'],
                    'extractor': {'builtins': ['claude']}},
    }


class SelectedProject(unittest.TestCase):
    RAW, WIKI = 'project', 'project-wiki'
    RAW_PATH, NAME = '.', 'project'
    ORIGIN = 'fresh'

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='minimal-rebind-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = self.home / 'project'
        self.auto = self.project / '.auto-context'
        (self.auto / 'wiki').mkdir(parents=True)
        (self.project / self.RAW_PATH).mkdir(parents=True, exist_ok=True)
        self.settings = self.auto / 'settings.json'
        self.legacy = (json.dumps(legacy_settings(self.RAW, self.WIKI, self.RAW_PATH,
                                                  self.NAME), indent=2,
                                  ensure_ascii=False) + '\n').encode()
        self.settings.write_bytes(self.legacy)
        self.state = self.home / 'state'
        self.state.mkdir(mode=0o700)
        self.plugin = self.home / 'installed-laya'
        self.plugin.mkdir()
        self.user_qmd = self.home / 'user-qmd'
        self.user_qmd.mkdir()
        self.journal = self.state / 'install-journal.sqlite'
        self.db, self.queue = self.state / 'index.sqlite', self.state / 'claims.sqlite'
        with closing(LayaIndexA(self.db, create=True)) as index:
            self.incarnation = index.incarnation()
        with closing(ClaimQueue.create(self.queue, index_incarnation=self.incarnation)):
            pass
        for name in ('catalog.sqlite', 'binding.sqlite'):
            (self.state / name).write_bytes(b'')
        self.proof = {'status': 'exact_commit_full_plugin_candidate_consistent',
                      'pluginRoot': str(self.plugin), 'sourceCommit': 'c' * 40,
                      'bundleManifestSha256': 'd' * 64,
                      'ownershipManifestSha256': 'e' * 64,
                      'vectorProfile': vector_profile()}
        self.route = {'schema': host_route.SCHEMA_V2, 'status': 'active',
                      'projectRoot': str(self.project),
                      'settingsSha256': host_route.settings_identity_digest(
                          self.project, self.collections()),
                      'dbPath': str(self.db), 'queuePath': str(self.queue),
                      'dbIncarnation': self.incarnation, 'scopeId': '6' * 32,
                      'collections': self.collections(),
                      'pluginRoot': str(self.plugin), 'journalPath': str(self.journal),
                      'installId': '3' * 32, 'generation': 'g-' + '4' * 32,
                      'bundleManifestSha256': 'd' * 64,
                      'ownershipManifestSha256': 'e' * 64,
                      'vectorProfile': vector_profile()}
        self.route_raw = canon(self.route)
        self.private_write(self.auto / host_route.RECORD, self.route_raw)
        self.private_write(self.state / 'reviewed-route.json', self.route_raw)
        attestation = {'schema': PRODUCTION_ATTESTATION_SCHEMA, 'sourceCommit': 'c' * 40,
                       'bundleManifestSha256': 'd' * 64,
                       'ownershipManifestSha256': 'e' * 64,
                       'journalPath': str(self.journal), 'vectorProfile': vector_profile(),
                       'providerManifest': {'providerPath': str(self.plugin / 'p.py')},
                       'dependencyClosure': []}
        att_raw = journal_mod._canon(attestation).encode('utf-8')
        self.private_write(self.state / journal_mod.PRODUCTION_ATTESTATION, att_raw)
        identity_rows = [[str(self.plugin / 'p.py'), 1, 2, 3, 4, 5]]
        with mock.patch.object(journal_mod, 'inspect_full_plugin_candidate',
                               return_value=self.proof):
            created = journal_mod.create_journal(self.journal, self.proof, self.route_raw,
                                                 user_qmd_roots=[str(self.user_qmd)])
        with mock.patch.object(journal_mod, '_inspect_identity', return_value=self.proof), \
                mock.patch.object(journal_mod, '_bind_attestation',
                                  return_value=(sha(att_raw), identity_rows)):
            selected = journal_mod.cas_activate(self.journal, created['epoch'],
                                                self.route_raw)
        self.assertEqual(selected['state'], 'active')
        identity = lambda path: [path.stat().st_dev, path.stat().st_ino]
        self.ledger = {
            'schema': fresh.SCHEMA, 'epoch': 3, 'phase': 'active',
            'projectRoot': str(self.project), 'journalPath': str(self.journal),
            'routeSha256': sha(self.route_raw), 'collections': self.collections(),
            'candidateDb': str(self.db), 'queueDb': str(self.queue),
            'catalogDb': str(self.state / 'catalog.sqlite'),
            'bindingStore': str(self.state / 'binding.sqlite'),
            'scope': '6' * 32, 'indexGeneration': 'g-' + '7' * 32 + '-b' + '8' * 64,
            'sourceManifest': '9' * 64, 'bindingDigest': '8' * 64,
            'catalogScanId': 'a' * 32, 'catalogRawDigest': 'b' * 64,
            'vectorProfileId': vector_profile()['profileId'], 'vectorRows': 1,
            'candidateIncarnation': self.incarnation,
            'attestationSha256': sha(att_raw),
            'providerFileIdentitySha256': selected['providerFileIdentitySha256'],
            'candidateIdentity': identity(self.db), 'queueIdentity': identity(self.queue),
            'catalogIdentity': identity(self.state / 'catalog.sqlite'),
            'bindingStoreIdentity': identity(self.state / 'binding.sqlite')}
        self.ledger_module, self.ledger_name = fresh, fresh.LEDGER
        self.ledger_module._write(self.state / self.ledger_name, self.ledger, replace=False)
        self.ledger_module._write(self.auto / self.ledger_module.MARKER,
                                  self.ledger_module._marker(self.ledger), replace=False)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(rebind, '_provider_lane', nullcontext))

    def load_ledger(self):
        return rebind._Ledger(self.ORIGIN).load(self.state / self.ledger_name)

    def collections(self):
        raw_root = (self.project / self.RAW_PATH).resolve()
        return [{'name': self.RAW, 'role': 'raw', 'sourceRoot': str(raw_root)},
                {'name': self.WIKI, 'role': 'wiki',
                 'sourceRoot': str(self.auto / 'wiki')}]

    @staticmethod
    def private_write(path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    def decision(self):
        return host_route.select_route(str(self.project), action='recall')

    def assert_selected_and_readable(self):
        decision = self.decision()
        self.assertEqual(decision['state'], 'candidate', decision)
        proof = journal_mod.verify_selected_route_read_only(decision)
        self.assertTrue(proof['readOnlyAllowed'])
        return decision, proof

    def serve(self, action, decision, payload):
        calls = []

        def fake_recall(host, served, payload, proof, recheck, *, degraded=False):
            self.assertEqual(recheck(served), proof)
            calls.append({'state': served['state'], 'degraded': degraded,
                          'readOnly': proof.get('readOnlyAllowed')})
            return {'served': True}
        with mock.patch.object(response, '_serve_recall', fake_recall):
            out = response.serve_selected(action, 'claude', decision, payload)
        return out, calls

    def crash_after(self, step_name):
        """Run the migration and fail right after one rebind step."""
        proposal = settings_migration.plan(str(self.project))
        self.assertEqual(proposal['status'], 'review_required', proposal)
        steps = rebind._file_steps

        def interrupted(intent, target):
            built = steps(intent, target)
            original = built[step_name]

            def then_crash():
                original()
                raise RuntimeError('synthetic_crash')
            return {**built, step_name: then_crash}
        with mock.patch.object(rebind, '_file_steps', interrupted):
            failed = settings_migration.apply(str(self.project),
                                              proposal['proposalSha256'])
        self.assertEqual(failed['reason'], 'settings_rebind_step_failed', failed)
        return proposal


class IdentityChangeChecks(SelectedProject):
    RAW, WIKI = 'notes', 'notes-wiki'

    def test_selected_route_with_other_collection_names_is_refused(self):
        before = self.settings.read_bytes()
        self.assert_selected_and_readable()
        result = settings_migration.plan(str(self.project))
        self.assertEqual(result['reason'], 'bound_collection_identity_changed', result)
        self.assertEqual({row['collection'] for row in result['identityDiff']},
                         {'notes', 'notes-wiki', 'project', 'project-wiki'})
        applied = settings_migration.apply(str(self.project), None)
        self.assertEqual(applied['reason'], 'bound_collection_identity_changed')
        self.assertEqual(self.settings.read_bytes(), before)
        self.assert_selected_and_readable()


class MinimalQmdIndexChecks(unittest.TestCase):
    def setUp(self):
        # Temp roots live under /private, which resolve_paths treats as risky.
        patcher = mock.patch.object(resolve_paths, 'is_risky_path', lambda _path: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_minimal_raw_root_is_never_a_qmd_index_entry(self):
        with tempfile.TemporaryDirectory(prefix='minimal-qmd-') as temp:
            root = Path(temp).resolve()
            (root / '.auto-context' / 'wiki').mkdir(parents=True)
            minimal = config.normalize_config({'name': 'project', 'skipPaths': []})
            resolved = resolve_paths.resolve_paths(str(root), json.dumps(minimal))
            self.assertEqual([row['name'] for row in resolved['indexEntries']],
                             ['project-wiki'])
            self.assertEqual([row['name'] for row in resolved['sourceEntries']], ['project'])
            self.assertEqual(len(resolved['entries']), 2)
            legacy = config.normalize_config(legacy_settings('project', 'project-wiki'))
            resolved = resolve_paths.resolve_paths(str(root), json.dumps(legacy))
            self.assertEqual([row['name'] for row in resolved['indexEntries']],
                             ['project', 'project-wiki'])
            (root / '.auto-context' / 'settings.json').write_text(
                json.dumps({'name': 'project', 'skipPaths': []}))
            with mock.patch.dict(os.environ, {'HOME': str(root.parent)}):
                raw = config.load_project_config_raw(str(root))
            self.assertEqual(raw['settingsModel'], 'minimal')
            self.assertEqual(raw['collectionRoles'], {'project': 'raw', 'project-wiki': 'wiki'})
            resolved = resolve_paths.resolve_paths(str(root), json.dumps(raw))
            self.assertEqual([row['name'] for row in resolved['indexEntries']],
                             ['project-wiki'])

    def test_shared_registry_drops_index_flag_only_for_minimal_raw(self):
        import qmd_shared_store as store
        with tempfile.TemporaryDirectory(prefix='minimal-registry-') as temp:
            base = Path(temp).resolve() / 'store'
            base.mkdir(mode=0o700)
            row = {'scope': '/p', 'path': '/p', 'indexed': True}
            registry = base / 'collections.json'
            registry.write_text(json.dumps({'project': row}))
            registry.chmod(0o600)
            demoted = {'project': {**row, 'indexed': False}}
            # An explicit role edit (no minimal settings) still needs reconciliation.
            with self.assertRaisesRegex(ValueError, 'path_change_requires_migration'):
                store._reserve_owners(base, demoted)
            minimal = config.normalize_config({'name': 'project', 'skipPaths': []})
            self.assertEqual(store.minimal_raw_names(minimal), {'project'})
            self.assertEqual(store.minimal_raw_names(
                config.normalize_config(legacy_settings('project', 'project-wiki'))), set())
            store._reserve_owners(base, demoted, demoted=store.minimal_raw_names(minimal))
            self.assertIs(json.loads(registry.read_text())['project']['indexed'], False)
            moved = {'project': {**row, 'path': '/q', 'indexed': False}}
            with self.assertRaisesRegex(ValueError, 'path_change_requires_migration'):
                store._reserve_owners(base, moved, demoted={'project'})


if __name__ == '__main__':
    unittest.main()
