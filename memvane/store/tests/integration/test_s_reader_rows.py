"""Recall over a selected format 4 store repins once when the view head moved under the pin.

The store refuses a pinned lexical read whose head moved (``view_head_changed``);
recall pins the new head once and never serves another view. The project is
onboarded through the install use cases (``tests.support.install_builder``).
"""
import unittest
from unittest import mock

from memvane.store.blob import blob_values as v
from memvane.store.view.view_rows import RowsMixin
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project

PROMPT = 'which protocol keeps marker token zqheronx'


class ReaderRows(FixtureCase):
    def setUp(self):
        super().setUp()
        project = make_project(self.home, name='readerrows')
        note = project.write_note('notes.md', 'Field notes: the emerald heron nests beside the reed pond.\n')
        project.write_card('concepts/heron.md', title='Heron protocol', sources=[note],
                           body='The heron protocol keeps marker token zqheronx for recall checks.\n')
        project.commit('fixture')
        self.install = build_install(self, project)
        self.install.settle()

    def test_recall_repins_once_after_the_view_head_moved(self):
        before = self.install.recall(PROMPT)
        self.assertIn('zqheronx', before)
        real, calls = RowsMixin._rows_search, []

        def moved_once(store, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                v.fail('view_head_changed')
            return real(store, *args, **kwargs)
        with mock.patch.object(RowsMixin, '_rows_search', moved_once):
            self.assertEqual(self.install.recall(PROMPT), before)
        self.assertEqual(len(calls), 2)

        def always(store, *args, **kwargs):
            calls.append(1)
            v.fail('view_head_changed')
        del calls[:]
        # A head that keeps moving is refused after the one repin; no other view answers.
        with mock.patch.object(RowsMixin, '_rows_search', always), \
                self.assertRaises(v.BlobStoreError) as caught:
            self.install.recall(PROMPT)
        self.assertEqual(caught.exception.reason, 'view_head_changed')
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
