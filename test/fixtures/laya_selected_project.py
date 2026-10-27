#!/usr/bin/env python3
"""A real-shaped selected Laya install in a temp HOME, shared by fixtures.

Minimal ``{name, skipPaths}`` settings, a v2 route record bound to the
settings identity, the private reviewed route, an install journal (sqlite)
activated on that route, a sealed production attestation, a fresh-wiki
selection ledger and marker, and real Laya index/claim-queue databases. Only
bundle identity reads are patched (no plugin cache is built).
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import laya_fresh_wiki_origin as fresh  # noqa: E402
import laya_host_response as response  # noqa: E402
import laya_index_host_route as host_route  # noqa: E402
import laya_install_journal as journal_mod  # noqa: E402
import laya_wiki_discovery as discovery  # noqa: E402
from laya_index_a import LayaIndexA  # noqa: E402
from laya_index_claim_queue import ClaimQueue  # noqa: E402
from laya_product_bundle import PRODUCTION_ATTESTATION_SCHEMA  # noqa: E402


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


class SelectedProject(unittest.TestCase):
    RAW, WIKI = 'project', 'project-wiki'
    RAW_PATH, NAME = '.', 'project'

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='laya-selected-')
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
        self.settings.write_text(json.dumps({'name': self.NAME, 'skipPaths': ['build']},
                                            indent=2) + '\n')
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
