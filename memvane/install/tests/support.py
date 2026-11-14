"""Install domain test helpers: a fixture project, synthetic pointers and refusal checks."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.support.case import FixtureCase
from tests.support.install_builder import Install, fixture_provider
from tests.support.project import make_project

INSTALL_ID = '4' * 32
OTHER_INSTALL = '5' * 32
MISSING_INSTALL = 'a' * 32


def canonical(record):
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class InstallCase(FixtureCase):
    """A committed fixture project with one raw note; installs are made on demand."""

    def setUp(self):
        super().setUp()
        self.fixture = make_project(self.home, name='fixture')
        self.project = self.fixture.root
        self.folder = self.project / '.memvane'
        self.fixture.write_note('notes.md', 'Field notes: the emerald heron nests beside the reed pond.\n')
        self.fixture.commit('fixture')

    # -- installs ------------------------------------------------------------------
    def install(self, install_id=INSTALL_ID, fixture=None):
        """An ``Install`` of the fixture project (no step run yet)."""
        return Install(self, fixture or self.fixture, install_id, fixture_provider(self))

    # -- refusals --------------------------------------------------------------------
    def blocked(self, reason, step, *args, **kwargs):
        from memvane.install.types import InstallBlocked
        with self.assertRaises(InstallBlocked) as caught:
            step(*args, **kwargs)
        self.assertEqual(str(caught.exception), reason)

    # -- project pointers ------------------------------------------------------------
    def write_pointer(self, name, record, project=None):
        """Write one project pointer (owner-only, as the installer writes it); returns its bytes."""
        raw = canonical(record) if isinstance(record, dict) else record
        path = (project or self.project) / '.memvane' / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
        return raw

    def pointers(self, project=None):
        folder = (project or self.project) / '.memvane'
        return {name: (folder / name).read_bytes() for name in ('route.json', 'selection.json')
                if os.path.lexists(folder / name)}

    def foreign_route(self, install_id=MISSING_INSTALL, **overrides):
        """A 1.x route of an install whose state root does not exist on this machine."""
        from memvane.kernel.memvane_paths import install_root
        state = install_root(install_id)
        record = {'schema': 'memvane-route-v4', 'status': 'active', 'projectRoot': str(self.project),
                  'settingsSha256': '0' * 64, 'stateRoot': str(state), 'stateNode': [1, 2],
                  'storePath': str(state / 'store.sqlite'), 'storeNode': [1, 3],
                  'storeIncarnation': 'b' * 32, 'installId': install_id, 'scopeId': 'c' * 32,
                  'stateFormat': 1, 'collections': [], 'vectorProfile': {}}
        record.update(overrides)
        return record

    def marker(self, install_id=MISSING_INSTALL, route_raw=b'', **overrides):
        import hashlib
        record = {'schema': 'memvane-selection-v1', 'projectRoot': str(self.project),
                  'installId': install_id, 'routeSha256': hashlib.sha256(route_raw).hexdigest()}
        record.update(overrides)
        return record

    def write_foreign_pair(self, install_id=MISSING_INSTALL):
        """Route and marker of one missing install; returns ``{name: bytes}``."""
        route = self.write_pointer('route.json', self.foreign_route(install_id))
        selection = self.write_pointer('selection.json', self.marker(install_id, route))
        return {'route.json': route, 'selection.json': selection}


def state_files(state):
    """Names present in an install state root (empty when it does not exist)."""
    state = Path(state)
    return sorted(path.name for path in state.iterdir()) if state.is_dir() else []
