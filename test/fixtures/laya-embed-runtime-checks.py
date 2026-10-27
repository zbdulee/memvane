#!/usr/bin/env python3
"""Laya-owned embedding runtime: provisioning, re-attestation, QMD-free use.

A synthetic HOME under /tmp holds a QMD-shaped runtime generation (hoisted or
nested ``node_modules``), a QMD model cache and an fnm-style node. The
"node" is a small Python program that speaks the reviewed worker's socket
protocol and derives each vector from the model and module-entry bytes, so a
moved runtime is proved to produce the same vectors through the unchanged
``start_owned_embed_service`` and provider client. The real provider review,
reproof, validator and bundle-upgrade code run against those files. Nothing
outside the temporary directory is read or written.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

FIXTURES = Path(__file__).resolve().parent
CORE = FIXTURES.parents[1] / 'core'
sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(CORE))
import laya_bundle_upgrade as auto  # noqa: E402
import laya_embed_runtime as R  # noqa: E402
import laya_product_bundle as bundle  # noqa: E402
import laya_product_install as product  # noqa: E402
from laya_index_a import IndexContractError  # noqa: E402
from laya_index_vector import profile_identity  # noqa: E402
from laya_local_embed_provider import (DEFAULT_EMBED_MODEL_URI,  # noqa: E402
                                       ReviewedLocalQmdProvider, recipe_fingerprint)
from laya_local_embed_service import start_owned_embed_service  # noqa: E402
from laya_provider_reproof import attestation_record, reattest  # noqa: E402

REAL_REVIEW = product.review_local_provider
RUNTIME_ID = 'qmd-2.5.3-node-llama-cpp-3.18.1'
RECIPE = 'qmd-regex-token-chunks-candidate-v1'
DIM = 4
PROVIDER_FILES = ('core/laya_local_embed_provider.py', 'core/laya_local_embed_worker.mjs')

# A stand-in for the node binary: binds the manifest socket, answers the
# worker protocol, and embeds from the model and module-entry bytes.
FAKE_NODE = r'''#!{python} -I
import hashlib, json, os, socket, sys, threading
config = json.loads(sys.stdin.readline())
seed = hashlib.sha256(open(config['modelPath'], 'rb').read() +
                      open(config['moduleEntryPath'], 'rb').read()).digest()
ident = {{k: config[k] for k in ('approvedProviderId', 'providerArtifactSha256',
                                 'embeddingFingerprint')}}
def vector(text):
    raw = hashlib.sha256(seed + text.encode()).digest()
    return [round(b / 255 - .5, 6) for b in raw[:config['dimension']]]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(config['serviceSocketPath'])
os.chmod(config['serviceSocketPath'], 0o600)
server.listen(8)
def handle(conn):
    with conn:
        data = b''
        while True:
            block = conn.recv(65536)
            if not block:
                break
            data += block
        request = json.loads(data)
        if request.get('kind') == 'document_chunks':
            result = {{'chunks': [{{'seq': 0, 'pos': 0, 'vector': vector(request['text'])}}]}}
        else:
            result = {{'vector': vector(request['text'])}}
        conn.sendall(json.dumps({{**ident, 'ok': True, 'result': result}}).encode())
def serve():
    while True:
        conn, _ = server.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
threading.Thread(target=serve, daemon=True).start()
sys.stdout.write('READY\n'); sys.stdout.flush()
sys.stdin.read()
os._exit(0)
'''


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, raw, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
    if mode is not None:
        path.chmod(mode)
    return path


def package(path, version, deps=None, optional=None):
    meta = {'name': path.name, 'version': version}
    if deps:
        meta['dependencies'] = deps
    if optional:
        meta['optionalDependencies'] = optional
    write(path / 'package.json', json.dumps(meta))


class Base(unittest.TestCase):
    """Temp HOME with a QMD runtime generation, QMD model cache and fnm node."""

    LAYOUT = 'hoisted'

    def setUp(self):
        # Short base: the provider socket path is limited to 100 bytes.
        tmp = tempfile.TemporaryDirectory(prefix='ler-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        env = mock.patch.dict(os.environ, {'HOME': str(self.home),
                                           'GIT_CONFIG_GLOBAL': os.devnull,
                                           'GIT_CONFIG_NOSYSTEM': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.qmd_runtime = (self.home / 'Library' / 'Application Support' /
                            'qmd-auto-context' / 'runtimes' / 'qmd')
        self.qmd_cache = self.home / '.cache' / 'qmd'
        self.fnm = self.home / '.local' / 'share' / 'fnm'
        self.build_qmd_tree(self.LAYOUT)
        self.plugin = self.base / 'plugin'
        for name in PROVIDER_FILES:
            write(self.plugin / name, (CORE / Path(name).name).read_bytes())
        self.state = self.base / 'state'
        (self.state / 'runtime').mkdir(parents=True, mode=0o700)
        self.state.chmod(0o700)
        self.old_att = self.attestation(self.old_manifest(), self.old_closure)

    def build_qmd_tree(self, layout):
        generation = self.qmd_runtime / 'generations' / 'g-0857'
        top = generation / 'package' / 'node_modules'
        qmd_pkg = top / '@tobilu' / 'qmd'
        package(qmd_pkg, '2.5.3', deps={'node-llama-cpp': '3.18.1',
                                        'better-sqlite3': '^12'})
        write(qmd_pkg / 'bin' / 'qmd', 'synthetic qmd', 0o755)
        package(top / 'better-sqlite3', '12.0.0')
        write(top / 'better-sqlite3' / 'build' / 'better_sqlite3.node', b'qmd only')
        home = qmd_pkg / 'node_modules' if layout == 'nested' else top
        module = home / 'node-llama-cpp'
        package(module, '3.18.1', deps={'lifecycle-utils': '^3', 'which': '^6'},
                optional={'@node-llama-cpp/mac-arm64-metal': '3.18.1',
                          '@node-llama-cpp/linux-x64': '3.18.1'})
        write(module / 'dist' / 'index.js', 'export const getLlama = () => null;\n')
        write(module / 'llama' / 'binariesGithubRelease.json', '{"release":"b1"}')
        package(module / 'node_modules' / 'which', '6.0.1', deps={'isexe': '^3'})
        write(module / 'node_modules' / 'which' / 'lib' / 'index.js', 'which')
        (module / 'node_modules' / '.bin').mkdir()
        (module / 'node_modules' / '.bin' / 'node-which').symlink_to('../which/lib/index.js')
        package(top / 'lifecycle-utils', '3.1.1')
        write(top / 'lifecycle-utils' / 'dist' / 'index.js', 'lifecycle')
        package(top / 'isexe', '3.1.1')
        write(top / 'isexe' / 'index.js', 'isexe')
        native = home / '@node-llama-cpp' / 'mac-arm64-metal'
        package(native, '3.18.1')
        bins = native / 'bins' / 'mac-arm64-metal'
        for name, raw in (('llama-addon.node', b'addon' * 50),
                          ('libggml-base.dylib', b'ggml' * 50),
                          ('libggml-cpu.so', b'cpu' * 50)):
            write(bins / name, raw)
        self.node = write(self.fnm / 'node-versions' / 'v24.14.0' / 'installation' /
                          'bin' / 'node', FAKE_NODE.format(python=sys.executable), 0o755)
        write(generation / 'prepared.json', json.dumps({
            'schema': 'qmd-managed-runtime-v1',
            'proof': {'node': str(self.node), 'nodeSha256': sha_file(self.node),
                      'qmd': str(qmd_pkg / 'bin' / 'qmd'), 'version': '2.5.3'}}))
        write(self.qmd_runtime / 'active.json', json.dumps({
            'schema': 'qmd-managed-runtime-v1', 'generation': str(generation),
            'previous': None}))
        self.model = write(self.qmd_cache / 'models' /
                           'hf_ggml-org_embeddinggemma-300M-Q8_0.gguf',
                           b'GGUF' + b'synthetic embedding model' * 40)
        self.module_entry = module / 'dist' / 'index.js'
        self.native = native
        self.old_closure = self.closure(native)

    @staticmethod
    def closure(native):
        rows = [path for path in Path(native).rglob('*')
                if path.suffix in ('.node', '.dylib', '.so')]
        return [{'path': str(path), 'sha256': sha_file(path), 'bytes': path.stat().st_size}
                for path in sorted(rows)]

    def old_manifest(self):
        """The manifest main's review sealed: QMD runtime and QMD cache paths."""
        closure_sha = hashlib.sha256(bundle._canonical(self.old_closure)).hexdigest()
        provider_sha = sha_file(self.plugin / PROVIDER_FILES[0])
        worker_sha = sha_file(self.plugin / PROVIDER_FILES[1])
        content = dict(model_id=DEFAULT_EMBED_MODEL_URI, model_sha256=sha_file(self.model),
                       runtime_id=RUNTIME_ID, recipe_id=RECIPE, provider_sha256=provider_sha,
                       worker_sha256=worker_sha, node_sha256=sha_file(self.node),
                       module_entry_sha256=sha_file(self.module_entry))
        fingerprint = recipe_fingerprint(**content, closure_sha256=closure_sha)
        approved = 'local-qmd-' + hashlib.sha256(
            (provider_sha + worker_sha + closure_sha).encode()).hexdigest()[:24]
        return {'approvedProviderId': approved, 'providerArtifactSha256': provider_sha,
                'providerRelativePath': PROVIDER_FILES[0],
                'providerPath': str(self.plugin / PROVIDER_FILES[0]),
                'workerPath': str(self.plugin / PROVIDER_FILES[1]),
                'workerSha256': worker_sha,
                'serviceSocketPath': str(self.state / 'runtime' / 'laya-embed.sock'),
                'nodePath': str(self.node), 'nodeSha256': content['node_sha256'],
                'moduleEntryPath': str(self.module_entry),
                'moduleEntrySha256': content['module_entry_sha256'],
                'modelPath': str(self.model), 'modelSha256': content['model_sha256'],
                'runtimeId': RUNTIME_ID, 'modelId': DEFAULT_EMBED_MODEL_URI,
                'embeddingFingerprint': fingerprint, 'dimension': DIM, 'recipeId': RECIPE,
                'dependencyClosureApproved': True, 'dependencyClosureSha256': closure_sha}

    def attestation(self, manifest, closure):
        profile = {'profileId': profile_identity(manifest['modelId'],
                                                 manifest['embeddingFingerprint'], DIM),
                   'modelId': manifest['modelId'],
                   'embeddingFingerprint': manifest['embeddingFingerprint'],
                   'dimension': DIM, 'metric': 'cosine',
                   'approvedProviderId': manifest['approvedProviderId'],
                   'providerArtifactSha256': manifest['providerArtifactSha256'],
                   'providerRelativePath': PROVIDER_FILES[0]}
        return {'schema': bundle.PRODUCTION_ATTESTATION_SCHEMA, 'sourceCommit': 'c' * 40,
                'bundleManifestSha256': 'd' * 64, 'ownershipManifestSha256': 'e' * 64,
                'journalPath': str(self.state / 'install-journal.sqlite'),
                'vectorProfile': profile, 'providerManifest': manifest,
                'dependencyClosure': closure}

    def validate(self, attestation):
        proof = {'status': 'exact_commit_full_plugin_candidate_consistent',
                 'pluginRoot': str(self.plugin), 'sourceCommit': 'c' * 40,
                 'bundleManifestSha256': 'd' * 64, 'ownershipManifestSha256': 'e' * 64,
                 'vectorProfile': attestation['vectorProfile']}
        with mock.patch.object(bundle, 'inspect_full_plugin_candidate', return_value=proof):
            return bundle.validate_production_provider_attestation(
                attestation['vectorProfile'], proof, attestation)

    def remove_qmd(self):
        """Every QMD-owned directory, plus the node the old review recorded."""
        for path in (self.qmd_runtime, self.qmd_cache, self.fnm):
            shutil.rmtree(path)

    def source_repo(self):
        """A marketplace clone whose HEAD holds the installed provider files."""
        source = self.base / 'source'
        for name in PROVIDER_FILES:
            write(source / name, (self.plugin / name).read_bytes())
        git = ['git', '-C', str(source), '-c', 'user.email=t@example.invalid',
               '-c', 'user.name=t', '-c', 'commit.gpgsign=false']
        subprocess.run(git[:3] + ['init', '-q'], check=True)
        subprocess.run(git + ['add', '-A'], check=True)
        subprocess.run(git + ['commit', '-qm', 'provider'], check=True)
        commit = subprocess.run(git[:3] + ['rev-parse', 'HEAD'], check=True,
                                capture_output=True, text=True).stdout.strip()
        return source, commit

    def review(self, source, commit, **extra):
        with mock.patch.dict(sys.modules, {'qmd_runtime': None}), \
                mock.patch('platform.system', return_value='Darwin'), \
                mock.patch('platform.machine', return_value='arm64'):
            return REAL_REVIEW(source_root=str(source), plugin_root=str(self.plugin),
                               state_root=str(self.state), commit_id=commit,
                               **{'dimension': DIM, **extra})

    def vectors(self, manifest, name):
        """One query and one document through the real service and client."""
        socket_dir = self.base / ('s-' + name)
        socket_dir.mkdir(mode=0o700)
        live = dict(manifest, serviceSocketPath=str(socket_dir / 'e.sock'))
        handle = start_owned_embed_service(live, admit=lambda: True)
        try:
            provider = ReviewedLocalQmdProvider(live)
            return (provider._embed('query', 'emerald heron', timeout=2),
                    provider.embed_document_chunks('# Heron\n\nnests by the pond', 'a/b.md',
                                                   timeout=30))
        finally:
            handle.stop()


