"""Synthetic host recall diagnostic integration checks (Task 3, RED)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from memvane.diagnostics import diagnostic_trace as diag
from memvane.diagnostics import event_log
from memvane.host import host_response as host

SENTINEL = 'synthetic-host-sentinel-7f3a9c'


class HostRecall(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.environ.get('QMD_SETUP_GUARD_FIXTURE'), '1')
        home = Path(os.environ['HOME']).resolve()
        self.assertIn(Path(tempfile.gettempdir()).resolve(), home.parents)
        tmp = tempfile.TemporaryDirectory(dir=str(home))
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.project = root / 'project'
        (self.project / '.memvane').mkdir(parents=True)
        self.settings = self.project / '.memvane' / 'settings.json'
        self.state = root / 'state'
        self.state.mkdir(mode=0o700)
        self.decision = {'state': 'candidate', 'projectRoot': str(self.project),
                         'journalPath': str(self.state / 'journal.sqlite'),
                         'installId': 'synthetic-install'}
        self.payload = {'prompt': 'synthetic prompt', 'session_id': 'synthetic-session'}
        self.opt_in(True)

    def opt_in(self, enabled):
        self.settings.write_text(json.dumps({'name': 'synthetic',
                                             'diagnostics': {'enabled': enabled}}))

    def rows(self):
        return event_log.read(self.state)

    def raw(self):
        path = self.state / event_log.NAME
        return path.read_bytes() if path.exists() else b''

    def recall_rows(self):
        return [row for row in self.rows() if row.get('event') == 'recall']

    def raise_in_scope(self):
        sentinel = ValueError(SENTINEL)
        with self.assertRaises(ValueError) as caught:
            with host._event_scope('recall', 'claude', self.decision, self.payload):
                raise sentinel
        self.assertIs(caught.exception, sentinel)
        return sentinel

    def test_enabled_exception_joins_recall_event(self):
        self.raise_in_scope()
        [row] = self.recall_rows()
        self.assertEqual(row.get('outcome'), 'refused')
        self.assertEqual(row.get('reason'), 'selected_proof_or_query_failed')
        diagnostic_id = row.get('diagnostic_id')
        self.assertRegex(diagnostic_id or '', r'^[0-9a-f]{32}$')
        record = diag.reassemble(self.rows(), diagnostic_id)
        self.assertEqual(record['status'], 'complete')
        linked = [item for item in self.rows() if item.get('diagnostic_id') == diagnostic_id]
        self.assertTrue(linked)
        self.assertTrue(all(item.get('host') == 'claude' for item in linked))
        self.assertTrue(all(item.get('session') == 'synthetic-session' for item in linked))
        self.assertEqual(record['kind'], 'exception')
        self.assertIn('Traceback', record['text'])
        self.assertIn(SENTINEL, record['text'])
        self.assertIn('raise_in_scope', record['text'])

    def test_disabled_records_no_sentinel_and_no_id(self):
        self.opt_in(False)
        self.raise_in_scope()
        [row] = self.recall_rows()
        self.assertEqual(row.get('reason'), 'selected_proof_or_query_failed')
        self.assertNotIn('diagnostic_id', row)
        self.assertFalse(any(r.get('event', '').startswith('diagnostic.')
                             for r in self.rows()))
        self.assertNotIn(SENTINEL.encode(), self.raw())

    def test_each_host_keeps_its_identity_in_diagnostics(self):
        for name in ('claude', 'codex', 'hermes'):
            with self.subTest(host=name):
                with self.assertRaises(ValueError):
                    with host._event_scope('recall', name, self.decision, self.payload):
                        raise ValueError(SENTINEL)
                row = self.recall_rows()[-1]
                linked = [item for item in self.rows()
                          if item.get('diagnostic_id') == row.get('diagnostic_id')]
                self.assertTrue(linked)
                self.assertTrue(all(item.get('host') == name for item in linked))
                self.assertEqual(diag.reassemble(self.rows(), row['diagnostic_id'])['status'],
                                 'complete')

    def test_logging_failure_keeps_original_exception(self):
        def boom(*args, **kwargs):
            raise OSError('synthetic logging failure')
        with patch.object(event_log, 'emit', side_effect=boom):
            self.raise_in_scope()
        with patch.object(diag, 'capture', side_effect=boom):
            self.raise_in_scope()

    def test_invalid_pool_warning_links_origin_stack(self):
        with host._event_scope('recall', 'claude', self.decision, self.payload):
            host._write_selection_log(self.project, 'laya_selector_invalid_pool', [])
        [row] = self.recall_rows()
        self.assertIn('laya_selector_invalid_pool', row.get('notes') or [])
        ids = row.get('diagnostic_ids') or [row.get('diagnostic_id')]
        self.assertTrue(ids and all(isinstance(i, str) for i in ids), row)
        record = diag.reassemble(self.rows(), ids[0])
        self.assertEqual(record['status'], 'complete')
        self.assertEqual(record['kind'], 'origin_stack')
        self.assertIn('test_invalid_pool_warning_links_origin_stack', record['text'])

    def test_no_candidates_is_not_a_warning(self):
        with host._event_scope('recall', 'claude', self.decision, self.payload):
            host._write_selection_log(self.project, 'no_candidates', [])
            host._write_selection_log(self.project, 'laya_selector_selected_zero', [])
        [row] = self.recall_rows()
        self.assertNotIn('diagnostic_id', row)
        self.assertNotIn('diagnostic_ids', row)
        self.assertFalse(any(r.get('event', '').startswith('diagnostic.')
                             for r in self.rows()))

    def test_exception_survives_warning_capture_limit(self):
        with self.assertRaisesRegex(ValueError, SENTINEL):
            with host._event_scope('recall', 'claude', self.decision, self.payload):
                for _ in range(5):
                    host._write_selection_log(self.project, 'laya_selector_invalid_pool', [])
                raise ValueError(SENTINEL)
        [row] = self.recall_rows()
        self.assertEqual(len(row['diagnostic_ids']), 4)
        detail = diag.reassemble(self.rows(), row['diagnostic_id'])
        self.assertEqual(detail['status'], 'complete')
        self.assertIn(SENTINEL, detail['text'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
