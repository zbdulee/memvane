"""1.0.24 follow-ups of the 1.0.23 pre-merge review.

F-2: case rotation plans without a write transaction, repeats a plan that
found nothing closed only after something it depends on changed, and leaves
an event for every planned outcome. (F-1 and the unfinished maintenance step
are in ``test_selector_retry``.)
"""
from __future__ import annotations

from contextlib import closing
import sqlite3
import unittest
from unittest import mock

from memvane.learning.context_learning import case_rotation, shared
from memvane.learning.tests.integration.test_rotation_123 import RotationFixture


class RotationPlan(RotationFixture):
    def planned(self):
        real = case_rotation._plan
        calls = []

        def counted(*args):
            calls.append(1)
            return real(*args)
        patcher = mock.patch.object(case_rotation, '_plan', side_effect=counted)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_a_capture_writes_while_the_plan_is_read(self):
        self.fill('q', 6, 'queued')
        closed = self.fill('p', 6, 'pending_review')
        real = case_rotation._plan
        written = []

        def capture_during_plan(*args):
            # Another connection commits a row without waiting: no write lock is held.
            with closing(sqlite3.connect(str(self.root / shared.DATABASE_NAME), timeout=0)) as other:
                with other:
                    other.execute('INSERT INTO cases SELECT project_key,project_root,?,input_sha256,sample,'
                                  'bodies,corpus_fingerprint,captured_at+1000,split,family_keys,use_state,'
                                  "'queued',NULL,NULL,NULL,NULL,NULL FROM cases WHERE request_id=?",
                                  ('late-capture', 'q-0000'))
            written.append(1)
            return real(*args)
        with mock.patch.object(case_rotation, '_plan', side_effect=capture_during_plan):
            result = self.rotate(limit=8)
        self.assertEqual(written, [1])
        # 12 rows planned 4 deletes; the capture made 13, and the four still go.
        self.assertEqual((result['status'], result['count'], result['caseRows']), ('deleted', 4, 9))
        self.assertEqual(self.ids() & set(closed), set(closed[4:]))
        self.assertIn('late-capture', self.ids())

    def test_rows_gone_since_the_plan_never_take_the_table_below_the_limit(self):
        self.fill('q', 4, 'queued')
        closed = self.fill('p', 6, 'pending_review')
        real = case_rotation._plan

        def shrink(db, *args):
            doomed, protected = real(db, *args)
            db.execute('DELETE FROM cases WHERE request_id=?', ('q-0000',))
            db.commit()
            return doomed, protected
        with mock.patch.object(case_rotation, '_plan', side_effect=shrink):
            result = self.rotate(limit=7)
        self.assertEqual((result['count'], result['caseRows']), (2, 7))
        self.assertEqual(self.ids() & set(closed), set(closed[2:]))

    def test_a_plan_that_found_nothing_is_not_repeated_until_something_changes(self):
        calls = self.planned()
        self.fill('q', 12, 'queued')
        first = self.rotate(limit=10)
        self.assertEqual((first['status'], first['count'], first.get('unchanged')), ('nothing_closed', 0, None))
        for _ in range(3):
            again = self.rotate(limit=10)
            self.assertEqual((again['status'], again['count'], again['caseRows'], again['unchanged']),
                             ('nothing_closed', 0, 12, True))
        self.assertEqual(len(calls), 1)
        self.assertEqual([(row['outcome'], row['count'], row['caseRows'], row['protected'], row['limit'])
                          for row in self.rotations()], [('nothing_closed', 0, 12, 0, 10)])
        # More waiting cases change nothing a plan depends on.
        self.fill('w', 3, 'queued')
        self.assertTrue(self.rotate(limit=10)['unchanged'])
        self.assertEqual(len(calls), 1)
        # A closed case does: the next pass plans and deletes it.
        closed = self.fill('p', 1, 'pending_review')
        result = self.rotate(limit=10)
        self.assertEqual((result['status'], result['count']), ('deleted', 1))
        self.assertEqual(len(calls), 2)
        self.assertNotIn(closed[0], self.ids())
        self.assertEqual([row['outcome'] for row in self.rotations()], ['nothing_closed', 'deleted'])
        # Above the limit again with the counts of the remembered plan: quiet at once.
        self.assertTrue(self.rotate(limit=10)['unchanged'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.rotations()), 2)

    def test_an_unreadable_memo_only_plans_again(self):
        calls = self.planned()
        self.fill('q', 12, 'queued')
        self.rotate(limit=10)
        (self.root / case_rotation.STATUS_FILE).write_text('{')
        self.assertEqual(self.rotate(limit=10)['status'], 'nothing_closed')
        self.assertEqual(len(calls), 2)
        with mock.patch('memvane.learning.context_learning.local_cycle._atomic_json',
                        side_effect=OSError('disk')):
            (self.root / case_rotation.STATUS_FILE).unlink()
            self.assertEqual(self.rotate(limit=10)['status'], 'nothing_closed')
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.count(), 12)


if __name__ == '__main__':
    unittest.main()