class Provisioning(Base):
    def files(self, runtime):
        generation = Path(runtime['generation'])
        return {path.relative_to(generation).as_posix(): path
                for path in generation.rglob('*') if not path.is_dir()}

    def assert_private_copies(self, runtime, sources):
        for rel, path in self.files(runtime).items():
            info = path.lstat()
            self.assertEqual(info.st_nlink, 1, rel)
            self.assertFalse(info.st_mode & 0o077, rel)
            if rel in sources:
                self.assertNotEqual(info.st_ino, Path(sources[rel]).stat().st_ino, rel)

    def test_attested_bytes_are_copied_once_and_sha_checked(self):
        # A copy killed mid-way left a staging tree behind.
        stale = R.root() / 'generations' / '.staging-killed'
        write(stale / 'node_modules' / 'partial.js', 'partial')
        R.root().chmod(0o700)
        for path in (R.root().parents[2], R.root().parents[1], R.root().parent,
                     R.root() / 'generations'):
            path.chmod(0o700)
        runtime = R.ensure_for_attestation(self.old_att)
        self.assertFalse(stale.exists())
        self.assertTrue(R.matches(runtime, self.old_att['providerManifest'],
                                  self.old_closure))
        # Already provisioned: no second copy.
        with mock.patch.object(R, '_provision', side_effect=AssertionError('copied')):
            self.assertEqual(R.ensure_for_attestation(self.old_att), runtime)
        # Re-provisioning the same bytes lands on the same content-addressed path.
        self.assertEqual(R.provision_from_attestation(self.old_att), runtime)

    def test_changed_or_missing_sources_refuse_without_activation(self):
        model = bytearray(self.model.read_bytes())
        model[-1] ^= 1
        self.model.write_bytes(bytes(model))
        with self.assertRaisesRegex(R.RuntimeUnavailable,
                                    '^laya_embed_runtime_source_changed$'):
            R.ensure_for_attestation(self.old_att)
        self.assertFalse((R.root() / 'active.json').exists())
        self.assertEqual(list((R.root() / 'generations').iterdir()), [])
        shutil.rmtree(self.qmd_cache)
        with self.assertRaisesRegex(R.RuntimeUnavailable,
                                    '^laya_embed_runtime_source_unavailable$'):
            R.ensure_for_attestation(self.old_att)
        # The bundle worker turns it into a refusal that is not retried soon.
        with self.assertRaisesRegex(auto.UpgradeRefused,
                                    '^laya_embed_runtime_source_unavailable$'):
            auto._ensure_embed_runtime(self.old_att)
        self.assertIn('laya_embed_runtime_source_unavailable', auto.REFUSALS)

    def test_fixture_never_touches_the_account_home(self):
        with mock.patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '1'}), \
                mock.patch.object(R, '_account_home', return_value=self.home):
            for call in (R.select, lambda: R.ensure_for_attestation(self.old_att)):
                with self.assertRaisesRegex(R.RuntimeUnavailable,
                                            '^laya_embed_runtime_fixture_real_home$'):
                    call()
        self.assertFalse((self.home / '.local' / 'state').exists())


