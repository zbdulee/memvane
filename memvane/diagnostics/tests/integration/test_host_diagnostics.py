import json
from unittest.mock import patch

from tests.support.case import FixtureCase
from memvane.host import api as host
from memvane.diagnostics import diagnostic_trace as diag, event_log


class HostDiagnosticsTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.tmp / 'project'
        (self.project / '.memvane').mkdir(parents=True)
        self.state = self.tmp / 'state'
        self.state.mkdir(mode=0o700)
        self.decision = {'state': 'candidate', 'projectRoot': str(self.project),
                         'journalPath': str(self.state / 'journal.sqlite'), 'installId': 'synthetic'}
        self.payload = {'prompt': 'synthetic', 'session_id': 'synthetic-session'}

    def consent(self, enabled):
        (self.project / '.memvane' / 'settings.json').write_text(json.dumps({'diagnostics': {'enabled': enabled}}))

    def test_host_errors_keep_identity_and_obey_consent(self):
        sentinel = 'synthetic-host-sentinel'
        for enabled in (True, False):
            self.consent(enabled)
            for name in ('claude', 'codex', 'hermes'):
                with self.subTest(enabled=enabled, host=name):
                    with patch('memvane.settings.api.diagnostic_enabled', return_value=enabled), patch('memvane.journal.api.verify_selected_route', side_effect=ValueError(sentinel)):
                        with self.assertRaisesRegex(ValueError, sentinel):
                            host.serve_selected('recall', name, self.decision, self.payload)
                    rows = event_log.read(self.state)
                    row = [r for r in rows if r.get('event') == 'recall'][-1]
                    self.assertEqual(row['host'], name)
                    self.assertEqual(row['outcome'], 'refused')
                    if enabled:
                        diagnostic_id = row['diagnostic_id']
                        record = diag.reassemble(rows, diagnostic_id)
                        self.assertEqual(record['status'], 'complete')
                        self.assertIn(sentinel, record['text'])
                        linked = [r for r in rows if r.get('diagnostic_id') == diagnostic_id]
                        self.assertTrue(all(r.get('host') == name for r in linked))
                        self.assertTrue(all(r.get('session') == 'synthetic-session' for r in linked))
                    else:
                        self.assertNotIn('diagnostic_id', row)

    def test_logging_failure_preserves_original_exception(self):
        self.consent(True)
        for target in ('memvane.diagnostics.api.emit', 'memvane.diagnostics.api.capture'):
            with self.subTest(target=target), patch('memvane.settings.api.diagnostic_enabled', return_value=True), patch(target, side_effect=OSError('logging failed')):
                with patch('memvane.journal.api.verify_selected_route', side_effect=ValueError('original')):
                    with self.assertRaisesRegex(ValueError, 'original'):
                        host.serve_selected('recall', 'claude', self.decision, self.payload)
