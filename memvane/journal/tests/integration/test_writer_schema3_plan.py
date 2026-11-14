"""The selected writer's store plan: exactly the reader record's format 4 store.

The writer binds the store path, incarnation, main worktree, worktree binding
and settings policy the selected reader record names, under the route's exact
roots, and marks its first write in the journal before it uses the store.
"""
import json
import os
import shutil
import unittest

from memvane.journal.tests.support import InstallCase, file_state


def journal_api():
    from memvane.journal import api
    return api


class WriterStorePlan(InstallCase):
    def test_the_selected_writer_binds_the_exact_store(self):
        from memvane.store import api as store_api
        record, row = self.record(), self.row()
        with self.writer() as writer:
            store, proof = writer.store(), writer.admission_proof()
        self.assertEqual({key: store[key] for key in ('path', 'incarnation', 'worktree_id',
                                                      'worktree_binding', 'policy_digest')},
                         {'path': record['db_path'], 'incarnation': record['incarnation'],
                          'worktree_id': record['worktree_id'],
                          'worktree_binding': record['worktree_binding'],
                          'policy_digest': record['settings_policy_digest']})
        header = store_api.read_header(self.install.store_path, project_root=str(self.project), timeout=5)
        self.assertEqual(store['repository'], json.loads(header['repository_json']))
        self.assertEqual({key: proof[key] for key in ('selectionEpoch', 'projectRoot', 'storePath',
                                                      'storeIncarnation', 'journalPath')},
                         {'selectionEpoch': row['epoch'], 'projectRoot': str(self.project),
                          'storePath': record['db_path'], 'storeIncarnation': record['incarnation'],
                          'journalPath': str(self.install.journal)})

    def test_a_replaced_store_refuses_the_writer(self):
        store = self.install.store_path
        copy = store.with_name('copy.sqlite')
        shutil.copy2(store, copy)
        os.chmod(copy, 0o600)
        original = store.with_name('original.sqlite')
        os.rename(store, original)
        os.rename(copy, store)
        # Same bytes under a new inode: the route no longer selects the recorded store.
        self.assertNotEqual(journal_api().select_route(str(self.project), action='recall'), self.decision)
        self.refused('host_decision_changed', self.writer().__enter__)
        os.replace(original, store)
        with self.writer() as writer:
            writer.validate()

    def test_a_pending_selection_transition_refuses_the_writer(self):
        journal = journal_api()
        ledger = self.state / 'ledger.json'
        raw, store = ledger.read_bytes(), file_state(self.install.store_path)
        for phase in ('restoring', 'prepared'):
            with self.subTest(phase=phase):
                journal.ledger_write(ledger, dict(json.loads(raw), phase=phase), replace=True)
                self.refused('selection_transition_pending', self.writer().__enter__)
                self.refused('selection_transition_pending', self.endpoint_id)
        ledger.write_bytes(raw)
        self.assertEqual(file_state(self.install.store_path), store)
        with self.writer() as writer:
            writer.validate()

    def test_the_first_write_mark_advances_the_epoch_once_and_reproves(self):
        epoch = self.row()['epoch']
        with self.writer() as writer:
            first = writer.mark_new_write()
            self.assertEqual(writer.mark_new_write(), first)
            self.assertEqual(writer.admission_proof(), first)
        row = self.row()
        self.assertEqual((first['selectionEpoch'], row['epoch'], row['newWriteSeen']),
                         (epoch + 1, epoch + 1, True))

    def test_a_selection_change_before_the_mark_refuses_it(self):
        epoch = self.row()['epoch']
        with self.writer() as writer:
            self.forge('UPDATE selection SET epoch=epoch+1 WHERE id=1')
            self.refused('normal_writer_changed', writer.mark_new_write)
        row = self.row()
        self.assertEqual((row['epoch'], row['newWriteSeen']), (epoch + 1, False))


if __name__ == '__main__':
    unittest.main()