class NestedProvisioning(Provisioning):
    """QMD's own ``node_modules`` holds node-llama-cpp; shared deps are hoisted."""
    LAYOUT = 'nested'


class CorruptRuntime(Base):
    def setUp(self):
        super().setUp()
        self.runtime = R.ensure_for_attestation(self.old_att)
        self.generation = Path(self.runtime['generation'])
        self.source, self.commit = self.source_repo()

    def corruptions(self):
        dep = self.generation / 'node_modules' / 'lifecycle-utils' / 'dist' / 'index.js'
        native = Path(self.runtime['dependencyClosure'][0]['path'])

        def flip(path):
            raw = bytearray(path.read_bytes())
            raw[0] ^= 1
            path.write_bytes(bytes(raw))
        yield 'js dependency byte', lambda: flip(dep)
        yield 'native byte', lambda: flip(native)
        yield 'model byte', lambda: flip(Path(self.runtime['modelPath']))
        yield 'extra file', lambda: write(self.generation / 'node_modules' / 'x.js', 'x', 0o600)
        yield 'missing file', lambda: dep.unlink()
        yield 'symlinked file', lambda: (dep.unlink(), dep.symlink_to(self.base))
        yield 'group readable', lambda: dep.chmod(0o640)
        yield 'hardlinked file', lambda: os.link(dep, self.base / 'second-link')
        yield 'manifest edit', lambda: (self.generation / 'runtime.json').write_bytes(
            (self.generation / 'runtime.json').read_bytes().replace(b'3.18.1', b'3.18.2'))
        yield 'pointer', lambda: (R.root() / 'active.json').write_text('{"generation":1}')

    def test_corrupted_runtime_refuses_review_and_reprovisioning(self):
        good = self.review(self.source, self.commit)
        for name, corrupt in self.corruptions():
            with self.subTest(corruption=name):
                saved = self.base / 'saved'
                shutil.copytree(R.root(), saved, symlinks=True)
                corrupt()
                with self.assertRaisesRegex(R.RuntimeUnavailable,
                                            '^laya_embed_runtime_corrupt$'):
                    R.select()
                with self.assertRaisesRegex(product.InstallBlocked,
                                            '^provider_review_laya_embed_runtime_corrupt$'):
                    self.review(self.source, self.commit)
                if name != 'pointer':
                    # The same content id cannot be silently rebuilt over it.
                    with self.assertRaisesRegex(
                            R.RuntimeUnavailable,
                            '^laya_embed_runtime_generation_conflict$'):
                        R.ensure_for_attestation(self.old_att)
                shutil.rmtree(R.root())
                (self.base / 'second-link').unlink(missing_ok=True)
                shutil.copytree(saved, R.root(), symlinks=True)
                shutil.rmtree(saved)
                self.assertEqual(self.review(self.source, self.commit), good)


