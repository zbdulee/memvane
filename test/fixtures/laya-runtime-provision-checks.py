#!/usr/bin/env python3
"""New-machine provisioning, runtime generation GC and the owner QMD cleanup.

Every check runs in a temporary HOME. Network and processes are injected:
the npm runner copies a synthetic registry tree, the model downloader writes
synthetic bytes, ``ps``/``kill``/``launchctl`` are fakes. Nothing outside
the temporary directory is read, written, signalled or downloaded.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import sys
import tempfile
import unittest
from unittest import mock

FIXTURES = Path(__file__).resolve().parent
CORE = FIXTURES.parents[1] / 'core'
REPO = FIXTURES.parents[1]
sys.path.insert(0, str(CORE))
EMBED = runpy.run_path(str(FIXTURES / 'laya-embed-runtime-checks.py'))
import laya_embed_runtime as R  # noqa: E402
from memvane.kernel import safe_rmtree  # noqa: E402

MACHO = b'\xcf\xfa\xed\xfe' + b'synthetic node binary'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def locked_dir(case, path):
    """A directory whose entries cannot be unlinked (mode 0500)."""
    write(path / 'pinned', 'x')
    path.chmod(0o500)
    case.addCleanup(path.chmod, 0o700)
    return path


def write(path, raw, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
    if mode is not None:
        path.chmod(mode)
    return path


class Result:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Machine(EMBED['Base']):
    """A clean machine: a node install and a registry, no QMD directory at all."""

    def setUp(self):
        super().setUp()
        # The synthetic QMD tree only serves as the "registry" contents.
        self.registry = self.base / 'registry'
        shutil.copytree(self.qmd_runtime / 'generations' / 'g-0857' / 'package' / 'node_modules',
                        self.registry)
        shutil.rmtree(self.qmd_runtime)
        shutil.rmtree(self.qmd_cache)
        self.model_bytes = b'GGUF' + b'synthetic downloaded model' * 30
        # node from the fnm fixture plus npm's CLI beside it.
        self.npm_cli = write(self.node.parents[1] / 'lib' / 'node_modules' / 'npm' / 'bin' /
                             'npm-cli.js', '// npm')
        self.runs = []
        self.fetches = []

    def npm(self, argv, *, cwd=None, env=None, timeout=60):
        self.runs.append({'argv': list(argv), 'cwd': cwd, 'env': dict(env or {})})
        if argv[1:] == ['--version']:
            return Result(stdout='v24.14.0\n')
        shutil.copytree(self.registry, Path(cwd) / 'node_modules')
        return Result()

    def fetch(self, url, target):
        self.fetches.append(url)
        write(target, self.model_bytes, 0o600)

    def provision(self, **options):
        return R.provision_new(approve_download=True, node=self.node, run=self.npm,
                               fetch=self.fetch, model_sha256=sha(self.model_bytes), **options)

    def auto_provision(self, path_env, *, run=None):
        # Keep real manager discovery under this fixture HOME, never a system
        # install as a fallback. Every subprocess and download is injected.
        manager_nodes = R._manager_nodes
        with mock.patch.object(R, '_manager_nodes', side_effect=lambda home: [
                path for path in manager_nodes(home) if path.is_relative_to(self.home)]):
            return R.provision_new(approve_download=True, home=self.home,
                                   path_env=path_env, run=run or self.npm,
                                   fetch=self.fetch, model_sha256=sha(self.model_bytes))


class Provisioning(Machine):
    def test_homebrew_libexec_npm_uses_the_selected_node(self):
        node = write(self.home / 'Cellar/node/24.14.0/bin/node', MACHO, 0o755)
        self.npm_cli.unlink()
        self.npm_cli = write(node.parents[1] / 'libexec/lib/node_modules/npm/bin/npm-cli.js',
                             '// synthetic Homebrew npm')
        runtime = self.auto_provision(str(node.parent))
        self.assertEqual(runtime['nodeSha256'], sha(node.read_bytes()))
        installs = [row['argv'] for row in self.runs if row['argv'][1:2] == [str(self.npm_cli)]]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0][:4], [str(node), str(self.npm_cli), 'ci', '--ignore-scripts'])

    def test_auto_provision_skips_path_node_without_npm_but_node_only_discovery_keeps_it(self):
        first = write(self.home / 'bin/node', MACHO + b'path', 0o755)
        self.node.write_bytes(MACHO + b'manager')
        self.assertEqual(R.discover_node(path_env=str(first.parent), home=self.home,
                                        run=self.npm)[0], first)
        runtime = self.auto_provision(str(first.parent))
        self.assertEqual(runtime['nodeSha256'], sha(self.node.read_bytes()))
        installs = [row['argv'] for row in self.runs if row['argv'][1:2] == [str(self.npm_cli)]]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0][0], str(self.node))

    def test_supported_nodes_without_npm_refuse_without_network(self):
        first = write(self.home / 'bin/node', MACHO + b'path', 0o755)
        self.node.write_bytes(MACHO + b'manager')
        self.npm_cli.unlink()
        with self.assertRaisesRegex(R.RuntimeUnavailable, '^laya_embed_runtime_npm_unavailable$'):
            self.auto_provision(str(first.parent))
        self.assertEqual(self.fetches, [])
        self.assertTrue(all(row['argv'][1:] == ['--version'] for row in self.runs))

    def test_no_supported_node_has_a_distinct_reason(self):
        first = write(self.home / 'bin/node', MACHO, 0o755)
        self.node.write_bytes(MACHO + b'manager')
        def unsupported(argv, **_kw):
            return Result(stdout='v20.18.0\n')
        with self.assertRaisesRegex(R.RuntimeUnavailable, '^laya_embed_runtime_node_unavailable$'):
            self.auto_provision(str(first.parent), run=unsupported)
        self.assertEqual((self.runs, self.fetches), ([], []))

    def test_explicit_node_without_npm_never_selects_another_node(self):
        self.npm_cli.unlink()
        other = write(self.home / '.nvm/versions/node/v26.1.0/bin/node', MACHO, 0o755)
        write(other.parents[1] / 'lib/node_modules/npm/bin/npm-cli.js', '// other npm')
        with mock.patch.object(R, 'discover_node', side_effect=AssertionError('explicit node changed')):
            with self.assertRaisesRegex(R.RuntimeUnavailable, '^laya_embed_runtime_npm_unavailable$'):
                self.provision()
        self.assertEqual((self.runs, self.fetches), ([], []))

    def test_clean_machine_builds_a_sealed_runtime_from_lock_and_download(self):
        self.assertFalse((self.home / '.cache' / 'qmd').exists())
        self.assertFalse(self.qmd_runtime.exists())
        runtime = self.provision()
        self.assertEqual(R.select(), runtime)
        self.assertEqual(runtime['modelSha256'], sha(self.model_bytes))
        self.assertEqual(runtime['nodeSha256'], sha(self.node.read_bytes()))
        self.assertEqual(self.fetches, [R.MODEL_URL])
        npm = [run for run in self.runs if run['argv'][1:2] == [str(self.npm_cli)]]
        self.assertEqual(len(npm), 1)
        self.assertEqual(npm[0]['argv'][2:4], ['ci', '--ignore-scripts'])
        self.assertEqual(npm[0]['env']['npm_config_ignore_scripts'], 'true')
        work = Path(npm[0]['cwd'])
        self.assertFalse(work.exists(), 'npm work tree is removed after sealing')
        files = {row[0] for row in json.loads(
            (Path(runtime['generation']) / 'runtime.json').read_text())['files']}
        # Only node-llama-cpp's runtime closure is sealed; other registry
        # packages and npm's .bin symlinks are not.
        expected = {'bin/node', 'models/embeddinggemma-300M-Q8_0.gguf',
                    'node_modules/lifecycle-utils/package.json',
                    'node_modules/lifecycle-utils/dist/index.js',
                    'node_modules/isexe/package.json', 'node_modules/isexe/index.js'}
        expected.update('node_modules/node-llama-cpp/' + rel for rel in (
            'package.json', 'dist/index.js', 'llama/binariesGithubRelease.json',
            'node_modules/which/package.json', 'node_modules/which/lib/index.js'))
        expected.update('node_modules/@node-llama-cpp/mac-arm64-metal/' + rel for rel in (
            'package.json', 'bins/mac-arm64-metal/llama-addon.node',
            'bins/mac-arm64-metal/libggml-base.dylib',
            'bins/mac-arm64-metal/libggml-cpu.so'))
        self.assertEqual(files, expected)
        for rel in files:
            info = (Path(runtime['generation']) / rel).lstat()
            self.assertEqual(info.st_nlink, 1, rel)
            self.assertFalse(info.st_mode & 0o077, rel)
        # The pinned lock is exactly what npm received.
        lock = json.loads((R.LOCK_DIR / 'package-lock.json').read_text())
        self.assertEqual(lock['packages']['node_modules/node-llama-cpp']['version'],
                         R.LLAMA_VERSION)
        self.assertEqual(lock['packages']['']['dependencies'],
                         {'node-llama-cpp': R.LLAMA_VERSION})

    def test_download_and_npm_need_explicit_approval(self):
        with self.assertRaisesRegex(R.RuntimeUnavailable,
                                    '^laya_embed_runtime_download_approval_required$'):
            R.provision_new(node=self.node, run=self.npm, fetch=self.fetch)
        self.assertEqual((self.runs, self.fetches), ([], []))

    def test_model_bytes_must_match_the_pinned_sha(self):
        with self.assertRaisesRegex(R.RuntimeUnavailable,
                                    '^laya_embed_runtime_model_sha_mismatch$'):
            R.provision_new(approve_download=True, node=self.node, run=self.npm,
                            fetch=self.fetch)  # default pin is the real GGUF sha
        self.assertFalse((R.root() / 'active.json').exists())
        self.assertEqual([p.name for p in R.root().iterdir() if p.name.startswith('.provision')], [])

    def test_local_model_file_skips_the_download(self):
        model = write(self.base / 'm.gguf', self.model_bytes)
        runtime = self.provision(model_file=str(model))
        self.assertEqual(self.fetches, [])
        self.assertEqual(runtime['modelSha256'], sha(self.model_bytes))

    def test_failed_npm_activates_nothing(self):
        def broken(argv, **kw):
            return Result(returncode=1)
        with self.assertRaisesRegex(R.RuntimeUnavailable, '^laya_embed_runtime_npm_failed$'):
            R.provision_new(approve_download=True, node=self.node, run=broken,
                            fetch=self.fetch, model_sha256=sha(self.model_bytes))
        self.assertFalse((R.root() / 'active.json').exists())

    def test_default_downloader_never_runs_in_tests(self):
        # The pinned URL is HTTPS and the downloader refuses anything else.
        self.assertTrue(R.MODEL_URL.startswith('https://huggingface.co/'))
        with self.assertRaisesRegex(R.RuntimeUnavailable,
                                    '^laya_embed_runtime_download_refused$'):
            R.download('http://example.invalid/model.gguf', self.base / 'x')
        self.assertEqual(R.MODEL_SHA256,
                         'f470220f84b6235197541352d22f10bf00098a8242c18eaacea9c8a4add557bc')


class NodeDiscovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='lnd-')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.versions = {}

    def node(self, path, version, raw=MACHO):
        node = write(self.home / path, raw, 0o755)
        self.versions[str(node.resolve())] = version
        return node

    def runner(self, argv, **kw):
        # Binaries outside the temporary HOME (system installs) never answer.
        if argv[0] not in self.versions:
            return Result(returncode=1)
        return Result(stdout=self.versions[argv[0]] + '\n')

    def discover(self, path_env=''):
        return R.discover_node(path_env=path_env, home=self.home, run=self.runner)

    def test_path_node_wins_when_supported(self):
        on_path = self.node('bin/node', 'v22.11.0')
        self.node('.local/share/fnm/node-versions/v24.14.0/installation/bin/node', 'v24.14.0')
        self.assertEqual(self.discover(str(on_path.parent)), (on_path.resolve(), '22.11.0'))

    def test_manager_fallback_gates_major_and_ignores_aliases(self):
        old = self.node('bin/node', 'v20.18.0')
        self.node('.local/share/mise/installs/node/latest/bin/node', 'v24.0.0')
        self.node('.nvm/versions/node/v26.1.0/bin/node', 'v26.1.0')
        self.node('.nvm/versions/node/v24.14.0/bin/node', 'v24.14.0')
        self.node('.local/share/fnm/node-versions/v23.9.0/installation/bin/node', 'v23.9.0')
        found = self.discover(str(old.parent))
        self.assertEqual(found[1], '26.1.0')

    def test_version_manager_shim_scripts_are_not_node(self):
        shim = self.node('shims/node', 'v24.14.0', raw=b'#!/bin/sh\nexec real-node "$@"\n')
        with mock.patch.object(R, '_manager_nodes', return_value=[]):
            with self.assertRaisesRegex(R.RuntimeUnavailable,
                                        '^laya_embed_runtime_node_unavailable$'):
                self.discover(str(shim.parent))


class GenerationGc(Machine):
    def generations(self):
        return sorted(p.name for p in (R.root() / 'generations').iterdir())

    def test_gc_keeps_active_previous_and_attested(self):
        first = Path(self.provision()['generation']).name
        self.model_bytes += b'2'
        second = Path(self.provision()['generation']).name
        self.model_bytes += b'3'
        third = Path(self.provision()['generation']).name
        self.model_bytes += b'4'
        fourth = Path(self.provision()['generation']).name
        self.assertEqual(len({first, second, third, fourth}), 4)
        # An install attestation still names the first generation.
        install = self.home / '.local' / 'state' / 'laya' / ('a' * 32)
        write(install / 'production-provider-attestation.json', json.dumps({
            'providerManifest': {'modelPath': str(R.root() / 'generations' / first /
                                                  'models' / R.MODEL_NAME)},
            'profileDependencyClosure': [{'path': str(R.root() / 'generations' / second /
                                                      'x.node')}]}))
        stale = R.root() / 'generations' / '.staging-dead'
        stale.mkdir()
        plan = R.collect()
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(plan['changesApplied'], 0)
        self.assertEqual(len(self.generations()), 5)
        actions = {row['generation']: row for row in plan['generations']}
        self.assertEqual(actions[fourth]['reason'], 'active')
        self.assertEqual(actions[third]['reason'], 'previous')
        self.assertEqual(actions[first]['reason'], 'attested')
        # The historical profile row is data, not a live reference.
        self.assertEqual(actions[second]['action'], 'remove')
        done = R.collect(approve=True)
        self.assertEqual(done['changesApplied'], 2)
        self.assertEqual(self.generations(), sorted([first, third, fourth]))
        self.assertEqual(Path(R.select()['generation']).name, fourth)

    def test_symlinked_generations_never_deletes_outside_the_runtime(self):
        self.provision()
        self.model_bytes += b'2'
        self.provision()
        self.model_bytes += b'3'
        self.provision()
        generations = R.root() / 'generations'
        outside = self.base / 'outside-generations'
        generations.rename(outside)
        generations.symlink_to(outside)
        (outside / '.staging-dead').mkdir()
        before = sorted(p.name for p in outside.iterdir())
        self.assertEqual(len(before), 4)
        with self.assertRaisesRegex(R.RuntimeUnavailable, '^laya_embed_runtime_unsafe$'):
            R.collect(approve=True)
        with self.assertRaisesRegex(R.RuntimeUnavailable, '^laya_embed_runtime_unsafe$'):
            R.collect()
        self.assertEqual(sorted(p.name for p in outside.iterdir()), before)

    def test_link_inside_a_removed_generation_is_not_followed(self):
        first = Path(self.provision()['generation'])
        for bump in (b'2', b'3'):
            self.model_bytes += bump
            self.provision()
        keep = self.base / 'keep-me'
        write(keep / 'data', 'outside')
        (first / 'escape').symlink_to(keep)
        done = R.collect(approve=True)
        self.assertEqual(done['changesApplied'], 1)
        self.assertFalse(first.exists())
        self.assertEqual((keep / 'data').read_text(), 'outside')

    def three_generations(self):
        first = Path(self.provision()['generation'])
        for bump in (b'2', b'3'):
            self.model_bytes += bump
            self.provision()
        stale = R.root() / 'generations' / '.staging-dead'
        stale.mkdir()
        return first, stale

    def test_failed_generation_keeps_the_removed_ones_reported(self):
        first, stale = self.three_generations()
        locked_dir(self, first / 'locked')
        done = R.collect(approve=True)
        self.assertEqual((done['status'], done['partial'], done['changesApplied']),
                         ('partial', True, 1))
        self.assertEqual(done['failed'], [{'generation': first.name,
                                           'reason': 'laya_embed_runtime_remove_failed'}])
        self.assertFalse(stale.exists())

    def test_mount_point_inside_a_generation_is_refused_untouched(self):
        first, stale = self.three_generations()
        write(first / 'mnt' / 'volume-data', 'mounted')
        with mock.patch.object(safe_rmtree, '_mount_points', create=True,
                               return_value={str(first / 'mnt')}):
            done = R.collect(approve=True)
        self.assertEqual(done['status'], 'partial')
        self.assertEqual(done['failed'], [{'generation': first.name,
                                           'reason': 'laya_embed_runtime_mount_point'}])
        self.assertTrue((first / 'mnt' / 'volume-data').exists())
        self.assertTrue((first / 'runtime.json').exists())
        self.assertFalse(stale.exists())

    def test_unreadable_attestation_blocks_gc(self):
        self.provision()
        install = self.home / '.local' / 'state' / 'laya' / ('b' * 32)
        write(install / 'production-provider-attestation.json', '{')
        self.assertEqual(R.collect(approve=True)['status'], 'blocked')


class SetupEntryActions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='lse-')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'QMD_SETUP_GUARD_FIXTURE': '1'})
        env.start()
        self.addCleanup(env.stop)

    def main(self, *argv):
        import contextlib
        import io
        import setup_entry
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup_entry.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_runtime_actions_need_their_approval(self):
        code, refused = self.main('provision-index-runtime')
        self.assertEqual((code, refused['reason']),
                         (1, 'laya_embed_runtime_download_approval_required'))
        code, missing = self.main('gc-index-runtime')
        self.assertEqual((code, missing['reason']), (1, 'laya_embed_runtime_missing'))


if __name__ == '__main__':
    unittest.main()
