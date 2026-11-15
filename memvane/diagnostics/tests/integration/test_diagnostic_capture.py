"""Synthetic, owner-private diagnostic capture and reconstruction checks."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from memvane.diagnostics import diagnostic_trace as diag
from memvane.settings import api as settings_api
from tests.support.case import FixtureCase
from memvane.diagnostics import event_log


class Capture(FixtureCase):
    def setUp(self):
        super().setUp()
        root = self.tmp
        self.project = root / 'project'
        (self.project / '.memvane').mkdir(parents=True)
        self.settings = self.project / '.memvane' / 'settings.json'
        self.state = root / 'state'
        self.state.mkdir(mode=0o700)
        self.decision = {'state': 'candidate', 'projectRoot': str(self.project),
                         'journalPath': str(self.state / 'journal.sqlite'),
                         'installId': 'synthetic-install'}
        self.opt_in(True)

    def opt_in(self, enabled):
        self.settings.write_text(json.dumps({'name': 'synthetic',
                                             'diagnostics': {'enabled': enabled}}))

    def rows(self):
        return event_log.read(self.state)

    def thrown(self, msg):
        try:
            try:
                raise ValueError('inner synthetic failure')
            except ValueError as exc:
                raise RuntimeError(msg) from exc
        except RuntimeError as exc:
            return exc

    def capture(self, exc=None, warning=False):
        return diag.capture(self.decision, event='recall', reason='synthetic_failure',
                            exc=exc, warning=warning, consent=settings_api.diagnostic_enabled)

    def test_exception_chain_and_origin_warning(self):
        marker = 'synthetic-message-012345 /Users/synthetic/example'
        private_local = 'synthetic-local-never-recorded'
        diagnostic_id = self.capture(self.thrown(marker))
        self.assertRegex(diagnostic_id, r'^[0-9a-f]{32}$')
        result = diag.reassemble(self.rows(), diagnostic_id)
        self.assertEqual(result['status'], 'complete')
        self.assertIn(marker, result['text'])
        self.assertIn('inner synthetic failure', result['text'])
        self.assertIn('test_diagnostic_capture.py', result['text'])
        self.assertNotIn(str(Path(__file__).resolve()), result['text'])
        self.assertIn('thrown', result['text'])
        self.assertNotIn(private_local, result['text'])
        warning_id = self.capture(warning=True)
        warning = diag.reassemble(self.rows(), warning_id)
        self.assertEqual(warning['status'], 'complete')
        self.assertEqual(warning['kind'], 'origin_stack')
        self.assertIn('test_exception_chain_and_origin_warning', warning['text'])

    def test_off_and_disabled_before_write_leave_no_secret(self):
        sentinel = 'sensitive-synthetic-never-emit'
        self.opt_in(False)
        self.assertIsNone(self.capture(self.thrown(sentinel)))
        self.assertEqual(self.rows(), [])
        self.opt_in(True)
        original = settings_api.diagnostic_enabled
        calls = []
        def turn_off(decision):
            calls.append(1)
            if len(calls) == 2:
                self.opt_in(False)
            return original(decision)
        with patch.object(settings_api, 'diagnostic_enabled', side_effect=turn_off):
            self.assertIsNone(self.capture(self.thrown(sentinel)))
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(self.rows(), [])

    def test_child_stderr_tail_is_bounded_and_disabled_consent_suppresses_it(self):
        sentinel = 'synthetic-child-tail-sentinel'
        token = event_log.bind(decision=self.decision, host='worker')
        try:
            ident = diag.capture(self.decision, event='wake.sync', reason='target_child_failed',
                                 child_stderr='가' * 3000 + sentinel,
                                 consent=settings_api.diagnostic_enabled)
            result = diag.reassemble(self.rows(), ident)
            self.assertEqual(result['kind'], 'child_stderr_tail')
            self.assertTrue(result['truncated'])
            self.assertIn(sentinel, result['text'])
            self.assertLessEqual(len(result['text'].encode('utf-8')), 4096)
            self.opt_in(False)
            self.assertIsNone(diag.capture(self.decision, event='wake.sync', reason='target_child_failed',
                                          child_stderr='disabled-sentinel',
                                          consent=settings_api.diagnostic_enabled))
            self.assertNotIn('disabled-sentinel', json.dumps(self.rows()))
        finally:
            event_log.unbind(token)

    def test_limits_and_incomplete_fragments(self):
        diagnostic_id = self.capture(self.thrown('한' * 10000))
        rows = self.rows()
        record = diag.reassemble(rows, diagnostic_id)
        self.assertEqual(record['status'], 'complete')
        self.assertTrue(record['truncated'])
        self.assertLessEqual(len(record['text'].encode('utf-8')), 16384)
        path = self.state / event_log.NAME
        self.assertTrue(all(len(line) + 1 <= event_log.MAX_LINE
                            for line in path.read_bytes().splitlines()))
        parts = [row for row in rows if row.get('event') == 'diagnostic.part'
                 and row.get('diagnostic_id') == diagnostic_id]
        self.assertTrue(1 <= len(parts) <= 96)
        self.assertTrue(all(len(row['data']) <= 240 for row in parts))
        self.assertEqual(diag.reassemble(list(reversed(rows)), diagnostic_id)['status'], 'complete')
        missing = [row for row in rows if row is not parts[0]]
        self.assertEqual(diag.reassemble(missing, diagnostic_id)['status'], 'incomplete')
        self.assertEqual(diag.reassemble(rows + [parts[0]], diagnostic_id)['status'], 'incomplete')
        corrupted = [dict(row) for row in rows]
        for row in corrupted:
            if row.get('event') == 'diagnostic.part' and row.get('diagnostic_id') == diagnostic_id:
                row['data'] = ('A' if row['data'][0] != 'A' else 'B') + row['data'][1:]
                break
        self.assertEqual(diag.reassemble(corrupted, diagnostic_id)['status'], 'incomplete')
        self.assertEqual(diag.reassemble(parts, diagnostic_id)['status'], 'incomplete')
        header = next(row for row in rows if row.get('event') == 'diagnostic.header'
                      and row.get('diagnostic_id') == diagnostic_id)
        self.assertEqual(header.get('diagnostic_version'), 1)
        bad_version = [dict(row, diagnostic_version=2) if row is header else row for row in rows]
        self.assertEqual(diag.reassemble(bad_version, diagnostic_id)['status'], 'incomplete')


if __name__ == '__main__':
    unittest.main(verbosity=2)