class ReattestedInstall(Base):
    """An attestation that already names the Laya runtime."""

    def test_runtime_named_by_the_attestation_is_reselected_never_recopied(self):
        runtime = R.ensure_for_attestation(self.old_att)
        manifest = dict(self.old_att['providerManifest'], nodePath=runtime['nodePath'],
                        moduleEntryPath=runtime['moduleEntryPath'],
                        modelPath=runtime['modelPath'])
        laya_att = dict(self.old_att, providerManifest=manifest,
                        dependencyClosure=runtime['dependencyClosure'])
        self.remove_qmd()
        # Another generation became active (for example a manual provision).
        (R.root() / 'active.json').write_text('{}')
        (R.root() / 'active.json').chmod(0o600)
        with mock.patch.object(R, '_provision', side_effect=AssertionError('copied')):
            self.assertEqual(R.ensure_for_attestation(laya_att), runtime)
            # A changed dependency the attestation does not hash is refused,
            # not laundered into a new generation.
            dep = Path(runtime['generation']) / 'node_modules' / 'isexe' / 'index.js'
            dep.write_text('changed')
            with self.assertRaisesRegex(R.RuntimeUnavailable,
                                        '^laya_embed_runtime_corrupt$'):
                R.ensure_for_attestation(laya_att)


class MovedProvider(Base):
    """The 9 selected installs: bytes move, the vector profile stays."""

    def test_reattest_after_move_keeps_profile_and_vectors_without_qmd(self):
        before = self.vectors(self.old_att['providerManifest'], 'qmd')
        self.validate(self.old_att)
        runtime = R.ensure_for_attestation(self.old_att)
        source, commit = self.source_repo()
        review = self.review(source, commit)
        manifest = review['reviewedProvider']['providerManifest']
        self.assertEqual(manifest['nodePath'], runtime['nodePath'])
        self.assertEqual(manifest['modelPath'], runtime['modelPath'])
        # The path-derived profile differs; content equality re-attests it.
        self.assertNotEqual(review['reviewedProvider']['vectorProfile'],
                            self.old_att['vectorProfile'])
        self.assertTrue(auto.provider_equivalent(self.old_att, review, self.plugin))
        again = reattest(self.old_att, review['reviewedProvider'])
        self.assertEqual(again['vectorProfile'], self.old_att['vectorProfile'])
        self.assertEqual(again['profileDependencyClosure'], self.old_closure)
        proof = {'sourceCommit': 'c' * 40, 'bundleManifestSha256': 'd' * 64,
                 'ownershipManifestSha256': 'e' * 64,
                 'vectorProfile': again['vectorProfile']}
        record = attestation_record(proof=proof,
                                    journal_path=self.state / 'install-journal.sqlite',
                                    reattested=again,
                                    schema=bundle.PRODUCTION_ATTESTATION_SCHEMA)
        self.validate(record)
        live = record['providerManifest']
        self.assertEqual(ReviewedLocalQmdProvider(live).profile['profile_id'],
                         self.old_att['vectorProfile']['profileId'])
        # Delete every QMD directory and the old node: nothing still names them.
        self.remove_qmd()
        names = json.dumps(record)
        for gone in (self.qmd_runtime, self.qmd_cache, self.fnm):
            self.assertNotIn(str(gone), json.dumps({**record, 'profileDependencyClosure': []}))
        self.assertIn(str(self.qmd_runtime), names)  # only as the reviewed closure rows
        self.validate(record)
        ReviewedLocalQmdProvider(live)._verify_artifacts()
        self.assertEqual(self.vectors(live, 'laya'), before)
        # A later bundle upgrade reviews the same runtime and stays equivalent.
        self.assertIsNotNone(reattest(record, self.review(source, commit)['reviewedProvider']))
        with self.assertRaisesRegex(IndexContractError, 'provider_artifact'):
            ReviewedLocalQmdProvider(self.old_att['providerManifest'])._verify_artifacts()

    def test_bundle_worker_provisions_then_reattests_through_real_review(self):
        source, commit = self.source_repo()
        host = self.home / '.claude' / 'plugins' / 'cache' / 'mkt' / 'qmd-auto-context' / '9.9.9'
        shutil.copytree(self.plugin, host)
        calls = []
        identity = {'pluginRoot': str(self.plugin), 'sourceCommit': '1' * 40,
                    'bundleManifestSha256': 'a' * 64,
                    'vectorProfile': self.old_att['vectorProfile']}
        import laya_active_upgrade
        with mock.patch.object(auto, 'classify', return_value=('active', identity)), \
                mock.patch.object(auto, 'installed_commit', return_value=None), \
                mock.patch.object(auto, 'select_source', return_value=(source, commit)), \
                mock.patch.object(auto, '_not_older'), \
                mock.patch.object(auto, '_read_attestation', return_value=self.old_att), \
                mock.patch.object(product, 'review_local_provider',
                                  lambda **kw: self.review_at(host, **kw)), \
                mock.patch.object(product, '_request_build_wake',
                                  return_value={'status': 'queued'}), \
                mock.patch.object(laya_active_upgrade, 'upgrade',
                                  side_effect=lambda **kw: calls.append(kw) or
                                  {'status': 'active_upgrade_complete'}):
            result = auto.upgrade_install(self.base / 'project', self.state, host)
        self.assertEqual(result['status'], 'active_upgrade_complete', result)
        self.assertIsNone(calls[0]['model_path'])
        self.assertTrue(R.matches(R.select(), self.old_att['providerManifest'],
                                  self.old_closure))

    def review_at(self, host, **kw):
        with mock.patch.dict(sys.modules, {'qmd_runtime': None}), \
                mock.patch('platform.system', return_value='Darwin'), \
                mock.patch('platform.machine', return_value='arm64'):
            return REAL_REVIEW(**{**kw, 'plugin_root': str(host)})


