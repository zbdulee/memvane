"""Proof F8: repeated proofs re-read a runtime file only when its identity moves.

Verify F8: the digests persist in the runtime root's owner-only cache file,
so a second process reads no unchanged file.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from memvane.kernel import persistent_identity
from memvane.provider import api as provider
from memvane.provider import embed_runtime as R
from memvane.provider import runtime_digests
from memvane.provider.adapters import local_embed_provider


ROOT = Path(__file__).resolve().parents[4]

# A fresh process: count every artifact read of two proofs.
_SECOND_PROCESS = r"""
import json, sys
from unittest import mock
from memvane.provider import api as provider, embed_runtime as R
from memvane.provider.adapters import local_embed_provider as L
attested, digest = json.loads(sys.argv[1])
reads = []
real_hash, real_file = R._hash, L._hash_file
count_hash = lambda p, *a, **k: reads.append(str(p)) or real_hash(p, *a, **k)
count_file = lambda p: reads.append(str(p)) or real_file(p)
with mock.patch.object(R, '_hash', count_hash), mock.patch.object(L, '_hash_file', count_file):
    assert R.verify_attested_files(attested, digest) == digest
    for key in ('nodePath', 'moduleEntryPath', 'modelPath'):
        provider.runtime_file_sha256(attested[key])
print(json.dumps(reads))
"""


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


class PersistedHashCache(unittest.TestCase):
    setUp = RuntimeHashCache.setUp

    def cache(self):
        return self.base / 'embed-runtime' / runtime_digests.CACHE_FILE

    def first_process(self):
        self.assertEqual(R.verify_attested_files(self.attested, self.digest), self.digest)
        for key in ('nodePath', 'moduleEntryPath', 'modelPath'):
            provider.runtime_file_sha256(self.attested[key])

    def second_process(self):
        result = subprocess.run(
            [sys.executable, '-B', '-c', _SECOND_PROCESS, json.dumps([self.attested, self.digest])],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout)

    def test_second_process_reads_no_unchanged_artifact(self):
        self.assertFalse(self.cache().exists())
        self.first_process()
        info = self.cache().lstat()
        self.assertEqual((oct(info.st_mode & 0o777), info.st_nlink), ('0o600', 1))
        self.assertEqual(self.second_process(), [])
        self.assertEqual(self.second_process(), [])

    def test_a_changed_identity_is_hashed_again(self):
        self.first_process()
        model = self.generation / R.MODEL
        info = model.stat()
        os.utime(model, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        # mtime moved, bytes equal: the model is read again by both hashers, nothing else.
        self.assertEqual(self.second_process(), [str(model)] * 2)
        self.assertEqual(self.second_process(), [])
        # A size change: the provider hasher reads the file again and the digest moves.
        before = provider.runtime_file_sha256(str(model))
        model.write_bytes(b'gguf-bytes\n' * 65)
        persistent_identity._DIGESTS.clear()
        reads = []
        real = local_embed_provider._hash_file
        with mock.patch.object(local_embed_provider, '_hash_file', lambda p: reads.append(p) or real(p)):
            self.assertNotEqual(provider.runtime_file_sha256(str(model)), before)
        self.assertEqual(reads, [str(model)])
        with self.assertRaises(R.RuntimeUnavailable):
            R.verify_attested_files(self.attested, self.digest)

    def test_restored_mtime_does_not_hide_new_bytes_from_a_later_process(self):
        self.first_process()
        model = self.generation / R.MODEL
        info = model.stat()
        model.write_bytes(b'GGUF-BYTES\n' * 64)  # same size
        os.utime(model, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(model.stat().st_mtime_ns, info.st_mtime_ns)
        result = subprocess.run(
            [sys.executable, '-B', '-c', _SECOND_PROCESS, json.dumps([self.attested, self.digest])],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('laya_embed_runtime_corrupt', result.stderr)

    def test_corrupt_or_unsafe_cache_falls_back_to_hashing(self):
        self.first_process()
        cache = self.cache()
        for damage in (lambda: cache.write_bytes(b'{not json'),
                       lambda: cache.write_text(json.dumps({'schema': 'other', 'rows': {}})),
                       lambda: cache.write_text('[1, 2]'),
                       lambda: cache.chmod(0o644)):
            with self.subTest(damage=damage):
                damage()
                reads = self.second_process()
                self.assertEqual(len(reads), self.files + 3)
                self.assertEqual(oct(cache.lstat().st_mode & 0o777), '0o600')
                self.assertEqual(self.second_process(), [])
        # A symlinked cache is never followed: hashing goes on, the link target stays untouched.
        target = self.base / 'elsewhere.json'
        target.write_text('{}')
        cache.unlink()
        cache.symlink_to(target)
        self.assertEqual(len(self.second_process()), self.files + 3)
        self.assertEqual(target.read_text(), '{}')


if __name__ == '__main__':
    unittest.main()
