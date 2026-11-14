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

    def test_recall_trace_warnings_are_bounded_and_exception_is_preserved(self):
        self.consent(True)
        proof = {'productRouteAllowed': True, 'stateFormat': 1, 'writerFloor': '1.0.0'}
        def serve(*args, **kwargs):
            trace = kwargs['trace']
            for _ in range(5):
                trace.note('laya_selector_invalid_pool')
            raise ValueError('synthetic-final-error')
        with patch('memvane.settings.api.diagnostic_enabled', return_value=True), \
             patch('memvane.journal.api.verify_selected_route', return_value=proof), \
             patch('memvane.recall.api.serve_endpoint_recall', side_effect=serve):
            with self.assertRaisesRegex(ValueError, 'synthetic-final-error'):
                host.serve_selected('recall', 'claude', self.decision, self.payload)
        rows = event_log.read(self.state)
        row = [r for r in rows if r.get('event') == 'recall'][-1]
        self.assertIn('laya_selector_invalid_pool', row['notes'])
        self.assertEqual(len(row['diagnostic_ids']), 4)
        warning = diag.reassemble(rows, row['diagnostic_ids'][0])
        self.assertEqual(warning['status'], 'complete')
        self.assertEqual(warning['kind'], 'origin_stack')
        exception = diag.reassemble(rows, row['diagnostic_id'])
        self.assertEqual(exception['status'], 'complete')
        self.assertIn('synthetic-final-error', exception['text'])

    def test_empty_recall_notes_do_not_produce_diagnostics(self):
        self.consent(True)
        proof = {'productRouteAllowed': True, 'stateFormat': 1, 'writerFloor': '1.0.0'}
        def serve(*args, **kwargs):
            kwargs['trace'].note('no_candidates')
            kwargs['trace'].note('laya_selector_selected_zero')
            from memvane.recall.types import RecallAnswer
            return RecallAnswer(self.decision)
        with patch('memvane.settings.api.diagnostic_enabled', return_value=True), \
             patch('memvane.journal.api.verify_selected_route', return_value=proof), \
             patch('memvane.recall.api.serve_endpoint_recall', side_effect=serve):
            result = host.serve_selected('recall', 'claude', self.decision, self.payload)
            self.assertEqual(result['hookSpecificOutput']['additionalContext'], '')
        rows = event_log.read(self.state)
        row = [r for r in rows if r.get('event') == 'recall'][-1]
        self.assertNotIn('diagnostic_id', row)
        self.assertNotIn('diagnostic_ids', row)
        self.assertFalse(any(r.get('event', '').startswith('diagnostic.') for r in rows))

    def test_recall_event_records_cards_and_hashed_prompt(self):
        import hashlib
        from memvane.recall.types import RecallAnswer
        self.consent(False)
        decision = {**self.decision, 'scopeId': 'a' * 32, 'sourceRoot': str(self.project),
                    'collectionName': 'fixture'}
        body = 'Synthetic source body'
        digest = hashlib.sha256(body.encode()).hexdigest()
        rows = [{'scope': 'a' * 32, 'generation': 'g1', 'path': 'docs/a.md',
                 'hash': digest, 'body': body, 'score': 0.5}]
        def serve(*args, **kwargs):
            kwargs['trace'].note('laya_vector_selected_provider_preparing')
            return RecallAnswer(decision, rows=rows, selection_fallback='laya_selector_not_prepared',
                                vector_fallback='selected_provider_preparing')
        prompt = 'private synthetic question'
        proof = {'productRouteAllowed': True, 'stateFormat': 1, 'writerFloor': '1.0.0'}
        with patch('memvane.settings.api.diagnostic_enabled', return_value=False), \
             patch('memvane.journal.api.verify_selected_route', return_value=proof), \
             patch('memvane.recall.api.serve_endpoint_recall', side_effect=serve):
            host.serve_selected('recall', 'codex', decision, {'prompt': prompt, 'session_id': 'session-1'})
        row = [r for r in event_log.read(self.state) if r.get('event') == 'recall'][-1]
        self.assertEqual((row['outcome'], row['reason'], row['count']), ('served', 'selected', 1))
        self.assertEqual(row['cards'], [{'path': 'docs/a.md', 'sha': digest}])
        self.assertEqual(row['selection_fallback'], 'laya_selector_not_prepared')
        self.assertEqual(row['vector_fallback'], 'selected_provider_preparing')
        self.assertIn('laya_vector_selected_provider_preparing', row['notes'])
        self.assertEqual(row['prompt'], {'sha256': hashlib.sha256(prompt.encode()).hexdigest(), 'chars': len(prompt)})
        self.assertNotIn(prompt, (self.state / event_log.NAME).read_text())

    def test_hook_actions_log_one_fixed_refusal_without_private_text(self):
        from memvane.recall.types import HostResponseError
        actions = ('gate', 'posttool', 'update', 'update-queued', 'index', 'compile', 'file-change-event')
        for action in actions:
            with self.subTest(action=action), \
                 patch('memvane.journal.api.verify_selected_route', side_effect=HostResponseError('selected_proof_changed')):
                with self.assertRaises(HostResponseError):
                    host.serve_selected(action, 'claude', self.decision, self.payload)
        rows = [r for r in event_log.read(self.state) if r.get('event') in actions]
        self.assertEqual([r['event'] for r in rows], list(actions))
        for row in rows:
            self.assertEqual((row['outcome'], row['reason'], row['host'], row['session']),
                             ('refused', 'selected_proof_changed', 'claude', 'synthetic-session'))
            self.assertNotIn('synthetic prompt', json.dumps(row))

    def test_learning_capture_status_is_recorded(self):
        from memvane.learning import api as learning
        status = learning.capture_recall('claude', self.decision, {}, {}, [], [])
        rows = [r for r in event_log.read(self.state) if r.get('event') == 'learning.capture']
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['outcome'], rows[0]['host']), (status, 'claude'))
        self.assertEqual(status, 'disabled')

    def test_logging_failure_preserves_original_exception(self):
        self.consent(True)
        for target in ('memvane.diagnostics.api.emit', 'memvane.diagnostics.api.capture'):
            with self.subTest(target=target), patch('memvane.settings.api.diagnostic_enabled', return_value=True), patch(target, side_effect=OSError('logging failed')):
                with patch('memvane.journal.api.verify_selected_route', side_effect=ValueError('original')):
                    with self.assertRaisesRegex(ValueError, 'original'):
                        host.serve_selected('recall', 'claude', self.decision, self.payload)