class UpgradeFence(unittest.TestCase):
    """The upgrade fence waits on the sockets the attestations seal."""

    def test_fence_waits_for_each_attested_socket(self):
        import laya_active_upgrade as active
        old = {'epoch': 4, 'phase': 'active'}
        intent = {'stateRoot': '/state', 'oldLedger': old,
                  'newLedger': {'epoch': 6, 'phase': 'upgrading'},
                  'oldAttestation': json.dumps({'providerManifest': {
                      'serviceSocketPath': '/install/runtime/laya-embed.sock'}}),
                  'newAttestation': json.dumps({'providerManifest': {
                      'serviceSocketPath': '/service/install.sock'}})}

        class Ledger:
            name = 'fresh-wiki-cutover.json'

            @staticmethod
            def load(_path):
                return intent['newLedger']
        waited = []
        with mock.patch.object(active, '_intent_ledger', return_value=Ledger), \
                mock.patch('laya_embed_resident.wait_for_release',
                           side_effect=lambda path, timeout: waited.append(path) or True):
            active._fence(intent)
        self.assertEqual(waited, ['/install/runtime/laya-embed.sock',
                                  '/service/install.sock'])
        with mock.patch.object(active, '_intent_ledger', return_value=Ledger), \
                mock.patch('laya_embed_resident.wait_for_release',
                           side_effect=lambda path, timeout: path.startswith('/install')):
            with self.assertRaisesRegex(Exception, '^active_upgrade_resident_busy$'):
                active._fence(intent)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVICE = _load('laya_resident_service_checks', FIXTURES / 'laya-resident-service-checks.py')


