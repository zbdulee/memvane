"""Provisioning seals a synthetic runtime without network access."""
import json
from pathlib import Path
from unittest import mock

from memvane.provider import embed_runtime as R
from memvane.provider.tests.support import MACHO, Machine, Result, sha, write


class NewMachineProvisioning(Machine):
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
