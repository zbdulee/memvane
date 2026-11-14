"""store.api.read_header: one snapshot of the store header and worktree rows."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from memvane.kernel.errors import _query_only_uri
from memvane.store import api as store_api
from memvane.store.types import STORE_HEADER_KEYS, STORE_WORKTREE_KEYS

_READONLY_ROLLBACK = getattr(sqlite3, 'SQLITE_READONLY_ROLLBACK', 776)
_HOT_JOURNAL = r'''
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute('PRAGMA cache_size=1')
db.execute('BEGIN IMMEDIATE')
db.execute('CREATE TABLE killed_writer(x BLOB)')
for _ in range(200):
    db.execute('INSERT INTO killed_writer VALUES (zeroblob(8192))')
os._exit(0)
'''


def make_store(base):
    """A private state dir holding a created store over a one-commit git checkout with settings."""
    project, state = base / 'project', base / 'state'
    project.mkdir()
    state.mkdir(mode=0o700)
    git = ['git', '-C', str(project), '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
           '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
    subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(project)],
                   check=True, capture_output=True, timeout=10)
    (project / '.memvane' / 'wiki').mkdir(parents=True)
    (project / '.memvane' / 'settings.json').write_text('{"name": "fixture"}\n')
    subprocess.run(git + ['add', '-A'], check=True, capture_output=True, timeout=10)
    subprocess.run(git + ['commit', '-qm', 'fixture'],
                   check=True, capture_output=True, timeout=10)
    path = state / 'store.sqlite'
    binding = store_api.observe_binding(path, scope_id='5' * 32, create_root=str(project))
    return project, state, path, binding


def leave_hot_journal(path):
    """A writer killed mid-transaction: its ``-journal`` stays and the store is hot."""
    subprocess.run([sys.executable, '-B', '-c', _HOT_JOURNAL, str(path)], check=True, timeout=60)
    assert os.path.lexists(str(path) + '-journal')


class ReadHeader(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvs-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.project, self.state, self.path, self.binding = make_store(self.base)
        self.worktree_id = self.binding['saved']['worktree_id']

    def test_header_by_worktree_and_by_project(self):
        header = store_api.read_header(self.path, worktree_id=self.worktree_id,
                                       project_root=str(self.project))
        self.assertEqual(tuple(header), STORE_HEADER_KEYS)
        self.assertEqual((header['incarnation'], header['scope_id'], header['settings_policy_digest']),
                         (self.binding['incarnation'], '5' * 32, self.binding['policy_digest']))
        self.assertIsInstance(header['repository_json'], str)
        self.assertEqual(tuple(header['worktree']), STORE_WORKTREE_KEYS)
        self.assertEqual((header['worktree']['project_root'], header['worktree']['binding_digest']),
                         (str(self.project), self.binding['saved']['binding_digest']))
        self.assertEqual(len(header['worktrees']), 1)
        self.assertEqual(header['worktrees'][0], header['worktree'])

    def test_missing_worktree_and_wrong_project_are_empty(self):
        header = store_api.read_header(self.path, worktree_id='0' * 32,
                                       project_root=str(self.base / 'elsewhere'))
        self.assertIsNone(header['worktree'])
        self.assertEqual(header['worktrees'], ())
        plain = store_api.read_header(self.path)
        self.assertEqual((plain['worktree'], plain['worktrees']), (None, ()))
        self.assertEqual(plain['incarnation'], self.binding['incarnation'])

    def test_no_header_row_is_none(self):
        empty = self.state / 'empty.sqlite'
        with closing(sqlite3.connect(empty, isolation_level=None)) as db:
            db.execute('CREATE TABLE store_meta(singleton INTEGER PRIMARY KEY, incarnation TEXT, '
                       'scope_id TEXT, repository_json TEXT, settings_policy_digest TEXT)')
            db.execute('CREATE TABLE worktrees(worktree_id TEXT, project_root TEXT, '
                       'binding_digest TEXT, record_json TEXT)')
        self.assertIsNone(store_api.read_header(empty, project_root=str(self.project)))

    def test_hot_journal_refused_without_recover_and_rolled_back_with_it(self):
        leave_hot_journal(self.path)
        if _query_only_uri(self.path).endswith('?mode=ro'):
            with self.assertRaises(sqlite3.Error) as caught:
                store_api.read_header(self.path)
            self.assertEqual(getattr(caught.exception, 'sqlite_errorcode', None), _READONLY_ROLLBACK)
            self.assertTrue(os.path.lexists(str(self.path) + '-journal'))
        header = store_api.read_header(self.path, worktree_id=self.worktree_id, recover=True)
        self.assertEqual(header['incarnation'], self.binding['incarnation'])
        self.assertEqual(header['worktree']['project_root'], str(self.project))
        # Rolled back: a mode=ro open no longer finds a hot journal (Apple's
        # libsqlite3 may keep a zeroed journal file instead of deleting it).
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='killed_writer'")
                             .fetchone()[0], 0)

    def test_read_writes_nothing(self):
        before = self.path.read_bytes()
        store_api.read_header(self.path, worktree_id=self.worktree_id, recover=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ['store.sqlite'])


if __name__ == '__main__':
    unittest.main()
