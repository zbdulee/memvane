"""Selected writer scope: same-thread admission, per-operation deadlines and clean refusal.

A writer is valid only in the thread that entered its scope, until that
thread exits it. Each validation re-proves the owner, the reader lifetime and
the selected route; a deadline bounds one operation, not the scope.
"""
import math
import threading
import time
import unittest

from memvane.journal import writer_scope
from memvane.journal.tests.support import InstallCase, file_state


def journal_api():
    from memvane.journal import api
    return api


class WriterScope(InstallCase):
    def test_each_operation_deadline_is_checked_without_ending_the_scope(self):
        with self.writer() as writer:
            for _ in range(3):
                writer.validate(deadline=time.monotonic() + 60)
            self.refused('normal_writer_deadline', writer.validate, deadline=time.monotonic() - 1)
            for value in (math.nan, math.inf, '1', True):
                with self.subTest(deadline=value):
                    self.refused('reader_window_deadline_invalid', writer.validate, deadline=value)
            self.assertEqual(writer.admission_proof()['selectionEpoch'], self.row()['epoch'])

    def test_a_held_reader_and_a_moved_head_do_not_block_admission(self):
        floor = self.record()['head_floor']
        with self.endpoint():
            with self.writer() as writer:
                writer.validate()
        self.fixture.write_card('egret.md', title='Snowy egret', body='The snowy egret wades at noon.\n')
        self.fixture.commit('egret')
        self.install.settle()
        self.assertGreater(self.head()[1], floor['epoch'])
        with self.writer() as writer:
            self.assertEqual(writer.store()['path'], str(self.install.store_path))

    def test_a_failed_admission_issues_nothing_and_releases_its_locks(self):
        journal = journal_api()
        store = file_state(self.install.store_path)
        self.forge("UPDATE selection SET writer_floor='99.0.0' WHERE id=1")
        with self.owner() as owner:
            scope = journal.selected_v1_writer_scope(self.decision, queue_owner=owner)
            self.refused('writer_floor', scope.__enter__)
            self.assertEqual(writer_scope._selected_issued, {})
            with journal.reader_lifetime(self.install.install_id, exclusive=True):
                pass
            self.refused('normal_writer_reentered', scope.__enter__)
            self.forge("UPDATE selection SET writer_floor='1.0.0' WHERE id=1")
            with journal.selected_v1_writer_scope(self.decision, queue_owner=owner) as writer:
                writer.validate()
        self.assertEqual(file_state(self.install.store_path), store)

    def test_another_thread_can_neither_use_nor_end_the_writer(self):
        outcomes = {}
        with self.writer() as writer:
            scope = writer_scope._selected_issued[writer]

            def foreign():
                for name, call in (('validate', writer.validate), ('exit', lambda: scope.__exit__(None, None, None))):
                    try:
                        call()
                        outcomes[name] = 'ok'
                    except ValueError as exc:
                        outcomes[name] = str(exc)
            thread = threading.Thread(target=foreign)
            thread.start()
            thread.join()
            writer.validate()
        self.assertEqual(outcomes, {'validate': 'normal_writer_expired', 'exit': 'normal_writer_owner_changed'})
        self.refused('normal_writer_not_issued', writer.validate)

    def test_an_unselected_or_changed_decision_cannot_issue(self):
        journal = journal_api()
        changed = {
            'unselected_host_route': (dict(self.decision, state='worktree'),
                                      dict(self.decision, routeSchema='memvane-route-v3')),
            'host_decision_changed': (dict(self.decision, storeIncarnation='0' * 32),
                                      dict(self.decision, scopeId='0' * 32)),
        }
        with self.owner() as owner:
            for reason, decisions in changed.items():
                for decision in decisions:
                    with self.subTest(reason=reason):
                        self.refused(reason, journal.selected_v1_writer_scope(decision, queue_owner=owner).__enter__)
        self.assertEqual(writer_scope._selected_issued, {})


if __name__ == '__main__':
    unittest.main()
