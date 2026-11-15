"""Supported node discovery rejects obsolete versions and shell shims."""
from pathlib import Path
from unittest import mock

from memvane.provider import embed_runtime as R
from tests.support.case import FixtureCase

MACHO = b"\xcf\xfa\xed\xfe" + b"synthetic node binary"


def write(path, raw, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    if mode is not None:
        path.chmod(mode)
    return path


class Result:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class NodeDiscovery(FixtureCase):
    def setUp(self):
        super().setUp()
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
