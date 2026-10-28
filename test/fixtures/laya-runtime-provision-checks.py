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
import legacy_cleanup as C  # noqa: E402

MACHO = b'\xcf\xfa\xed\xfe' + b'synthetic node binary'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


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



class Provisioning(Machine):
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

    def test_unreadable_attestation_blocks_gc(self):
        self.provision()
        install = self.home / '.local' / 'state' / 'laya' / ('b' * 32)
        write(install / 'production-provider-attestation.json', '{')
        self.assertEqual(R.collect(approve=True)['status'], 'blocked')


class LegacyCleanup(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='llc-')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        support = self.home / 'Library' / 'Application Support' / 'qmd-auto-context'
        self.runtime = support / 'runtimes' / 'qmd'
        self.entry = self.runtime / 'generations' / ('g-' + 'e' * 32) / 'qmd-native.mjs'
        write(self.entry, 'qmd')
        write(support / 'shared-qmd' / 'store.sqlite', 'store')
        self.backend = support / 'backend'
        write(self.backend / 'daemon-8584.pid', '4242\n')
        write(self.backend / 'daemon-8484.pid', '5151\n')
        write(self.backend / 'daemon.log', 'log')
        write(support / 'learning' / 'keep.json', '{}')
        write(support / 'runtimes' / 'laya' / 'keep', 'laya')
        write(self.home / '.cache' / 'qmd' / 'index.sqlite', 'user index')
        write(self.home / '.config' / 'qmd' / 'index.yml', 'collections: {}')
        agents = self.home / 'Library' / 'LaunchAgents'
        write(agents / 'com.qmd-mcp-daemon.plist', '<!-- managed-by: qmd-auto-context -->')
        write(agents / 'com.qmd-keepalive.plist', 'someone else')
        write(agents / 'com.qmd-logrotate.plist.bak-20260616', 'backup')
        self.processes = {
            4242: (os.getuid(), '/opt/node/bin/node ' + str(self.entry) +
                   ' mcp --http --port 8584'),
            5151: (os.getuid(), '/usr/bin/vim notes.txt'),
        }
        self.killed, self.launchctl = [], []

    def ps(self, pid):
        return self.processes.get(pid)

    def kill(self, pid, sig):
        self.killed.append((pid, sig))
        self.processes.pop(pid, None)

    def ps_all(self):
        return [(pid, uid, command) for pid, (uid, command) in self.processes.items()]

    def apply(self, **options):
        return C.apply(self.home, ps=self.ps, ps_all=self.ps_all, kill=self.kill,
                       launchctl=self.launchctl.append, sleep=lambda _s: None, **options)

    def plan(self, **options):
        return C.plan(self.home, ps=self.ps, ps_all=self.ps_all, **options)

    def test_plan_is_read_only_and_classifies_everything(self):
        before = sorted(str(p) for p in self.home.rglob('*'))
        plan = self.plan()
        self.assertEqual(sorted(str(p) for p in self.home.rglob('*')), before)
        states = {row['port']: row['state'] for row in plan['daemons']}
        self.assertEqual(states, {8584: 'running_verified', 8484: 'identity_mismatch'})
        agents = {row['label']: row['state'] for row in plan['launchAgents']}
        self.assertEqual(agents['com.qmd-mcp-daemon'], 'managed')
        self.assertEqual(agents['com.qmd-keepalive'], 'unmanaged_untouched')
        self.assertEqual(agents['com.qmd-logrotate.plist.bak-20260616'], 'backup_untouched')
        actions = {row['category']: row['action'] for row in plan['paths']}
        self.assertEqual(actions['plugin_qmd_runtime'], 'remove')
        self.assertEqual(actions['user_qmd_cache'], 'kept_needs_user_data_approval')
        self.assertEqual(plan['blockers'], [])

    def test_apply_needs_approval(self):
        with self.assertRaisesRegex(C.CleanupBlocked, '^legacy_cleanup_approval_required$'):
            self.apply()
        self.assertEqual(self.killed, [])

    def test_apply_stops_only_the_verified_daemon_and_removes_plugin_trees(self):
        result = self.apply(approved=True)
        self.assertEqual(result['status'], 'cleaned')
        self.assertEqual(self.killed, [(4242, signal.SIGTERM)])
        self.assertIn(5151, self.processes)
        self.assertEqual([p.name for p in self.launchctl], ['com.qmd-mcp-daemon.plist'])
        agents = self.home / 'Library' / 'LaunchAgents'
        self.assertEqual(sorted(p.name for p in agents.iterdir()),
                         ['com.qmd-keepalive.plist', 'com.qmd-logrotate.plist.bak-20260616'])
        support = self.home / 'Library' / 'Application Support' / 'qmd-auto-context'
        self.assertEqual(sorted(p.name for p in support.iterdir()), ['learning', 'runtimes'])
        self.assertEqual(sorted(p.name for p in (support / 'runtimes').iterdir()), ['laya'])
        # User QMD data stays without its own approval.
        self.assertTrue((self.home / '.cache' / 'qmd' / 'index.sqlite').exists())
        self.assertTrue((self.home / '.config' / 'qmd' / 'index.yml').exists())
        again = self.apply(approved=True, include_user_data=True)
        self.assertEqual(again['status'], 'cleaned')
        self.assertFalse((self.home / '.cache' / 'qmd').exists())
        self.assertFalse((self.home / '.config' / 'qmd').exists())

    def test_identity_change_before_signal_is_not_killed(self):
        real = self.ps

        def changing(pid):
            seen = real(pid)
            if pid == 4242 and getattr(self, 'checked', False):
                return (os.getuid(), '/usr/bin/other')
            if pid == 4242:
                self.checked = True
            return seen
        result = C.apply(self.home, ps=changing, ps_all=self.ps_all, kill=self.kill,
                         launchctl=self.launchctl.append, sleep=lambda _s: None, approved=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('qmd_daemon_identity_changed', result['blockers'])
        self.assertEqual(self.killed, [])
        self.assertTrue(self.runtime.exists())

    def test_daemon_that_survives_sigterm_keeps_its_trees(self):
        clock = iter(range(0, 100))
        result = C.apply(self.home, ps=self.ps, ps_all=self.ps_all,
                         kill=lambda pid, sig: self.killed.append(pid),
                         launchctl=self.launchctl.append, sleep=lambda _s: None,
                         clock=lambda: next(clock), approved=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('qmd_daemon_stop_timeout', result['blockers'])
        self.assertTrue(self.runtime.exists())
        self.assertEqual(self.launchctl, [])

    def test_attestation_naming_a_target_blocks_everything(self):
        install = self.home / '.local' / 'state' / 'laya' / ('c' * 32)
        write(install / 'production-provider-attestation.json', json.dumps({
            'providerManifest': {'modelPath': str(self.home / '.cache' / 'qmd' / 'models' / 'm')},
            'profileDependencyClosure': [{'path': str(self.entry)}]}))
        result = self.apply(approved=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['blockers'], ['attestation_references_legacy_path'])
        self.assertEqual(self.killed, [])
        self.assertTrue(self.runtime.exists())

    def test_install_bound_to_an_older_bundle_blocks_everything(self):
        old_bundle = self.home / 'plugins' / '0.39.1'
        write(old_bundle / 'core' / 'backend_manager.sh', '#!/bin/sh\n')
        install = self.home / '.local' / 'state' / 'laya' / ('e' * 32)
        write(install / 'reviewed-route.json', json.dumps({'pluginRoot': str(old_bundle)}))
        result = self.apply(approved=True)
        self.assertEqual(result['blockers'], ['older_host_bundle_bound'])
        self.assertEqual(self.killed, [])
        (old_bundle / 'core' / 'backend_manager.sh').unlink()
        self.assertEqual(self.plan()['blockers'], [])

    def test_profile_closure_rows_alone_do_not_block(self):
        install = self.home / '.local' / 'state' / 'laya' / ('d' * 32)
        write(install / 'production-provider-attestation.json', json.dumps({
            'providerManifest': {'modelPath': '/elsewhere/m.gguf'},
            'profileDependencyClosure': [{'path': str(self.entry)}]}))
        self.assertEqual(self.plan()['blockers'], [])

    def test_fixture_runs_refuse_the_account_home(self):
        with mock.patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '1'}), \
                mock.patch.object(C, '_account_home', return_value=self.home):
            with self.assertRaisesRegex(C.CleanupBlocked, '^legacy_cleanup_fixture_real_home$'):
                self.plan()

    def outside_runtimes(self):
        """Move ``runtimes`` outside HOME and leave a link in its place."""
        tmp = tempfile.TemporaryDirectory(prefix='llc-out-')
        self.addCleanup(tmp.cleanup)
        outside = Path(tmp.name).resolve() / 'runtimes'
        runtimes = self.runtime.parent
        runtimes.rename(outside)
        runtimes.symlink_to(outside)
        return outside

    def test_symlinked_ancestor_blocks_and_keeps_the_outside_tree(self):
        outside = self.outside_runtimes()
        plan = self.plan()
        actions = {row['category']: row['action'] for row in plan['paths']}
        self.assertEqual(actions['plugin_qmd_runtime'], 'refused_unsafe_path')
        result = self.apply(approved=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('legacy_cleanup_target_unsafe', result['blockers'])
        self.assertEqual(self.killed, [])
        self.assertTrue((outside / 'qmd' / self.entry.relative_to(self.runtime)).exists())
        self.assertTrue((outside / 'laya' / 'keep').exists())

    def test_ancestor_swapped_after_plan_is_refused_at_removal(self):
        support = self.runtime.parents[1]
        plan = self.plan()
        self.assertEqual(plan['blockers'], [])
        outside = self.outside_runtimes()
        with self.assertRaisesRegex(C.CleanupBlocked, '^legacy_cleanup_target_unsafe$'):
            C._remove_tree(self.runtime, self.home)
        self.assertTrue((outside / 'qmd' / self.entry.relative_to(self.runtime)).exists())
        # A link inside a removed tree is unlinked, never followed.
        keep = Path(tempfile.mkdtemp(prefix='llc-keep-'))
        self.addCleanup(shutil.rmtree, keep, True)
        write(keep / 'data', 'outside')
        (support / 'shared-qmd' / 'escape').symlink_to(keep)
        C._remove_tree(support / 'shared-qmd', self.home)
        self.assertFalse((support / 'shared-qmd').exists())
        self.assertEqual((keep / 'data').read_text(), 'outside')

    def test_portless_legacy_pid_file_stops_the_verified_daemon(self):
        write(self.backend / 'daemon.pid', '6161\n')
        self.processes[6161] = (os.getuid(), '/opt/node/bin/node ' + str(self.entry) +
                                ' mcp --http --port 8181')
        plan = self.plan()
        legacy = next(row for row in plan['daemons'] if row['pidFile'].endswith('/daemon.pid'))
        self.assertEqual((legacy['state'], legacy['port']), ('running_verified', 8181))
        result = self.apply(approved=True)
        self.assertEqual(result['status'], 'cleaned')
        self.assertIn((6161, signal.SIGTERM), self.killed)
        self.assertNotIn(6161, self.processes)
        self.assertFalse(self.runtime.exists())

    def test_unverified_daemon_using_the_runtime_blocks_deletion(self):
        write(self.backend / 'daemon.pid', '7171\n')
        # Names the runtime but is not the exact daemon argv: never signalled.
        self.processes[7171] = (os.getuid(), '/opt/node/bin/node ' + str(self.entry) +
                                ' mcp --http')
        result = self.apply(approved=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('qmd_daemon_unverified', result['blockers'])
        self.assertEqual(self.killed, [])
        self.assertTrue(self.runtime.exists())
        self.assertEqual(self.launchctl, [])

    def test_runtime_process_without_pid_file_blocks_deletion(self):
        self.processes[8181] = (os.getuid() + 1, '/opt/node/bin/node ' + str(self.entry) +
                                ' mcp --http --port 8585')
        result = self.apply(approved=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['blockers'], ['qmd_daemon_unverified'])
        self.assertEqual([row['pid'] for row in result['unverifiedProcesses']], [8181])
        self.assertEqual(self.killed, [])
        self.assertTrue(self.runtime.exists())


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

    def test_cleanup_plan_and_refused_apply(self):
        code, plan = self.main('plan-legacy-cleanup')
        self.assertEqual((code, plan['changesApplied']), (0, 0))
        self.assertEqual(plan['home'], str(self.home))
        code, refused = self.main('apply-legacy-cleanup')
        self.assertEqual((code, refused['reason']), (1, 'legacy_cleanup_approval_required'))

    def test_runtime_actions_need_their_approval(self):
        code, refused = self.main('provision-index-runtime')
        self.assertEqual((code, refused['reason']),
                         (1, 'laya_embed_runtime_download_approval_required'))
        code, missing = self.main('gc-index-runtime')
        self.assertEqual((code, missing['reason']), (1, 'laya_embed_runtime_missing'))


if __name__ == '__main__':
    unittest.main()