class ServicePickup(SERVICE.Fixture):
    """The shared worker keeps serving; its successor starts from the new paths."""

    def test_reattested_manifest_is_used_by_the_next_worker_start(self):
        started = []
        real_start = self.start_worker

        def start_worker(manifest, admit, **kw):
            started.append(manifest['nodePath'])
            return real_start(manifest, admit, **kw)
        self.start_worker = start_worker
        project, manifest = self.project('alpha')
        SERVICE.svc.register(project, root=self.root)
        old = self.service(bundle={'version': '1.0.0', 'digest': 'old'})
        self.wait(lambda: self.ready(manifest))
        self.assertEqual(started, ['/synthetic/node'])
        # Re-attestation: same identity, Laya runtime paths.
        laya = dict(manifest, nodePath='/laya/embed-runtime/g/bin/node',
                    moduleEntryPath='/laya/embed-runtime/g/node_modules/x/dist/index.js',
                    modelPath='/laya/embed-runtime/g/models/m.gguf')
        decision = self.routes[project][0]
        self.routes[project] = (decision, {'providerManifest': laya})
        self.wait(lambda: old.bindings.get(project) is not None and
                  old.bindings[project].manifest == laya, message='not re-adopted')
        self.wait(lambda: self.ready(laya))
        self.assertEqual(len(SERVICE.embed(laya)), SERVICE.DIM)
        self.assertEqual(started, ['/synthetic/node'])  # the loaded worker kept serving
        # A newer bundle replaces the service; its worker starts from Laya paths.
        with mock.patch.object(SERVICE.svc, 'bundle_identity',
                               return_value={'version': '1.0.1', 'digest': 'new'}):
            SERVICE.svc.ensure_service(root=self.root, learning_root=False,
                                       spawn=lambda argv, **kw: None)
        self.assertTrue(old.stopped.wait(15))
        old.close()
        self.service(bundle={'version': '1.0.1', 'digest': 'new'})
        self.wait(lambda: self.ready(laya))
        self.assertEqual(len(SERVICE.embed(laya)), SERVICE.DIM)
        self.assertEqual(started, ['/synthetic/node', '/laya/embed-runtime/g/bin/node'])


