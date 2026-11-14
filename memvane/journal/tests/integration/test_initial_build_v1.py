"""journal.initial_build keeps only the 1.0 build record and its pointer."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from memvane.journal import initial_build as build
from memvane.journal.build_record import _canon
from memvane.journal.types import V1_BUILD_FILE, V1_BUILD_SCHEMA
from memvane.kernel.errors import IndexContractError

REMOVED = ('SCHEMA', 'RECORD', 'PHASES', 'IDENTITY_FIELDS', 'RECORD_FIELDS', 'SEALED_FIELDS',
           'create_or_verify', 'read', 'record_path', 'advance', 'transition',
           'request_freshness', 'freshness_satisfied')


class InitialBuildV1(unittest.TestCase):
    def setUp(self):
        self.home = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvb-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = self.home / 'project'
        self.project.mkdir()
        self.state = self.home / 'state'
        self.state.mkdir(mode=0o700)
        self.identity = {
            'projectRoot': str(self.project), 'stateRoot': str(self.state), 'installId': '1' * 32,
            'scopeId': '2' * 32, 'storePath': str(self.state / 'store.sqlite'), 'storeNode': [1, 2],
            'storeIncarnation': '3' * 32, 'journalPath': str(self.state / 'journal.sqlite'),
            'routeSha256': '4' * 64, 'attestationSha256': '5' * 64}
        self.write(self.state / V1_BUILD_FILE, {
            'schema': V1_BUILD_SCHEMA, 'epoch': 1, 'phase': 'registered',
            'identity': self.identity, 'reason': None, 'updatedNs': 1})

    def write(self, path, value):
        path.write_bytes(_canon(value))
        path.chmod(0o600)

    def pointers(self):
        directory = self.home.joinpath('.local', 'state', 'memvane', 'v1', build.POINTERS)
        return sorted(directory.iterdir()) if directory.exists() else []

    def refused(self, reason, call, *args):
        with self.assertRaises(IndexContractError) as caught:
            call(*args)
        self.assertEqual(str(caught.exception), reason)

    def test_no_zero_x_record_path_remains(self):
        self.assertEqual([name for name in REMOVED if hasattr(build, name)], [])

    def test_pointer_carries_the_memvane_schema_and_resolves(self):
        path = Path(build.register_project(self.identity))
        self.assertEqual(self.pointers(), [path])
        pointer = json.loads(path.read_text())
        self.assertEqual(pointer, {'schema': 'memvane-initial-build-pointer-v1',
                                   'projectRoot': str(self.project), 'stateRoot': str(self.state),
                                   'installId': '1' * 32})
        self.assertEqual(build.lookup(str(self.project))['identity'], self.identity)
        self.assertEqual(build.register_project(self.identity), str(path))
        self.assertEqual(len(self.pointers()), 1)

    def test_old_pointer_id_and_zero_x_record_are_refused(self):
        path = Path(build.register_project(self.identity))
        self.write(path, {**json.loads(path.read_text()), 'schema': 'laya-initial-build-pointer-v1'})
        self.refused('initial_build_pointer_invalid', build.lookup, str(self.project))
        self.refused('initial_build_already_registered', build.register_project, self.identity)
        path.unlink()
        # A 0.x record without a 1.0 build.json is never read.
        (self.state / V1_BUILD_FILE).rename(self.state / 'initial-build.json')
        self.write(path, {'schema': 'memvane-initial-build-pointer-v1', 'projectRoot': str(self.project),
                          'stateRoot': str(self.state), 'installId': '1' * 32})
        self.refused('initial_build_pointer_invalid', build.lookup, str(self.project))

    def test_zero_x_identity_is_refused(self):
        legacy = {key: self.identity.get(key, 'x') for key in (
            'projectRoot', 'stateRoot', 'installId', 'scopeId', 'collections', 'candidateDb',
            'queueDb', 'catalogDb', 'bindingStore', 'dbIncarnation', 'journalPath', 'routeFile',
            'routeSha256', 'attestationSha256', 'vectorProfileId', 'proposalSha256',
            'providerProposalSha256', 'originMode')}
        self.refused('initial_build_identity_invalid', build.register_project, legacy)
        self.refused('initial_build_identity_invalid', build.retire_project_pointer, legacy)
        self.assertEqual(self.pointers(), [])

    def test_retire_archives_the_memvane_pointer_and_reruns(self):
        build.register_project(self.identity)
        self.assertEqual(build.retire_project_pointer(self.identity), 'retired')
        self.assertEqual(self.pointers(), [])
        archive = json.loads((self.state / build.RETIRED_POINTER).read_text())
        self.assertEqual(archive['schema'], 'memvane-initial-build-pointer-v1')
        self.assertEqual(build.retire_project_pointer(self.identity), 'absent')
        self.assertIsNone(build.lookup(str(self.project)))


if __name__ == '__main__':
    unittest.main()
