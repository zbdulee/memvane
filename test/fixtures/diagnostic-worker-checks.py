"""Synthetic worker error boundary integration checks."""
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
from memvane.route import index_host_route as routes; import memvane.journal.route_selection as routes_route_selection
from memvane.app import runtime_dispatch as dispatch
from memvane.wiki.compile import wiki_compile_worker as compile_worker
from memvane.wiki.compile import wiki_verify_worker

SENTINEL = 'synthetic-worker-exception-091'


class Worker(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.environ.get('QMD_SETUP_GUARD_FIXTURE'), '1')
        home = Path(os.environ['HOME']).resolve()
        self.assertIn(Path(tempfile.gettempdir()).resolve(), home.parents)
        tmp = tempfile.TemporaryDirectory(dir=str(home))
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.project = base / 'project'
        (self.project / '.memvane').mkdir(parents=True)
        self.settings = self.project / '.memvane' / 'settings.json'
        self.state = base / 'state'
        self.state.mkdir(mode=0o700)
        self.decision = {'state': 'candidate', 'projectRoot': str(self.project),
                         'journalPath': str(self.state / 'install-journal.sqlite'),
                         'installId': 'synthetic-install'}
        self.enable(True)
        token = event_log.bind(decision=self.decision, host='worker')
        self.addCleanup(lambda: event_log.unbind(token))

    def enable(self, value):
        self.settings.write_text(json.dumps({'name': 'synthetic',
                                             'diagnostics': {'enabled': value}}))

    def check_event(self, event, enabled):
        rows = event_log.read(self.state)
        matching = [r for r in rows if r.get('event') == event]
        self.assertEqual(len(matching), 1)
        row = matching[0]
        if enabled:
            detail = diag.reassemble(rows, row.get('diagnostic_id'))
            self.assertEqual(detail['status'], 'complete')
            self.assertIn(SENTINEL, detail['text'])
        else:
            self.assertNotIn('diagnostic_id', row)
            self.assertNotIn(SENTINEL, json.dumps(rows))

    def test_verify_exception_does_not_break_worker(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.enable(enabled)
                with patch.object(routes_route_selection, 'select_route', return_value=self.decision), \
                     patch.object(wiki_verify_worker, 'run', side_effect=ValueError(SENTINEL)):
                    self.assertIsNone(compile_worker._run_verify_pass(self.project, {}, {}))
                self.check_event('compile.verify_pass', enabled)
                (self.state / event_log.NAME).unlink()

    def test_dispatch_exception_preserves_identity(self):
        target = {'terminal': False, 'project': str(self.project),
                  'journalPath': self.decision['journalPath'],
                  'installId': self.decision['installId']}
        exc = ValueError(SENTINEL)
        with patch.object(routes_route_selection, 'select_route', return_value=self.decision), \
             patch.object(dispatch, 'resolve', return_value=target), \
             patch.object(dispatch, '_run_target', side_effect=exc):
            with self.assertRaises(ValueError) as caught:
                dispatch.run('laya-sync', str(self.project))
        self.assertIs(caught.exception, exc)
        self.check_event('wake.sync', True)

    def test_preparing_records_warning_origin(self):
        target = {'terminal': False, 'project': str(self.project),
                  'journalPath': self.decision['journalPath'],
                  'installId': self.decision['installId']}
        exc = dispatch.DispatchFailure('target_preparing')
        with patch.object(routes_route_selection, 'select_route', return_value=self.decision), \
             patch.object(dispatch, 'resolve', return_value=target), \
             patch.object(dispatch, '_run_target', side_effect=exc):
            with self.assertRaises(dispatch.DispatchFailure):
                dispatch.run('laya-sync', str(self.project))
        rows = event_log.read(self.state)
        row = next(r for r in rows if r.get('event') == 'wake.sync')
        self.assertEqual(row.get('severity'), 'warning')
        detail = diag.reassemble(rows, row.get('diagnostic_id'))
        self.assertEqual(detail['status'], 'complete')
        self.assertEqual(detail['kind'], 'origin_stack')

    def test_child_process_stack_is_not_invented(self):
        target = {'terminal': False, 'project': str(self.project),
                  'journalPath': self.decision['journalPath'],
                  'installId': self.decision['installId']}
        exc = dispatch.DispatchFailure('target_child_failed', detail='exit 1\n' + SENTINEL)
        exc.child_stderr_available = True
        with patch.object(routes_route_selection, 'select_route', return_value=self.decision), \
             patch.object(dispatch, 'resolve', return_value=target), \
             patch.object(dispatch, '_run_target', side_effect=exc):
            with self.assertRaises(dispatch.DispatchFailure):
                dispatch.run('laya-sync', str(self.project))
        rows = event_log.read(self.state)
        row = next(r for r in rows if r.get('event') == 'wake.sync')
        self.assertEqual(row.get('trace_unavailable'), 'child_process')
        self.assertNotIn(SENTINEL, json.dumps(rows))
        detail = diag.reassemble(rows, row.get('diagnostic_id'))
        self.assertEqual(detail['status'], 'complete')
        self.assertEqual(detail['kind'], 'child_stderr_tail')
        self.assertIn(SENTINEL, detail['text'])
        self.assertLessEqual(len(detail['text'].encode('utf-8')), 4096)

    def test_child_tail_limit_and_disabled_setting(self):
        text = '가' * 3000 + SENTINEL
        ident = diag.capture(self.decision, event='wake.sync', reason='target_child_failed',
                             child_stderr=text)
        result = diag.reassemble(event_log.read(self.state), ident)
        self.assertEqual(result['kind'], 'child_stderr_tail')
        self.assertTrue(result['truncated'])
        self.assertIn(SENTINEL, result['text'])
        self.assertLessEqual(len(result['text'].encode('utf-8')), 4096)
        self.enable(False)
        self.assertIsNone(diag.capture(self.decision, event='wake.sync',
                                       reason='target_child_failed', child_stderr='OFF-SENTINEL'))
        self.assertNotIn('OFF-SENTINEL', json.dumps(event_log.read(self.state)))

    def test_diagnostic_failure_does_not_replace_original_exception(self):
        target = {'terminal': False, 'project': str(self.project),
                  'journalPath': self.decision['journalPath'],
                  'installId': self.decision['installId']}
        exc = ValueError(SENTINEL)
        with patch.object(routes_route_selection, 'select_route', return_value=self.decision), \
             patch.object(dispatch, 'resolve', return_value=target), \
             patch.object(dispatch, '_run_target', side_effect=exc), \
             patch.object(diag, 'capture', side_effect=RuntimeError('synthetic logger failure')):
            with self.assertRaises(ValueError) as caught:
                dispatch.run('laya-sync', str(self.project))
        self.assertIs(caught.exception, exc)


if __name__ == '__main__':
    unittest.main(verbosity=2)