E2E = runpy.run_path(str(FIXTURES / 'laya-fresh-onboarding-e2e-checks.py'))


class QueryProvider:
    """The warm query owner of the synthetic build profile (as in the e2e)."""

    def __init__(self, manifest):
        self.manifest = manifest

    def __call__(self, prompt, profile):
        return {'profileId': profile['profileId'],
                'approvedProviderId': profile['approvedProviderId'],
                'providerArtifactSha256': profile['providerArtifactSha256'],
                'vector': [1.0, 0.0]}


class OnboardingWithOnlyLayaRuntime(E2E['FreshOnboarding']):
    """A new project on a machine whose only embedding runtime is Laya's."""

    def setUp(self):
        super().setUp()
        for path in PROVIDER_FILES:
            write(self.plugin / path, (CORE / Path(path).name).read_bytes())
        git = ['git', '-C', str(self.source), '-c', 'user.email=t@example.invalid',
               '-c', 'user.name=t', '-c', 'commit.gpgsign=false']
        for path in PROVIDER_FILES:
            write(self.source / path, (CORE / Path(path).name).read_bytes())
        with mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': os.devnull,
                                          'GIT_CONFIG_NOSYSTEM': '1'}):
            subprocess.run(git[:3] + ['init', '-q'], check=True)
            subprocess.run(git + ['add', '-A'], check=True)
            subprocess.run(git + ['commit', '-qm', 'provider'], check=True)
            self.commit = subprocess.run(git[:3] + ['rev-parse', 'HEAD'], check=True,
                                         capture_output=True, text=True).stdout.strip()
        # A vendor tree outside any QMD path, copied into the Laya runtime, then gone.
        vendor = self.home / 'vendor' / 'node_modules'
        package(vendor / 'node-llama-cpp', '3.18.1',
                optional={'@node-llama-cpp/mac-arm64-metal': '3.18.1'})
        write(vendor / 'node-llama-cpp' / 'dist' / 'index.js', 'export {}')
        native = vendor / '@node-llama-cpp' / 'mac-arm64-metal'
        package(native, '3.18.1')
        write(native / 'bins' / 'llama-addon.node', b'addon')
        write(native / 'bins' / 'libllama.dylib', b'dylib')
        node = write(self.home / 'vendor' / 'node', b'node', 0o755)
        model = write(self.home / 'vendor' / 'model.gguf', b'GGUF model')
        self.runtime = R._provision(node=str(node), module_entry=str(
            vendor / 'node-llama-cpp' / 'dist' / 'index.js'), model=str(model), expect={})
        shutil.rmtree(self.home / 'vendor')
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(product, 'review_local_provider', REAL_REVIEW))
        stack.enter_context(mock.patch('platform.system', return_value='Darwin'))
        stack.enter_context(mock.patch('platform.machine', return_value='arm64'))

    def test_minimal_settings_project_is_built_selected_and_recalled(self):
        for path in (self.home / '.cache' / 'qmd',
                     self.home / 'Library' / 'Application Support' / 'qmd-auto-context'):
            self.assertFalse(path.exists())
        plan = product.plan_fresh_wiki_origin(self.project)
        # Review and prepare never import QMD's runtime module. (The build's
        # wiki-card path still imports recall.py's QMD modules: PR-10.)
        with mock.patch.dict(sys.modules, {'qmd_runtime': None}):
            review = product.review_local_provider(
                source_root=str(self.source), plugin_root=str(self.plugin),
                state_root=str(self.state), commit_id=self.commit, dimension=2)
            manifest = review['reviewedProvider']['providerManifest']
            self.assertEqual(manifest['modelPath'], self.runtime['modelPath'])
            self.assertEqual(manifest['nodePath'], self.runtime['nodePath'])
            prepared = product.prepare_fresh_wiki_origin(
                project_root=self.project, source_root=str(self.source),
                plugin_root=str(self.plugin), state_root=str(self.state),
                commit_id=self.commit, expected_proposal_sha256=plan['proposalSha256'],
                install_id=E2E['BUILD']['INSTALL'],
                accepted_provider_sha256=review['proposalSha256'], dimension=2)
        self.assertEqual(prepared['status'], 'initial_build_registered', prepared)
        sealed = json.loads((self.state / 'production-provider-attestation.json').read_bytes())
        self.assertEqual(sealed['providerManifest'], manifest)
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        self.assertEqual(self.finalize()['status'], 'freshness_pending')
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        self.assertEqual(self.finalize()['status'], 'cutover_prepared')
        route_bytes = (self.state / 'reviewed-route.json').read_bytes()
        with product._install_lock(self.state):
            active = product.activate_origin_selection(self.project, self.journal_path,
                                                       route_bytes)
        self.assertEqual(active['phase'], 'active', active)
        decision = E2E['host_route'].select_route(str(self.project), action='recall')
        proof = E2E['journal_mod'].verify_selected_route_read_only(decision)
        with mock.patch('laya_local_embed_provider.ReviewedLocalQmdProvider',
                        QueryProvider), \
                mock.patch('laya_embed_resident.admit_query', lambda **_kw: None):
            served = E2E['response']._serve_recall(
                'claude', decision, {'prompt': 'emerald heron'}, proof,
                E2E['journal_mod'].verify_selected_route_read_only)
        self.assertIn('emerald heron nests', served['hookSpecificOutput']['additionalContext'])

    # Covered by laya-fresh-onboarding-e2e; not repeated here.
    test_qmd_origin_marker_is_refused_with_a_clear_reason = None


if __name__ == '__main__':
    unittest.main()
