import tempfile
import unittest
from pathlib import Path

from memvane.provider.adapters import reattest


class ReattestationTest(unittest.TestCase):
    def setUp(self):
        self.original = {'provider': 'a' * 64, 'worker': 'b' * 64}
        self.current = {'provider': 'a' * 64, 'worker': 'c' * 64}
        self.identity = {'modelSha256': 'd' * 64, 'tokenizer': 'embedded',
                         'chunkerSha256': 'e' * 64, 'chunkRules': '900/135/200',
                         'dimension': 768, 'metric': 'cosine', 'pooling': 'model-default',
                         'normalization': 'none', 'maxTokens': 2048}

    def test_exact_pair_and_identity(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root).resolve()
            record = reattest.write(state, self.original, self.current, self.identity, self.identity)
            self.assertTrue(reattest.valid(state, self.original, self.current, self.identity))
            self.assertEqual(record['outcome'], 'accepted')
            self.assertFalse(reattest.valid(state, self.original, {**self.current, 'worker': 'f'*64}, self.identity))
            self.assertEqual((state / 'provider-reattest.json').stat().st_mode & 0o777, 0o600)
            newer = {**self.current, 'worker': 'f' * 64}
            reattest.write(state, self.original, newer, self.identity, self.identity)
            self.assertTrue(reattest.valid(state, self.original, newer, self.identity))
            self.assertFalse(reattest.valid(state, self.original, self.current, self.identity))

    def test_different_or_absent_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root).resolve()
            for changed in ({**self.identity, 'modelSha256': 'f'*64},
                            {**self.identity, 'pooling': 'mean'},
                            {**self.identity, 'normalization': 'l2'}, None):
                with self.assertRaises(ValueError):
                    reattest.write(state, self.original, self.current, self.identity, changed)
                self.assertFalse((state / 'provider-reattest.json').exists())

    def test_unsafe_file_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root).resolve()
            reattest.write(state, self.original, self.current, self.identity, self.identity)
            (state / 'provider-reattest.json').chmod(0o644)
            self.assertFalse(reattest.valid(state, self.original, self.current, self.identity))

    def test_known_legacy_pair_and_new_identity(self):
        from unittest.mock import patch
        manifest = {'workerSha256': reattest.LEGACY_WORKER,
                    'providerArtifactSha256': reattest.LEGACY_PROVIDER,
                    'modelSha256': 'd'*64, 'dimension': 768}
        old = {**self.identity, 'tokenizer': 'model-embedded',
               'chunkerSha256': reattest.LEGACY_CHUNKER}
        with tempfile.TemporaryDirectory() as root, patch.object(reattest, '_current', return_value=(self.current, old)):
            state = Path(root).resolve()
            self.assertFalse(reattest.check(state, manifest))
            reattest.update(state, manifest)
            self.assertTrue(reattest.check(state, manifest))
            for key, value in (('pooling', 'mean'), ('normalization', 'l2'), ('maxTokens', 1024),
                               ('chunkerSha256', 'f'*64)):
                with patch.object(reattest, '_current', return_value=(self.current, {**old, key: value})):
                    self.assertFalse(reattest.check(state, manifest))
                    with self.assertRaises(ValueError):
                        reattest.update(state, manifest)
            with patch.object(reattest, '_current', side_effect=ValueError('reattest_manifest_missing')):
                with self.assertRaises(ValueError):
                    reattest.check(state, manifest)

    def test_unknown_attested_bytes_cannot_be_bootstrapped(self):
        from unittest.mock import patch
        manifest = {'workerSha256': self.original['worker'],
                    'providerArtifactSha256': self.original['provider'],
                    'modelSha256': 'd'*64, 'dimension': 768}
        with tempfile.TemporaryDirectory() as root, patch.object(reattest, '_current', return_value=(self.current, self.identity)):
            with self.assertRaises(ValueError):
                reattest.update(Path(root), manifest)

    def test_journal_accepts_only_proved_pair_without_changing_vectors_or_attestation(self):
        import hashlib
        import json
        import sqlite3
        from unittest.mock import patch
        from memvane.journal import api as journal
        from memvane.provider.adapters.local_embed_provider import recipe_fingerprint, DEFAULT_EMBED_MODEL_URI
        from memvane.provider.domain.profile import profile_identity
        from memvane.provider import api as provider_api
        from memvane.kernel import memvane_paths
        with tempfile.TemporaryDirectory() as root:
            state = Path(root).resolve()
            journal_path = state / 'journal.sqlite'
            store = state / 'vectors.sqlite'
            db = sqlite3.connect(store)
            db.execute('create table vectors (vector text)')
            db.executemany('insert into vectors values (?)', [('one',), ('two',)])
            db.commit()
            manifest = {'approvedProviderId':'legacy', 'providerArtifactSha256':reattest.LEGACY_PROVIDER,
                'providerRelativePath':'memvane/provider/adapters/local_embed_provider.py',
                'workerSha256':reattest.LEGACY_WORKER, 'serviceSocketPath':str(state/'service.sock'),
                'nodePath':str(state/'node'), 'nodeSha256':'1'*64,
                'moduleEntryPath':str(state/'module'), 'moduleEntrySha256':'2'*64,
                'modelPath':str(state/'model'), 'modelSha256':'3'*64,
                'runtimeId':'qmd-fork-f4633f9-node-llama-cpp-3.20.0', 'modelId':DEFAULT_EMBED_MODEL_URI,
                'dimension':768, 'recipeId':'qmd-regex-token-chunks-candidate-v1',
                'dependencyClosureApproved':True, 'dependencyClosureSha256':hashlib.sha256(b'[]').hexdigest()}
            manifest['embeddingFingerprint'] = recipe_fingerprint(model_id=manifest['modelId'],
                model_sha256=manifest['modelSha256'], runtime_id=manifest['runtimeId'],
                recipe_id=manifest['recipeId'], provider_sha256=manifest['providerArtifactSha256'],
                worker_sha256=manifest['workerSha256'], node_sha256=manifest['nodeSha256'],
                module_entry_sha256=manifest['moduleEntrySha256'], closure_sha256=manifest['dependencyClosureSha256'])
            profile = {'profileId':profile_identity(manifest['modelId'],manifest['embeddingFingerprint'],768),
                       'modelId':manifest['modelId'], 'embeddingFingerprint':manifest['embeddingFingerprint'],
                       'dimension':768, 'metric':'cosine', 'approvedProviderId':'legacy',
                       'providerArtifactSha256':manifest['providerArtifactSha256'],
                       'providerRelativePath':manifest['providerRelativePath']}
            record = {'schema':'memvane-provider-attestation-v1', 'journalPath':str(journal_path),
                      'vectorProfile':profile, 'providerManifest':manifest, 'dependencyClosure':[],
                      'runtimeFilesSha256':'4'*64}
            original = json.dumps(record, sort_keys=True).encode()
            attestation = state/'provider-attestation.json'; attestation.write_bytes(original)
            active = {key:manifest[key] for key in ('runtimeId','nodeSha256','moduleEntrySha256','modelSha256')}
            active[provider_api.FILES_SHA] = '4'*64
            identity = {**self.identity, 'modelSha256':'3'*64, 'tokenizer':'model-embedded',
                        'chunkerSha256':reattest.LEGACY_CHUNKER}
            current = {'provider':reattest.LEGACY_PROVIDER, 'worker':'c'*64}
            files = {manifest['nodePath']:'1'*64, manifest['moduleEntryPath']:'2'*64,
                     manifest['modelPath']:'3'*64}
            with patch.object(provider_api, 'fixture_port', return_value=None), \
                 patch.object(provider_api, 'hash_file', side_effect=lambda p: current['worker'] if p.endswith('.mjs') else current['provider']), \
                 patch.object(provider_api, 'runtime_file_sha256', side_effect=lambda p: files[p]), \
                 patch.object(provider_api, 'select', return_value=active), \
                 patch.object(provider_api, 'verify_attested_files'), \
                 patch.object(memvane_paths, 'install_socket', return_value=Path(manifest['serviceSocketPath'])), \
                 patch.object(reattest, '_current', return_value=(current, identity)):
                self.assertFalse(journal.provider_manifest(record, profile, allow_local_unavailable=True)[1])
                reattest.update(state, manifest)
                result, available = journal.provider_manifest(record, profile, allow_local_unavailable=True)
                self.assertTrue(available)
                self.assertEqual(result['embeddingFingerprint'], profile['embeddingFingerprint'])
                self.assertEqual(attestation.read_bytes(), original)
                self.assertEqual(db.execute('select count(*) from vectors').fetchone()[0],2)
                with patch.object(reattest, '_current', return_value=({**current,'worker':'f'*64}, identity)):
                    self.assertFalse(journal.provider_manifest(record, profile, allow_local_unavailable=True)[1])
            db.close()
