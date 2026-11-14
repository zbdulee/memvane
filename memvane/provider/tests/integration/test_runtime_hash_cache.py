"""Proof F8: repeated proofs re-read a runtime file only when its identity moves."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from memvane.kernel import persistent_identity
from memvane.provider import api as provider
from memvane.provider import embed_runtime as R
from memvane.provider.adapters import local_embed_provider


def _generation(base):
    """A sealed generation with node, module entry, one native file and a model."""
    contents = {R.NODE: (b'#!node\n', True),
                'node_modules/node-llama-cpp/dist/index.js': (b'export {};\n', False),
                'node_modules/@node-llama-cpp/mac-arm64-metal/bins/llama.node': (b'native\n', False),
                R.MODEL: (b'gguf-bytes\n' * 64, False)}
    files = sorted([name, hashlib.sha256(raw).hexdigest(), len(raw), executable]
                   for name, (raw, executable) in contents.items())
    manifest = {'schema': R.SCHEMA, 'runtimeId': R.RUNTIME_ID, 'node': R.NODE,
                'moduleEntry': 'node_modules/node-llama-cpp/dist/index.js',
                'nativePackage': 'node_modules/@node-llama-cpp/mac-arm64-metal',
                'model': R.MODEL, 'files': files}
    raw = R._canonical(manifest)
    generations = base / 'embed-runtime' / 'generations'
    generation = generations / ('g-' + hashlib.sha256(raw).hexdigest()[:32])
    for name, (data, executable) in contents.items():
        path = generation / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o700 if executable else 0o600)
    (generation / R.MANIFEST).write_bytes(raw)
    (generation / R.MANIFEST).chmod(0o600)
    for directory in (base / 'embed-runtime', generations, *[p for p in generation.rglob('*') if p.is_dir()],
                      generation):
        directory.chmod(0o700)
    return generation, manifest


class RuntimeHashCache(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvp-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.base.chmod(0o700)
        persistent_identity._DIGESTS.clear()
        self.addCleanup(persistent_identity._DIGESTS.clear)
        self.generation, manifest = _generation(self.base)
        self.files = len(manifest['files'])
        self.attested = {'nodePath': str(self.generation / R.NODE),
                         'moduleEntryPath': str(self.generation / manifest['moduleEntry']),
                         'modelPath': str(self.generation / R.MODEL)}
        self.digest = R.files_sha256(manifest)

    def counted(self):
        reads = []
        real = R._hash

        def count(path, *args, **kwargs):
            reads.append(os.path.basename(path))
            return real(path, *args, **kwargs)
        return reads, mock.patch.object(R, '_hash', count)

    def test_second_proof_reads_no_unchanged_file(self):
        reads, patch = self.counted()
        with patch:
            self.assertEqual(R.verify_attested_files(self.attested, self.digest), self.digest)
            self.assertEqual(len(reads), self.files)
            for _ in range(3):
                self.assertEqual(R.verify_attested_files(self.attested, self.digest), self.digest)
            self.assertEqual(len(reads), self.files, 'unchanged files are not read again')
            # Same size, new bytes and mtime: re-read once, and the attested digest refuses.
            model = self.generation / R.MODEL
            info = model.stat()
            model.write_bytes(b'GGUF-BYTES\n' * 64)
            os.utime(model, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
            with self.assertRaises(R.RuntimeUnavailable):
                R.verify_attested_files(self.attested, self.digest)
            self.assertEqual(len(reads), self.files + 1)

    def test_provider_hasher_is_cached_by_file_identity(self):
        node = self.attested['nodePath']
        reads = []
        real = local_embed_provider._hash_file

        def count(path):
            reads.append(path)
            return real(path)
        with mock.patch.object(local_embed_provider, '_hash_file', count):
            first = provider.runtime_file_sha256(node)
            self.assertEqual([provider.runtime_file_sha256(node) for _ in range(3)], [first] * 3)
            self.assertEqual(len(reads), 1)
            info = os.stat(node)
            Path(node).write_bytes(b'#!NODE\n')
            os.utime(node, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
            self.assertNotEqual(provider.runtime_file_sha256(node), first)
            self.assertEqual(len(reads), 2)


if __name__ == '__main__':
    unittest.main()
