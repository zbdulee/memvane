"""Journal store reads through store.api.read_header keep their identity checks and refusals."""
import os
import unittest

from memvane.journal import journal_reader as R
from memvane.journal.types import JournalEvidenceError
from memvane.kernel.errors import _query_only_uri
from memvane.store import api as store_api
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.sqlite_hot_journal import kill_mid_write

SCOPE = '5' * 32


class JournalStoreHeader(FixtureCase):
    def setUp(self):
        super().setUp()
        self.base = self.home
        fixture = make_project(self.home)
        fixture.commit('fixture')
        self.project, self.state = fixture.root, self.home / 'state'
        self.state.mkdir(mode=0o700)
        self.path = self.state / 'store.sqlite'
        self.binding = store_api.observe_binding(self.path, scope_id=SCOPE, create_root=str(self.project))
        self.worktree_id = self.binding['saved']['worktree_id']
        self.path.chmod(0o600)

    def refused(self, reason, call, *args, **kwargs):
        with self.assertRaises(JournalEvidenceError) as caught:
            call(*args, **kwargs)
        self.assertEqual(str(caught.exception), reason)

    def schema3_store(self, project=None, worktree_id=None, scope=SCOPE):
        return R._schema3_store(self.state, project or self.project, {'scopeId': scope},
                                {'storePath': str(self.path)}, str(self.path),
                                worktree_id or self.worktree_id)

    def test_schema3_store_identity(self):
        actual = self.schema3_store()
        self.assertEqual((actual['incarnation'], actual['worktree_id'], actual['worktree_binding'],
                          actual['settings_policy_digest']),
                         (self.binding['incarnation'], self.worktree_id,
                          self.binding['saved']['binding_digest'], self.binding['policy_digest']))
        self.assertEqual(actual['db_node'], R._file(self.path, self.state))
        self.assertEqual(len(actual['repository_digest']), 64)

    def test_missing_worktree_wrong_project_or_scope_is_store_changed(self):
        self.refused('reader_store_changed', self.schema3_store, worktree_id='0' * 32)
        self.refused('reader_store_changed', self.schema3_store, project=self.base / 'elsewhere')
        self.refused('reader_store_changed', self.schema3_store, scope='6' * 32)

    def test_identity_checks_stay_around_the_read(self):
        self.path.chmod(0o644)
        self.refused('reader_store_unsafe', R._store_header, self.path, self.state)
        self.path.chmod(0o600)
        self.refused('reader_store_path_invalid', R._store_header, self.path, self.base)
        garbage = self.state / 'garbage.sqlite'
        garbage.write_bytes(b'not a database' * 100)
        garbage.chmod(0o600)
        self.refused('reader_store_invalid', R._store_header, garbage, self.state)

    def test_hot_journal_pending_without_recover_rolled_back_with_it(self):
        kill_mid_write(self.path)
        os.chmod(str(self.path) + '-journal', 0o600)
        if _query_only_uri(self.path).endswith('?mode=ro'):
            self.refused('reader_store_recovery_pending', R._store_header, self.path, self.state)
            self.refused('reader_store_recovery_pending', self.schema3_store)
        header = R._store_header(self.path, self.state, recover=True, worktree_id=self.worktree_id)
        self.assertEqual(header['worktree']['project_root'], str(self.project))
        self.assertEqual(self.schema3_store()['incarnation'], self.binding['incarnation'])


if __name__ == '__main__':
    unittest.main()
