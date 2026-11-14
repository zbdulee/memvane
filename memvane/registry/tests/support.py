"""Registry test helpers: selected installs under the case HOME and registry row access."""
from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path
import sqlite3

from memvane.registry import memvane_registry as registry
from tests.support.case import FixtureCase

DAY = 24 * 3600


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Selected:
    """One selected install as the registry sees it: project root, state root, install id."""

    def __init__(self, install):
        from memvane.journal.api import inspect_journal
        self.install = install
        self.fixture = install.project
        self.project = install.project.root
        self.state = install.state
        self.install_id = install.install_id
        self.journal = install.state / 'journal.sqlite'
        self.scope_id = inspect_journal(self.journal)['scopeId']


class RegistryCase(FixtureCase):
    """Selected installs ``self.a``, ``self.b`` (built on first use) and no registry file yet."""

    installs = 1

    def setUp(self):
        super().setUp()
        self._built = []
        for _ in range(self.installs):
            self.selected_install(len(self._built))

    def selected_install(self, index):
        """The ``index``-th selected install (``projectN``, install id ``N+4`` repeated)."""
        from tests.support.install_builder import build_install
        from tests.support.project import make_project
        while len(self._built) <= index:
            n = len(self._built)
            project = make_project(self.home, name=f'fixture{n}', dirname=f'project{n}')
            project.write_note('notes.md', f'Field notes of project {n}.\n')
            project.commit()
            self._built.append(Selected(build_install(self, project, install_id='%x' % (n + 4) * 32)))
        return self._built[index]

    @property
    def selected(self):
        return list(self._built)

    @property
    def a(self):
        return self.selected_install(0)

    @property
    def b(self):
        return self.selected_install(1)

    @property
    def db(self):
        return registry.registry_path()

    def sql(self, statement, args=()):
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute(statement, args)
            db.commit()

    def insert(self, install_id, state_root, project_root, target_root, *, kind='main', status='active',
               parent=None):
        self.sql("INSERT INTO entries(install_id,state_root,project_root,target_root,kind,parent_install,"
                 "status,observed_at,version) VALUES (?,?,?,?,?,?,?,1,1)",
                 (install_id, str(state_root), str(project_root), str(target_root), kind, parent, status))

    def rows(self):
        if not self.db.exists():
            return {}
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.row_factory = sqlite3.Row
            return {(row['install_id'], row['target_root']): dict(row)
                    for row in db.execute('SELECT * FROM entries ORDER BY id')}

    def row(self, selected, target=None):
        target = str(target or selected.project)
        found = [row for (iid, root), row in self.rows().items()
                 if iid == selected.install_id and root == target]
        return found[0] if found else None

    def register(self, selected, explicit=True, now=1000.0):
        return registry.register(str(selected.project), str(selected.state), explicit=explicit, now=now)

    def backfill(self, now=1000.0):
        result = None
        for _ in range(8):
            result = registry.reconcile(now=now, max_rows=64, seconds=60.0, backfill_page=64)
            if result['complete']:
                return result
        self.fail(f'backfill did not complete: {result}')
