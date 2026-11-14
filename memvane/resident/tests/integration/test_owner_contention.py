"""A busy sync owner does not invalidate a live resident binding."""
from types import SimpleNamespace
from unittest.mock import Mock

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.resident import embed_resident, resident_service
from tests.support.case import FixtureCase


class OwnerContention(FixtureCase):
    def test_owner_busy_keeps_live_binding(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        def busy(_project):
            raise ResourceDeferred('owner_busy')
        service = resident_service.Service(root, bundle={'version': '1.0.0'}, prove=busy)
        manifest = {key: 'synthetic' for key in embed_resident._IDENTITY}
        manifest.update(modelId='model', dimension=4)
        binding = SimpleNamespace(manifest=manifest, recheck_at=0, closed=False)
        project = '/synthetic/project'
        service.bindings[project] = binding
        service.workers[resident_service._worker_key(manifest)] = SimpleNamespace(alive=lambda: True)
        service._release = Mock()
        service._check_owned(project)
        self.assertIs(service.bindings[project], binding)
        self.assertFalse(binding.closed)
        service._release.assert_not_called()
