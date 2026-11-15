import json
import subprocess
from unittest.mock import patch

from tests.support.case import FixtureCase
from hermes_adapter.core_bridge import recall_context
from memvane.diagnostics import api as diagnostics, diagnostic_trace, event_log


class HermesDiagnosticsTests(FixtureCase):
    def setUp(self):
        super().setUp()
        from memvane.host import api as host
        host.refusal_code(ValueError('fixture preload'))
        self.project = self.tmp / 'project'
        (self.project / '.memvane').mkdir(parents=True)
        (self.project / '.memvane/settings.json').write_text(json.dumps({'name': 'fixture'}))
        self.state = self.tmp / 'state'
        self.state.mkdir(mode=0o700)
        self.decision = {'state': 'candidate', 'projectRoot': str(self.project),
                         'journalPath': str(self.state / 'journal.sqlite'), 'installId': 'fixture'}

    def invoke(self, error, *, enabled=True, capture_fails=False):
        selected = subprocess.CompletedProcess([], 0, stdout='candidate\n', stderr='')
        capture = patch('memvane.diagnostics.api.capture', side_effect=OSError('synthetic')) if capture_fails else None
        with patch('subprocess.run', return_value=selected), \
             patch('memvane.journal.api.select_route', return_value=self.decision), \
             patch('memvane.host.api.serve_selected', side_effect=error), \
             patch('memvane.settings.api.diagnostic_enabled', return_value=enabled):
            if capture:
                with capture:
                    return recall_context('synthetic prompt', cwd=str(self.project))
            return recall_context('synthetic prompt', cwd=str(self.project))

    def test_unlogged_exception_is_linked_without_exposing_body_in_refusal(self):
        answer = self.invoke(ValueError('synthetic-private-error'))
        self.assertEqual(answer, {'context': '[memvane] selected proof or query failed; request refused'})
        rows = event_log.read(self.state)
        row = next(r for r in rows if r.get('event') == 'recall.selected')
        self.assertEqual(row['host'], 'hermes')
        self.assertNotIn('synthetic-private-error', json.dumps(row))
        detail = diagnostic_trace.reassemble(rows, row['diagnostic_id'])
        self.assertEqual(detail['status'], 'complete')
        self.assertIn('synthetic-private-error', detail['text'])

    def test_disabled_and_logging_failure_preserve_refusal_without_diagnostics(self):
        for enabled, capture_fails in ((False, False), (True, True)):
            with self.subTest(enabled=enabled, capture_fails=capture_fails):
                answer = self.invoke(ValueError('synthetic-private-error'), enabled=enabled,
                                     capture_fails=capture_fails)
                self.assertEqual(answer, {'context': '[memvane] selected proof or query failed; request refused'})
                rows = event_log.read(self.state)
                row = [r for r in rows if r.get('event') == 'recall.selected'][-1]
                self.assertNotIn('diagnostic_id', row)
                self.assertNotIn('synthetic-private-error', json.dumps(rows))

    def test_already_logged_exception_is_not_recorded_twice(self):
        with patch('memvane.settings.api.diagnostic_enabled', return_value=True):
            try:
                raise ValueError('synthetic-recorded-error')
            except ValueError as error:
                ident = diagnostics.capture(self.decision, event='recall.selected', reason='synthetic',
                                            exc=error, consent=lambda _decision: True)
                error._memvane_event_logged = True
                answer = self.invoke(error)
        self.assertIsNotNone(ident)
        self.assertEqual(answer, {'context': '[memvane] selected proof or query failed; request refused'})
        rows = event_log.read(self.state)
        self.assertEqual(len([r for r in rows if r.get('event') == 'diagnostic.header']), 1)
        self.assertEqual({r['diagnostic_id'] for r in rows if r.get('diagnostic_id')}, {ident})
