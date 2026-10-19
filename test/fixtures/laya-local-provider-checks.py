#!/usr/bin/env python3
"""Local provider boundary with tiny inert artifacts; no model is loaded."""
import hashlib
import json
import os
import socket
import threading
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'core'))

from laya_index_a import IndexContractError
from laya_local_embed_provider import (DEFAULT_EMBED_MODEL_URI,
                                       ReviewedLocalQmdProvider, recipe_fingerprint)
from laya_local_embed_service import start_owned_embed_service


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ProviderChecks(unittest.TestCase):
    def setUp(self):
        # macOS AF_UNIX has a short path limit; use a canonical short root.
        self.tmp = tempfile.TemporaryDirectory(prefix='lp-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(mode=0o700)
        self.sock = self.runtime / 'laya-embed.sock'
        core = self.root / 'core'
        core.mkdir()
        self.worker = core / 'laya_local_embed_worker.mjs'
        shutil.copyfile(ROOT / 'core/laya_local_embed_worker.mjs', self.worker)
        self.provider_file = core / 'laya_local_embed_provider.py'
        shutil.copyfile(ROOT / 'core/laya_local_embed_provider.py', self.provider_file)
        shutil.copyfile(ROOT / 'core/laya_qmd_chunking.mjs', core / 'laya_qmd_chunking.mjs')
        self.node = self.root / 'synthetic-node'
        self.node.write_text('#!/bin/sh\nprintf \'{"vector":[1.0,0.0]}\'\n', encoding='utf-8')
        self.node.chmod(0o700)
        self.module = self.root / 'synthetic-module.mjs'
        self.module.write_text('export function getLlama() { throw Error("not run") }\n')
        self.model = self.root / 'synthetic.gguf'
        self.model.write_bytes(b'GGUF' + b'fixture-only')
        runtime_id = 'qmd-fork-f4633f9-node-llama-cpp-3.20.0'
        fp = recipe_fingerprint(model_id=DEFAULT_EMBED_MODEL_URI, model_sha256=sha(self.model),
                                runtime_id=runtime_id,
                                recipe_id='qmd-query-format-single-document-v1',
                                provider_sha256=sha(self.provider_file),
                                worker_sha256=sha(self.worker), node_sha256=sha(self.node),
                                module_entry_sha256=sha(self.module), closure_sha256='d'*64)
        self.manifest = {
            'approvedProviderId': 'synthetic-provider',
            'providerArtifactSha256': sha(self.provider_file),
            'providerRelativePath': 'core/laya_local_embed_provider.py',
            'providerPath': str(self.provider_file),
            'workerPath': str(self.worker), 'workerSha256': sha(self.worker),
            'nodePath': str(self.node),
            'serviceSocketPath': str(self.sock),
            'nodeSha256': sha(self.node), 'moduleEntryPath': str(self.module),
            'moduleEntrySha256': sha(self.module), 'modelPath': str(self.model),
            'modelSha256': sha(self.model), 'runtimeId': runtime_id,
            'modelId': DEFAULT_EMBED_MODEL_URI, 'embeddingFingerprint': fp,
            'dimension': 2, 'recipeId': 'qmd-query-format-single-document-v1',
            'dependencyClosureApproved': True, 'dependencyClosureSha256': 'd'*64,
        }

    @contextmanager
    def fake_service(self, responses, manifest=None):
        identity = manifest or self.manifest
        seen = []
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.sock))
        self.sock.chmod(0o600)
        listener.listen(4)
        def serve():
            for result in responses:
                conn, _ = listener.accept()
                with conn:
                    data = bytearray()
                    while True:
                        part = conn.recv(65536)
                        if not part:
                            break
                        data.extend(part)
                    seen.append(json.loads(data.decode('utf-8')))
                    envelope = dict(approvedProviderId=identity['approvedProviderId'],
                        providerArtifactSha256=identity['providerArtifactSha256'],
                        embeddingFingerprint=identity['embeddingFingerprint'],
                        ok=True, result=result)
                    if isinstance(result, dict) and '__envelope__' in result:
                        # A raw failure reply: replace ok/result, add fields.
                        envelope = {**envelope, **result['__envelope__']}
                    conn.sendall(json.dumps(envelope).encode('utf-8'))
            listener.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield seen
        finally:
            thread.join(timeout=2)
            if self.sock.exists():
                self.sock.unlink()

    def test_manifest_is_inert_and_exact_profile(self):
        provider = ReviewedLocalQmdProvider(self.manifest)
        self.assertEqual(provider.profile['embedding_fingerprint'],
                         self.manifest['embeddingFingerprint'])
        self.assertEqual(provider.profile['dimension'], 2)
        self.assertEqual(provider.profile['metric'], 'cosine')

    def test_closure_and_recipe_are_mandatory(self):
        for change in ({'dependencyClosureApproved': False},
                       {'embeddingFingerprint': '0' * 64},
                       {'modelId': 'hf:unapproved/qwen-embed.gguf'},
                       {'providerRelativePath': '../provider.py'}):
            manifest = {**self.manifest, **change}
            with self.assertRaises(IndexContractError):
                ReviewedLocalQmdProvider(manifest)

    def test_each_executed_artifact_digest_changes_profile(self):
        original = ReviewedLocalQmdProvider(self.manifest).profile
        digest_keys = ('providerArtifactSha256', 'workerSha256', 'nodeSha256',
                       'moduleEntrySha256', 'dependencyClosureSha256')
        for key in digest_keys:
            changed = {**self.manifest, key: 'f'*64}
            changed['embeddingFingerprint'] = recipe_fingerprint(
                model_id=changed['modelId'], model_sha256=changed['modelSha256'],
                runtime_id=changed['runtimeId'], recipe_id=changed['recipeId'],
                provider_sha256=changed['providerArtifactSha256'],
                worker_sha256=changed['workerSha256'],
                node_sha256=changed['nodeSha256'],
                module_entry_sha256=changed['moduleEntrySha256'],
                closure_sha256=changed['dependencyClosureSha256'])
            candidate = ReviewedLocalQmdProvider(changed).profile
            self.assertNotEqual(candidate['embedding_fingerprint'],
                                original['embedding_fingerprint'])
            self.assertNotEqual(candidate['profile_id'], original['profile_id'])

    def test_synthetic_stub_is_explicit_and_no_model_runs(self):
        provider = ReviewedLocalQmdProvider(self.manifest)
        public_profile = dict(profileId=provider.profile['profile_id'],
                              modelId=provider.profile['model_id'],
                              embeddingFingerprint=provider.profile['embedding_fingerprint'],
                              dimension=2, metric='cosine')
        with self.fake_service([{'vector': [1.0, 0.0]}, {'vector': [1.0, 0.0]}]) as seen:
            result = provider('otter', public_profile)
            self.assertEqual(result['vector'], [1.0, 0.0])
            self.assertEqual(result['approvedProviderId'], 'synthetic-provider')
            self.assertEqual(provider.embed_document('small doc'), [1.0, 0.0])
        self.assertEqual([item['kind'] for item in seen], ['query', 'document'])
        with self.assertRaisesRegex(IndexContractError, 'vector_profile_mismatch'):
            provider('otter', {**public_profile, 'dimension': 3})

    def test_artifact_or_gguf_mismatch_fails_before_stub(self):
        provider = ReviewedLocalQmdProvider(self.manifest)
        public_profile = dict(profileId=provider.profile['profile_id'],
                              modelId=provider.profile['model_id'],
                              embeddingFingerprint=provider.profile['embedding_fingerprint'],
                              dimension=2, metric='cosine')
        self.model.write_bytes(b'bad model')
        with self.assertRaisesRegex(IndexContractError, 'provider_artifact_hash_mismatch'):
            provider._verify_artifacts()
        self.manifest['modelSha256'] = sha(self.model)
        self.manifest['embeddingFingerprint'] = recipe_fingerprint(
            model_id=DEFAULT_EMBED_MODEL_URI, model_sha256=self.manifest['modelSha256'],
            runtime_id=self.manifest['runtimeId'], recipe_id=self.manifest['recipeId'],
            provider_sha256=self.manifest['providerArtifactSha256'],
            worker_sha256=self.manifest['workerSha256'],
            node_sha256=self.manifest['nodeSha256'],
            module_entry_sha256=self.manifest['moduleEntrySha256'],
            closure_sha256=self.manifest['dependencyClosureSha256'])
        provider = ReviewedLocalQmdProvider(self.manifest)
        public_profile = dict(profileId=provider.profile['profile_id'],
                              modelId=provider.profile['model_id'],
                              embeddingFingerprint=provider.profile['embedding_fingerprint'],
                              dimension=2, metric='cosine')
        with self.assertRaisesRegex(IndexContractError, 'provider_model_not_gguf'):
            provider._verify_artifacts()

    def test_no_path_or_env_fallback(self):
        manifest = {**self.manifest, 'nodePath': 'node'}
        provider = ReviewedLocalQmdProvider(manifest)
        with self.assertRaisesRegex(IndexContractError, 'provider_artifact_path_invalid'):
            provider._verify_artifacts()
        os.environ['QMD_EMBED_MODEL'] = 'hf:unapproved/model'
        self.addCleanup(os.environ.pop, 'QMD_EMBED_MODEL', None)
        provider = ReviewedLocalQmdProvider(self.manifest)
        with self.assertRaisesRegex(IndexContractError, 'provider_unavailable'):
            provider('otter', dict(profileId=provider.profile['profile_id'],
                modelId=provider.profile['model_id'],
                embeddingFingerprint=provider.profile['embedding_fingerprint'],
                dimension=2, metric='cosine'))

    def test_exact_utf8_bytes_and_surrogate_rejection(self):
        provider = ReviewedLocalQmdProvider(self.manifest)
        with self.fake_service([{'vector': [1, 0]}]) as seen:
            self.assertEqual(provider.embed_document('é 한글 😀', title='Ţ'), [1, 0])
            self.assertEqual((seen[0]['text'], seen[0]['title']), ('é 한글 😀', 'Ţ'))
            for text, title in [('bad\ud800', None), ('okay', '\udfff')]:
                with self.assertRaisesRegex(IndexContractError, 'provider_text_invalid'):
                    provider.embed_document(text, title=title)
            self.assertEqual(len(seen), 1)

    def test_chunk_recipe_is_distinct_and_result_is_strict(self):
        manifest = {**self.manifest, 'recipeId': 'qmd-regex-token-chunks-candidate-v1'}
        manifest['embeddingFingerprint'] = recipe_fingerprint(
            model_id=manifest['modelId'], model_sha256=manifest['modelSha256'],
            runtime_id=manifest['runtimeId'], recipe_id=manifest['recipeId'],
            provider_sha256=manifest['providerArtifactSha256'],
            worker_sha256=manifest['workerSha256'],
            node_sha256=manifest['nodeSha256'],
            module_entry_sha256=manifest['moduleEntrySha256'],
            closure_sha256=manifest['dependencyClosureSha256'])
        provider = ReviewedLocalQmdProvider(manifest)
        self.assertNotEqual(provider.profile['embedding_fingerprint'],
                            self.manifest['embeddingFingerprint'])
        rows = [{'seq': 0, 'pos': 0, 'vector': [1, 0]},
                {'seq': 1, 'pos': 765, 'vector': [0, 1]}]
        with self.fake_service([{'chunks': rows}, {'chunks': [
                {'seq': 0, 'pos': 0, 'vector': [1, 0]},
                {'seq': 1, 'pos': 0, 'vector': [0, 1]}]}], manifest=manifest) as seen:
            self.assertEqual(provider.embed_document_chunks('hello', path='docs/T.md'), rows)
            with self.assertRaisesRegex(IndexContractError, 'provider_result_invalid'):
                provider.embed_document_chunks('hello', path='docs/T.md')
        self.assertEqual([item['path'] for item in seen], ['docs/T.md'] * 2)
        with self.assertRaisesRegex(IndexContractError, 'provider_recipe_invalid'):
            ReviewedLocalQmdProvider(self.manifest).embed_document_chunks('hello', path='docs/T.md')

    def test_member_refusal_is_named_only_for_document_chunks(self):
        manifest = {**self.manifest, 'recipeId': 'qmd-regex-token-chunks-candidate-v1'}
        manifest['embeddingFingerprint'] = recipe_fingerprint(
            model_id=manifest['modelId'], model_sha256=manifest['modelSha256'],
            runtime_id=manifest['runtimeId'], recipe_id=manifest['recipeId'],
            provider_sha256=manifest['providerArtifactSha256'],
            worker_sha256=manifest['workerSha256'],
            node_sha256=manifest['nodeSha256'],
            module_entry_sha256=manifest['moduleEntrySha256'],
            closure_sha256=manifest['dependencyClosureSha256'])
        provider = ReviewedLocalQmdProvider(manifest)
        refused = {'__envelope__': {'ok': False, 'result': None,
                                    'refused': 'provider_chunks_invalid'}}
        unknown = {'__envelope__': {'ok': False, 'result': None, 'refused': 'busy'}}
        busy = {'__envelope__': {'ok': False, 'result': None}}
        with self.fake_service([refused, unknown, busy], manifest=manifest):
            with self.assertRaisesRegex(IndexContractError, '^provider_member_refused$'):
                provider.embed_document_chunks('hello', path='docs/T.md')
            for _ in range(2):
                with self.assertRaisesRegex(IndexContractError, '^provider_result_invalid$'):
                    provider.embed_document_chunks('hello', path='docs/T.md')
        profile = dict(profileId=provider.profile['profile_id'],
                       modelId=provider.profile['model_id'],
                       embeddingFingerprint=provider.profile['embedding_fingerprint'],
                       dimension=2, metric='cosine')
        refused_query = {'__envelope__': {'ok': False, 'result': None,
                                          'refused': 'provider_context_exceeded'}}
        with self.fake_service([refused_query], manifest=manifest):
            # A hook query never gets the member code: it is not a document.
            with self.assertRaisesRegex(IndexContractError, '^provider_result_invalid$'):
                provider('otter', profile)

    def test_query_fails_closed_without_warm_worker(self):
        provider = ReviewedLocalQmdProvider(self.manifest)
        profile = dict(profileId=provider.profile['profile_id'],
                       modelId=provider.profile['model_id'],
                       embeddingFingerprint=provider.profile['embedding_fingerprint'],
                       dimension=2, metric='cosine')
        with self.assertRaisesRegex(IndexContractError, 'provider_unavailable'):
            provider('otter', profile)
        with self.fake_service([{'vector': [1, 0]}], manifest={
                **self.manifest, 'approvedProviderId': 'wrong-owner'}):
            with self.assertRaisesRegex(IndexContractError, 'provider_result_invalid'):
                provider('otter', profile)

    def test_explicit_owner_start_and_admission_with_synthetic_module(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node unavailable')
        stub = self.root / 'warm-module.mjs'
        stub.write_text('''
export async function getLlama() {
  return {async loadModel() { return {
    trainContextSize: 2048,
    tokenize: () => new Uint32Array(3),
    async createEmbeddingContext() { return {
      async getEmbeddingFor() { return {vector: [1, 0]}; },
      async dispose() {} }; },
    async dispose() {}
  }; }, async dispose() {} };
}
''', encoding='utf-8')
        manifest = dict(self.manifest, nodePath=os.path.realpath(node),
                        nodeSha256=sha(Path(os.path.realpath(node))),
                        moduleEntryPath=str(stub), moduleEntrySha256=sha(stub))
        manifest['embeddingFingerprint'] = recipe_fingerprint(
            model_id=manifest['modelId'], model_sha256=manifest['modelSha256'],
            runtime_id=manifest['runtimeId'], recipe_id=manifest['recipeId'],
            provider_sha256=manifest['providerArtifactSha256'],
            worker_sha256=manifest['workerSha256'],
            node_sha256=manifest['nodeSha256'],
            module_entry_sha256=manifest['moduleEntrySha256'],
            closure_sha256=manifest['dependencyClosureSha256'])
        with self.assertRaisesRegex(IndexContractError, 'provider_admission_deferred'):
            start_owned_embed_service(manifest, admit=lambda: False)
        provider = ReviewedLocalQmdProvider(manifest)
        class Lease:
            released = False
            def release(self):
                self.released = True
        lease = Lease()
        service = start_owned_embed_service(manifest, admit=lambda: lease)
        try:
            with self.assertRaisesRegex(IndexContractError, 'provider_already_running'):
                start_owned_embed_service(manifest, admit=lambda: True)
            self.assertEqual(provider.embed_document('small'), [1, 0])
            # Simulate owner death: the pipe closes without a stop RPC.
            service.process.stdin.close()
            self.assertEqual(service.process.wait(timeout=5), 0)
        finally:
            service.stop()
            service.stop()
        self.assertFalse(self.sock.exists())
        self.assertTrue(lease.released)
        self.assertIsNotNone(service.process.poll())

    def test_real_worker_names_a_content_refusal(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node unavailable')
        stub = self.root / 'refusing-module.mjs'
        # One token per character. A first heading longer than the context
        # is prefixed to every chunk, so no chunking can make it fit.
        stub.write_text('''
export async function getLlama() {
  return {async loadModel() { return {
    trainContextSize: 2048,
    tokenize: text => new Uint32Array(String(text).length),
    async createEmbeddingContext() { return {
      async getEmbeddingFor() { return {vector: [1, 0]}; },
      async dispose() {} }; },
    async dispose() {}
  }; }, async dispose() {} };
}
''', encoding='utf-8')
        manifest = dict(self.manifest, nodePath=os.path.realpath(node),
                        nodeSha256=sha(Path(os.path.realpath(node))),
                        moduleEntryPath=str(stub), moduleEntrySha256=sha(stub),
                        recipeId='qmd-regex-token-chunks-candidate-v1')
        manifest['embeddingFingerprint'] = recipe_fingerprint(
            model_id=manifest['modelId'], model_sha256=manifest['modelSha256'],
            runtime_id=manifest['runtimeId'], recipe_id=manifest['recipeId'],
            provider_sha256=manifest['providerArtifactSha256'],
            worker_sha256=manifest['workerSha256'],
            node_sha256=manifest['nodeSha256'],
            module_entry_sha256=manifest['moduleEntrySha256'],
            closure_sha256=manifest['dependencyClosureSha256'])
        provider = ReviewedLocalQmdProvider(manifest)
        service = start_owned_embed_service(manifest, admit=lambda: True)
        try:
            self.assertEqual(len(provider.embed_document_chunks('small text', path='docs/a.md')), 1)
            with self.assertRaisesRegex(IndexContractError, '^provider_member_refused$'):
                provider.embed_document_chunks('# ' + 'T' * 3000 + '\nbody\n',
                                               path='docs/b.md')
            # The worker is still serving after a refusal.
            self.assertEqual(len(provider.embed_document_chunks('small again', path='docs/c.md')), 1)
        finally:
            service.stop()

    def test_worker_stays_local_and_preserves_default_format(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node unavailable')
        stub = self.root / 'fake-llama.mjs'
        stub.write_text('''
export async function getLlama(opts) {
  const seen = {opts, embedded: []};
  return {
    async loadModel(modelOpts) { seen.modelOpts = modelOpts; return {
      trainContextSize: 2048,
      tokenize: () => new Uint32Array(3),
      async createEmbeddingContext() { return {
        async getEmbeddingFor(s) { seen.embedded.push(s); return {vector:[1,0]}; },
        async dispose() {} }; },
      async dispose() { process.stderr.write(JSON.stringify(seen)); }
    }; },
    async dispose() {}
  };
}
''', encoding='utf-8')
        for kind, text, title, expected in [
            ('query', 'otter', None, 'task: search result | query: otter'),
            ('document', 'body', 'T', 'title: T | text: body')]:
            request = {'kind': kind, 'text': text, 'title': title,
                       'modelPath': str(self.model), 'moduleEntryPath': str(stub),
                       'dimension': 2, 'runtimeId': self.manifest['runtimeId']}
            result = subprocess.run([node, str(self.worker)],
                input=json.dumps(request).encode('utf-8'), capture_output=True,
                timeout=15, env={}, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'vector': [1, 0]})
            seen = json.loads(result.stderr)
            self.assertEqual(seen['opts']['build'], 'never')
            self.assertTrue(seen['opts']['skipDownload'])
            self.assertEqual(seen['opts']['gpu'], 'auto')
            self.assertEqual(seen['opts']['maxThreads'], 1)
            self.assertEqual(seen['modelOpts']['gpuLayers'], 0)
            self.assertEqual(seen['embedded'], [expected])
        request = {'kind': 'document_chunks', 'text': '# T\n' + 'z' * 3000,
                   'title': None, 'path': 'docs/T.md', 'modelPath': str(self.model),
                   'moduleEntryPath': str(stub), 'dimension': 2,
                   'runtimeId': self.manifest['runtimeId']}
        result = subprocess.run([node, str(self.worker)],
            input=json.dumps(request).encode('utf8'), capture_output=True,
            timeout=15, env={}, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        chunks = json.loads(result.stdout)['chunks']
        self.assertGreater(len(chunks), 1)
        self.assertEqual([row['seq'] for row in chunks], list(range(len(chunks))))
        self.assertTrue(all(row['vector'] == [1, 0] for row in chunks))
        seen = json.loads(result.stderr)
        self.assertTrue(seen['opts']['skipDownload'])
        self.assertEqual(seen['opts']['gpu'], 'auto')
        self.assertEqual(seen['opts']['maxThreads'], 1)
        self.assertEqual(seen['modelOpts']['gpuLayers'], 0)


if __name__ == '__main__':
    unittest.main()
