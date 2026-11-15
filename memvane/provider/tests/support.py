"""Synthetic registry and node for provider runtime provisioning tests."""
import hashlib
import json
from pathlib import Path
import shutil
from unittest import mock

from memvane.provider import embed_runtime as R
from tests.support.case import FixtureCase

MACHO = b"\xcf\xfa\xed\xfe" + b"synthetic node binary"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Result:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


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


class Machine(FixtureCase):
    def setUp(self):
        super().setUp()
        self.base = self.home
        self.qmd_runtime = self.home / 'unused-qmd-runtime'
        self.registry = self.home / 'registry'
        top = self.registry
        module = top / 'node-llama-cpp'
        package(module, '3.18.1', deps={'lifecycle-utils': '^3', 'which': '^6'},
                optional={'@node-llama-cpp/mac-arm64-metal': '3.18.1'})
        write(module / 'dist/index.js', 'export const getLlama = () => null;')
        write(module / 'llama/binariesGithubRelease.json', '{"release":"b1"}')
        package(module / 'node_modules/which', '6.0.1', deps={'isexe': '^3'})
        write(module / 'node_modules/which/lib/index.js', 'which')
        package(top / 'lifecycle-utils', '3.1.1')
        write(top / 'lifecycle-utils/dist/index.js', 'lifecycle')
        package(top / 'isexe', '3.1.1')
        write(top / 'isexe/index.js', 'isexe')
        native = top / '@node-llama-cpp/mac-arm64-metal'
        package(native, '3.18.1')
        for name, raw in (('llama-addon.node', b'addon'),
                          ('libggml-base.dylib', b'ggml'),
                          ('libggml-cpu.so', b'cpu')):
            write(native / 'bins/mac-arm64-metal' / name, raw * 50)
        self.node = write(self.home / '.local/share/fnm/node-versions/v24.14.0/installation/bin/node',
                          MACHO, 0o755)
        self.npm_cli = write(self.node.parents[1] / 'lib/node_modules/npm/bin/npm-cli.js', '// npm')
        self.model_bytes = b'GGUF' + b'synthetic downloaded model' * 30
        self.runs, self.fetches = [], []

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
                path for path in manager_nodes(home) if (path == self.home or self.home in path.parents)]):
            return R.provision_new(approve_download=True, home=self.home,
                                   path_env=path_env, run=run or self.npm,
                                   fetch=self.fetch, model_sha256=sha(self.model_bytes))
