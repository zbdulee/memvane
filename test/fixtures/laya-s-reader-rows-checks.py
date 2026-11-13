#!/usr/bin/env python3
"""Recall repins a journal-selected view when its head moves."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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

    def test_recall_repins_once_after_the_view_head_moved(self):
        self.switched()
        before = self.answer()
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


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(ReaderRowsChecks):
        if name in ReaderRowsChecks.__dict__:
            suite.addTest(ReaderRowsChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()
