import tempfile
import unittest
from pathlib import Path

from memvane.provider.adapters import reattest


class ReattestationTest(unittest.TestCase):
    def test_exact_artifacts_do_not_require_a_new_identity_manifest(self):
        import hashlib
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder).resolve()
            worker=state/'worker.mjs';worker.write_text('legacy worker')
            provider=state/'provider.py';provider.write_text('legacy provider')
            sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
            manifest={'workerPath':str(worker),'workerSha256':sha(worker),
                      'providerPath':str(provider),'providerArtifactSha256':sha(provider)}
            with patch.object(reattest,'_current',side_effect=ValueError('reattest_manifest_missing')):
                self.assertEqual(reattest.authorize(state,manifest),{'worker':sha(worker),'provider':sha(provider)})
    def setUp(self):
        self.original = {'provider': 'a' * 64, 'worker': 'b' * 64}
        self.current = {'provider': 'a' * 64, 'worker': 'c' * 64}
        self.identity = {'modelSha256': 'd' * 64, 'tokenizer': 'embedded',
                         'chunkerSha256': 'e' * 64, 'chunkRules': '900/135/200',
                         'dimension': 768, 'metric': 'cosine', 'pooling': 'model-default',
                         'normalization': 'none', 'maxTokens': 2048, **reattest.LEGACY_TEMPLATES}

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
            self.assertTrue(reattest.valid(state, self.original, self.current, self.identity))

    def test_pair_rotation_and_legacy_current(self):
        import json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            state = Path(root).resolve()
            other = {**self.current, 'worker':'f'*64}
            with patch.object(reattest.time, 'time_ns', side_effect=range(1, 30)):
                for current in (self.current, other, self.current):
                    reattest.write(state, self.original, current, self.identity, self.identity)
                    self.assertTrue(reattest.valid(state, self.original, current, self.identity))
                    self.assertTrue(reattest.valid(state, self.original, current, self.identity, require_legacy=True))
                self.assertTrue(reattest.valid(state, self.original, other, self.identity))
                legacy = json.loads((state/'provider-reattest.json').read_text())
                self.assertEqual(legacy['current'], self.current)
                self.assertEqual(set(legacy['attestedIdentity']), reattest._LEGACY_FIELDS)
                for value in range(17):
                    current = {**self.current, 'worker':format(value, '064x')}
                    reattest.write(state, self.original, current, self.identity, self.identity)
            record = json.loads((state/'provider-reattest-pairs.json').read_text())
            self.assertEqual(len(record['records']), 16)
            self.assertEqual((state/'provider-reattest-pairs.json').stat().st_mode & 0o777, 0o600)
            self.assertFalse(reattest.valid(state, self.original, self.current, self.identity))

    def test_corrupt_pairs_fail_closed_even_with_valid_v1(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root).resolve()
            reattest.write(state, self.original, self.current, self.identity, self.identity)
            path = state/'provider-reattest-pairs.json'
            path.write_text('{"schema":"wrong","records":[]}'); path.chmod(0o600)
            self.assertFalse(reattest.valid(state, self.original, self.current, self.identity))
            with self.assertRaises(ValueError):
                reattest.write(state, self.original, self.current, self.identity, self.identity)

    def test_existing_origin_cannot_be_rebound_to_a_different_identity(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root).resolve()
            reattest.write(state, self.original, self.current, self.identity, self.identity)
            changed = {**self.identity, 'pooling':'mean'}
            with self.assertRaisesRegex(ValueError, 'reattest_identity_changed'):
                reattest.write(state, self.original, {**self.current, 'worker':'f'*64}, changed, changed)
            self.assertTrue(reattest.valid(state, self.original, self.current, self.identity))

    def test_legacy_only_and_unsafe_pairs(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root).resolve()
            original = reattest.LEGACY_PAIR
            reattest.write(state, original, self.current, self.identity, self.identity)
            path = state/'provider-reattest-pairs.json'
            path.unlink()
            self.assertTrue(reattest.valid(state, original, self.current, self.identity))
            newer = {**self.current, 'worker':'f'*64}
            reattest.write(state, original, newer, self.identity, self.identity)
            self.assertTrue(reattest.valid(state, original, self.current, self.identity))
            self.assertTrue(reattest.valid(state, original, newer, self.identity))
            path.unlink()
            path.symlink_to(state/'provider-reattest.json')
            self.assertFalse(reattest.valid(state, original, self.current, self.identity))
            with self.assertRaises(OSError):
                reattest.write(state, original, self.current, self.identity, self.identity)

    def test_legacy_ledger_read_failure_reaches_the_caller(self):
        import errno
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            state = Path(root).resolve()
            original = reattest.LEGACY_PAIR
            reattest.write(state, original, self.current, self.identity, self.identity)
            (state / reattest.PAIRS_FILE).unlink()
            with patch.object(reattest, '_read', side_effect=OSError(errno.EIO, 'fixture read')), \
                 self.assertRaises(OSError) as failure:
                reattest.valid(state, original, self.current, self.identity)
            self.assertEqual(failure.exception.errno, errno.EIO)
            self.assertTrue(reattest.valid(state, original, self.current, self.identity))

    def modern_pair(self):
        from memvane.kernel.bundle_paths import bundle_file, PROVIDER_RELATIVE_PATH, WORKER_RELATIVE_PATH
        from memvane.provider.api import hash_file
        provider = str(bundle_file(PROVIDER_RELATIVE_PATH))
        return {'providerPath': provider, 'providerArtifactSha256': hash_file(provider),
                'workerPath': str(bundle_file(WORKER_RELATIVE_PATH)), 'workerSha256': '1' * 64,
                'modelSha256': self.identity['modelSha256'], 'dimension': 768}

    def test_modern_pair_rejects_missing_corrupt_and_unsafe_proof(self):
        manifest = self.modern_pair()
        cases = [('unregistered', None), ('corrupt', b'{corrupt'), ('invalid', b'{}'),
                 ('invalid_utf8', b'\xff'), ('nested', b'[' * 1500 + b']' * 1500),
                 ('permissive', b'{}'), ('symlink', None)]
        for label, content in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder:
                state = Path(folder).resolve()
                path = state / reattest.PAIRS_FILE
                if content is not None:
                    path.write_bytes(content)
                    path.chmod(0o644 if label == 'permissive' else 0o600)
                if label == 'symlink':
                    target = state / 'other.json'
                    target.write_text('{}')
                    path.symlink_to(target)
                self.assertFalse(reattest.check(state, manifest))
                with self.assertRaisesRegex(ValueError, '^reattest_not_authorized$'):
                    reattest.authorize(state, manifest)

    def test_socket_and_fifo_are_unsafe_ledgers(self):
        import os
        import socket
        manifest = self.modern_pair()
        for name in (reattest.PAIRS_FILE, reattest.FILE):
            for kind in ('socket', 'fifo'):
                with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory(dir='/tmp') as folder:
                    state = Path(folder).resolve()
                    path = state / name
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
                        if kind == 'socket':
                            endpoint.bind(str(path))
                        else:
                            os.mkfifo(path, 0o600)
                        if name == reattest.FILE:
                            self.assertFalse(reattest.valid(state, reattest.LEGACY_PAIR, self.current, self.identity))
                        else:
                            self.assertFalse(reattest.check(state, manifest))
                            with self.assertRaisesRegex(ValueError, '^reattest_not_authorized$'):
                                reattest.authorize(state, manifest)

    def test_state_metadata_io_remains_retryable(self):
        import errno
        from unittest.mock import patch
        manifest = self.modern_pair()
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            original = Path.stat
            for code in (errno.EACCES, errno.EIO):
                def state_stat(path, *args, **kwargs):
                    if path == state:
                        raise OSError(code, 'state metadata unavailable')
                    return original(path, *args, **kwargs)
                with patch.object(Path, 'stat', state_stat), self.assertRaises(OSError) as failure:
                    reattest.check(state, manifest)
                self.assertEqual(failure.exception.errno, code)

    def test_modern_pair_ledger_io_reaches_both_callers_then_recovers(self):
        import errno
        import os
        from unittest.mock import patch
        manifest = self.modern_pair()
        original = {'worker': manifest['workerSha256'], 'provider': manifest['providerArtifactSha256']}
        current, identity = reattest._current(manifest)
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            reattest.write(state, original, current, identity, identity)
            opening = os.open
            for code in (errno.EIO, errno.EACCES, errno.ENXIO, errno.EOPNOTSUPP):
                def fail_ledger(path, *args, **kwargs):
                    if path == state / reattest.PAIRS_FILE:
                        raise OSError(code, 'fixture ledger I/O')
                    return opening(path, *args, **kwargs)
                with patch.object(os, 'open', side_effect=fail_ledger):
                    for read in (reattest.check, reattest.authorize):
                        with self.subTest(errno=code, caller=read.__name__), self.assertRaises(OSError) as failure:
                            read(state, manifest)
                        self.assertEqual(failure.exception.errno, code)
            self.assertEqual(reattest.authorize(state, manifest), current)
            self.assertTrue(reattest.check(state, manifest))

    def test_ledger_changed_while_reading_is_retryable(self):
        import errno
        import json
        import os
        from unittest.mock import patch
        manifest = self.modern_pair()
        original = {'worker': manifest['workerSha256'], 'provider': manifest['providerArtifactSha256']}
        current, identity = reattest._current(manifest)
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            reattest.write(state, original, current, identity, identity)
            path = state / reattest.PAIRS_FILE
            read = json.load
            def change_during_read(stream, **kwargs):
                row = read(stream, **kwargs)
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
                return row
            with patch.object(json, 'load', side_effect=change_during_read), self.assertRaises(OSError) as failure:
                reattest.check(state, manifest)
            self.assertEqual(failure.exception.errno, errno.EAGAIN)
            self.assertTrue(reattest.check(state, manifest))

    def test_two_host_versions_alternate_authorization(self):
        import json
        from unittest.mock import patch
        original = reattest.LEGACY_PAIR
        identity = {**self.identity, 'tokenizer':'model-embedded', 'chunkerSha256':reattest.LEGACY_CHUNKER}
        host_a = {'worker':'1'*64, 'provider':'2'*64}
        host_b = {'worker':'3'*64, 'provider':'2'*64}
        manifest = {'workerSha256':original['worker'], 'providerArtifactSha256':original['provider'],
                    'workerPath':'/fixture/worker.mjs', 'providerPath':'/fixture/provider.py',
                    'modelSha256':identity['modelSha256'], 'dimension':768}
        with tempfile.TemporaryDirectory() as root:
            state = Path(root).resolve()
            for host in (host_a, host_b, host_a):
                with patch.object(reattest, '_current', return_value=(host, identity)), \
                     patch('memvane.provider.api.hash_file', side_effect=lambda path: host['worker'] if path.endswith('.mjs') else host['provider']):
                    reattest.update(state, manifest)
                    self.assertTrue(reattest.check(state, manifest))
                    self.assertEqual(json.loads((state/'provider-reattest.json').read_text())['current'], host)
            for host in (host_a, host_b):
                with patch.object(reattest, '_current', return_value=(host, identity)), \
                     patch('memvane.provider.api.hash_file', side_effect=lambda path: host['worker'] if path.endswith('.mjs') else host['provider']):
                    self.assertTrue(reattest.check(state, manifest))

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
            (state / 'provider-reattest-pairs.json').unlink()
            self.assertFalse(reattest.valid(state, self.original, self.current, self.identity))

    def test_known_legacy_pair_and_new_identity(self):
        from unittest.mock import patch
        manifest = {'workerSha256': reattest.LEGACY_WORKER,
                    'providerArtifactSha256': reattest.LEGACY_PROVIDER,
                    'workerPath':'/synthetic/worker.mjs', 'providerPath':'/synthetic/provider.py',
                    'modelSha256': 'd'*64, 'dimension': 768}
        old = {**self.identity, 'tokenizer': 'model-embedded',
               'chunkerSha256': reattest.LEGACY_CHUNKER}
        with tempfile.TemporaryDirectory() as root, patch.object(reattest, '_current', return_value=(self.current, old)), \
             patch('memvane.provider.api.hash_file', side_effect=lambda p: self.current['worker'] if p.endswith('.mjs') else self.current['provider']):
            state = Path(root).resolve()
            self.assertFalse(reattest.check(state, manifest))
            reattest.update(state, manifest)
            self.assertTrue(reattest.check(state, manifest))
            for key, value in (('pooling', 'mean'), ('normalization', 'l2'), ('maxTokens', 1024),
                               ('chunkerSha256', 'f'*64), ('queryTemplate', 'changed'),
                               ('documentTemplate', 'changed'), ('contextMargin', 5)):
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
                    'workerPath':'/synthetic/worker.mjs', 'providerPath':'/synthetic/provider.py',
                    'modelSha256': 'd'*64, 'dimension': 768}
        with tempfile.TemporaryDirectory() as root, patch.object(reattest, '_current', return_value=(self.current, self.identity)):
            with self.assertRaises(ValueError):
                reattest.update(Path(root), manifest)

    def test_worker_source_templates_match_manifest_and_template_change_is_detected(self):
        from unittest.mock import patch
        from memvane.kernel.bundle_paths import bundle_file, WORKER_RELATIVE_PATH
        source = bundle_file(WORKER_RELATIVE_PATH).read_text(encoding='utf8')
        manifest = {'modelSha256':'d'*64, 'dimension':768}
        with tempfile.TemporaryDirectory() as root:
            worker = Path(root)/'worker.mjs'; worker.write_text(source)
            def bundle(path):
                return worker if path == WORKER_RELATIVE_PATH else Path(root)/'provider.py'
            with patch('memvane.kernel.bundle_paths.bundle_file', side_effect=bundle), \
                 patch('memvane.provider.api.hash_file', return_value='e'*64):
                _, identity = reattest._current(manifest)
                self.assertEqual({key:identity[key] for key in reattest.LEGACY_TEMPLATES}, reattest.LEGACY_TEMPLATES)
                worker.write_text(source.replace('task: search result | query:', 'task: altered | query:'))
                _, changed = reattest._current(manifest)
                self.assertNotEqual(identity['queryTemplate'], changed['queryTemplate'])

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
