#!/usr/bin/env python3
"""A journal-selected schema3 endpoint keeps recall and its parity floor after the row-format conversion."""
import importlib.util
from pathlib import Path
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('schema3_journal_fixture', HERE / 'laya-journal-schema3-checks.py')
journal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal)


class ReaderRowsChecks(journal.Schema3EndpointChecks):
    def answer(self):
        with self.resolved() as endpoint:
            result = self.query(endpoint)
        return ([(row['path'], row['score'], row['hash']) for row in result.rows],
                dict(result.source_locations), result.semantic_status)

    def convert(self):
        with self.writer() as view:
            view.convert_to_row_format(parity=dict(gen_id=self.gens[0], receipt_digest=self.parity()['digest']))
        store = self.views.ViewStore.open(self.store_path, repository=self.repository,
                                          scope_id=self.case.route['scopeId'])
        self.addCleanup(store.close)
        return store

    def test_converted_selected_store_answers_the_same_recall_and_parity_floor(self):
        self.switched()
        before = self.answer()
        self.assertTrue(before[0], before)
        with self.writer() as view:
            ordinal = view._db.execute('SELECT ordinal FROM generations WHERE gen_id=?',
                                       (self.gens[0],)).fetchone()[0]
            view_id = view.capture_view(self.worktree_id).view_id
        store = self.convert()
        self.assertTrue(store._rows)
        self.assertEqual(self.answer(), before)
        self.assertEqual(self.reader._generation_ordinal(store, self.gens[0], view_id), ordinal)
        anchor = store._db.execute('SELECT gen_id,ordinal,incarnation FROM s_parity_anchor').fetchall()
        self.assertEqual([tuple(row) for row in anchor], [(self.gens[0], ordinal, store._incarnation)])

    def test_recall_repins_once_after_the_view_head_moved(self):
        self.switched()
        before = self.answer()
        self.convert()
        import laya_view_rows as rows
        import laya_blob_values as v
        real, calls = rows.RowsMixin._rows_search, []

        def moved_once(self_, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                v.fail('view_head_changed')
            return real(self_, *args, **kwargs)
        with mock.patch.object(rows.RowsMixin, '_rows_search', moved_once):
            self.assertEqual(self.answer(), before)
        self.assertGreater(len(calls), 1)

        def always(self_, *args, **kwargs):
            v.fail('view_head_changed')
        with mock.patch.object(rows.RowsMixin, '_rows_search', always):
            with self.assertRaises(Exception) as caught:
                self.answer()
        self.assertIn('view_head_changed', str(getattr(caught.exception, 'reason', caught.exception)))

    def test_conversion_refuses_a_parity_generation_outside_the_store(self):
        self.switched()
        with self.writer() as view:
            with self.assertRaises(Exception) as caught:
                view.convert_to_row_format(parity=dict(gen_id='g-' + 'a' * 32 + '-b' + 'b' * 64,
                                                       receipt_digest='d' * 64))
        self.assertEqual(getattr(caught.exception, 'reason', None), 'row_conversion_parity_invalid')


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(ReaderRowsChecks):
        if name in ReaderRowsChecks.__dict__:
            suite.addTest(ReaderRowsChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()
