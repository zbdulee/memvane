"""Synthetic RED checks: bundle-upgrade worker and Hermes recall diagnostic boundaries.

Everything runs under an isolated temporary HOME; route selection, the upgrade
itself and the selected recall are mocked, and a synthetic ValueError is
injected at the real boundary. Only observable outcomes are asserted: the
returned result / user-visible refusal and the private event-log rows.
"""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'core'))
sys.path.insert(0, str(REPO))
from memvane.diagnostics import diagnostic_trace as diag
from memvane.diagnostics import event_log
import laya_bundle_upgrade as upgrade
import laya_host_response as host_response
from memvane.route import index_host_route as routes
import laya_project_dir_move
import laya_state_move
from hermes_adapter import core_bridge

SENTINEL = 'synthetic-boundary-exception-417'
REFUSAL = '[memvane] selected proof or query failed; request refused'


class Base(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.environ.get('QMD_SETUP_GUARD_FIXTURE'), '1')
        home = Path(os.environ['HOME']).resolve()
        self.assertIn(Path(tempfile.gettempdir()).resolve(), home.parents)
        tmp = tempfile.TemporaryDirectory(dir=str(home))
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = home
        self.project, self.state, self.decision = self.install('project', 'state',
                                                               'synthetic-install')

    def install(self, name, state_name, install_id):
        project = self.base / name
        (project / '.memvane').mkdir(parents=True)
        state = self.base / state_name
        state.mkdir(mode=0o700)
        decision = {'state': 'candidate', 'projectRoot': str(project),
                    'journalPath': str(state / 'install-journal.sqlite'),
                    'installId': install_id}
        self.enable(project, True)
        return project, state, decision

    def enable(self, project, value):
        (project / '.memvane' / 'settings.json').write_text(
            json.dumps({'name': 'synthetic', 'diagnostics': {'enabled': value}}))

    def rows(self, state=None):
        return event_log.read(state or self.state)

    def headers(self, rows):
        return [r for r in rows if r.get('event') == diag.HEADER_EVENT]

    def reset_log(self, state=None):
        path = (state or self.state) / event_log.NAME
        if path.exists():
            path.unlink()


class BundleUpgrade(Base):
    """``laya_bundle_upgrade.run`` failure boundary."""

    def run_failing(self, selected, *, capture_error=False):
        exc = ValueError(SENTINEL)
        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(patch.object(upgrade, '_project_states',
                                             return_value=[self.state]))
            stack.enter_context(patch.object(upgrade, 'read_status', return_value=None))
            stack.enter_context(patch.object(upgrade, '_write_status'))
            stack.enter_context(patch.object(upgrade, '_bound_bundle',
                                             return_value=(None, None)))
            stack.enter_context(patch.object(upgrade, '_host', return_value='synthetic'))
            stack.enter_context(patch.object(upgrade, '_live_state',
                                             side_effect=lambda s, h: s))
            stack.enter_context(patch.object(upgrade, '_upgrade_with_backoff',
                                             side_effect=exc))
            stack.enter_context(patch.object(upgrade, '_collect_shared', return_value=None))
            stack.enter_context(patch.object(upgrade, '_learning_upkeep', return_value=None))
            stack.enter_context(patch.object(laya_state_move, 'intent_present',
                                             return_value=False))
            stack.enter_context(patch.object(laya_project_dir_move, 'intent_present',
                                             return_value=False))
            stack.enter_context(patch.object(laya_project_dir_move, 'needed',
                                             return_value=None))
            stack.enter_context(patch.object(routes, 'select_route', return_value=selected))
            if capture_error:
                stack.enter_context(patch.object(
                    diag, 'capture', side_effect=RuntimeError('synthetic logger failure')))
            results = upgrade.run(self.project, root=REPO, home=self.home)
        return [{k: v for k, v in r.items() if k in ('status', 'reason')}
                for r in results if r.get('stateRoot') == str(self.state)]

    def failed_row(self, rows):
        matching = [r for r in rows if r.get('event') == 'bundle.upgrade'
                    and r.get('outcome') == 'failed']
        self.assertEqual(len(matching), 1, matching)
        return matching[0]

    def baseline(self):
        self.enable(self.project, False)
        outcome = self.run_failing(self.decision)
        self.reset_log()
        self.enable(self.project, True)
        return outcome

    def test_failure_keeps_fixed_outcome_and_links_opt_in_exception(self):
        on = self.run_failing(self.decision)
        rows_on = self.rows()
        self.reset_log()
        self.assertEqual(on, self.baseline())
        self.assertEqual(on[0]['status'], 'failed')
        self.assertNotIn(SENTINEL, json.dumps(on))
        row = self.failed_row(rows_on)
        self.assertNotIn(SENTINEL, json.dumps(row))
        detail = diag.reassemble(rows_on, row.get('diagnostic_id'))
        self.assertEqual(detail['status'], 'complete')
        self.assertEqual(detail['kind'], 'exception')
        self.assertIn('ValueError', detail['text'])
        self.assertIn(SENTINEL, detail['text'])
        self.assertEqual(len(self.headers(rows_on)), 1)

    def test_failure_with_diagnostics_off_omits_raw_exception(self):
        self.enable(self.project, False)
        outcome = self.run_failing(self.decision)
        rows = self.rows()
        row = self.failed_row(rows)
        self.assertNotIn('diagnostic_id', row)
        self.assertEqual(self.headers(rows), [])
        self.assertNotIn(SENTINEL, json.dumps(rows))
        self.assertEqual(outcome[0]['status'], 'failed')

    def test_unproved_selection_marks_trace_unavailable(self):
        expected = self.baseline()
        for selected in ({'state': 'blocked', 'reason': 'synthetic'}, None):
            with self.subTest(selected=selected):
                self.reset_log()
                self.assertEqual(self.run_failing(selected), expected)
                rows = self.rows()
                row = self.failed_row(rows)
                self.assertNotIn('diagnostic_id', row)
                self.assertTrue(row.get('trace_unavailable'))
                self.assertNotIn(SENTINEL, json.dumps(rows))
                self.reset_log()

    def test_selection_of_another_install_is_never_used(self):
        expected = self.baseline()
        other_project, other_state, other = self.install('other', 'other-state',
                                                         'synthetic-other-install')
        self.assertEqual(self.run_failing(other), expected)
        rows = self.rows()
        row = self.failed_row(rows)
        self.assertNotIn('diagnostic_id', row)
        self.assertTrue(row.get('trace_unavailable'))
        self.assertNotIn(SENTINEL, json.dumps(rows))
        self.assertNotIn(SENTINEL, json.dumps(self.rows(other_state)))
        self.assertEqual(self.headers(self.rows(other_state)), [])
        del other_project

    def test_diagnostic_write_failure_keeps_outcome(self):
        expected = self.baseline()
        self.assertEqual(self.run_failing(self.decision, capture_error=True), expected)
        row = self.failed_row(self.rows())
        self.assertNotIn('diagnostic_id', row)
        self.assertNotIn(SENTINEL, json.dumps(self.rows()))


class HermesRecall(Base):
    """``core_bridge.recall_context`` selected-route failure boundary."""

    def recall(self, serve, *, capture_error=False):
        env = {k: v for k, v in os.environ.items() if k != 'MEMVANE_BRIDGE_PREFLIGHT'}
        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            stack.enter_context(patch.object(core_bridge, '_is_noop_env', return_value=False))
            stack.enter_context(patch.object(core_bridge, '_laya_route_state',
                                             return_value='candidate'))
            stack.enter_context(patch.object(routes, 'select_route',
                                             return_value=self.decision))
            stack.enter_context(patch.object(host_response, 'serve_selected',
                                             side_effect=serve))
            if capture_error:
                stack.enter_context(patch.object(
                    diag, 'capture', side_effect=RuntimeError('synthetic logger failure')))
            return core_bridge.recall_context('synthetic prompt', cwd=str(self.project))

    def hermes_rows(self, rows):
        return [r for r in rows if r.get('host') == 'hermes'
                and r.get('event') not in (diag.HEADER_EVENT, diag.PART_EVENT)]

    def test_unlogged_failure_links_host_exception_and_keeps_refusal(self):
        def serve(*_args, **_kwargs):
            raise ValueError(SENTINEL)
        answer = self.recall(serve)
        self.assertEqual(answer, {'context': REFUSAL})
        rows = self.rows()
        linked = [r for r in self.hermes_rows(rows) if r.get('diagnostic_id')]
        self.assertEqual(len(linked), 1, rows)
        self.assertNotIn(SENTINEL, json.dumps(linked[0]))
        detail = diag.reassemble(rows, linked[0]['diagnostic_id'])
        self.assertEqual(detail['status'], 'complete')
        self.assertEqual(detail['kind'], 'exception')
        self.assertIn(SENTINEL, detail['text'])
        self.assertEqual(len(self.headers(rows)), 1)

    def test_diagnostics_off_keeps_refusal_without_raw_exception(self):
        self.enable(self.project, False)

        def serve(*_args, **_kwargs):
            raise ValueError(SENTINEL)
        self.assertEqual(self.recall(serve), {'context': REFUSAL})
        rows = self.rows()
        self.assertEqual(self.headers(rows), [])
        self.assertNotIn(SENTINEL, json.dumps(rows))
        for row in self.hermes_rows(rows):
            self.assertNotIn('diagnostic_id', row)

    def test_already_logged_exception_is_not_captured_twice(self):
        def serve(*_args, **_kwargs):
            exc = ValueError(SENTINEL)
            try:
                raise exc
            except ValueError:
                # The inner boundary records it first, as a real core layer would.
                self.assertIsNotNone(diag.capture(self.decision, event='recall.selected',
                                                  reason='synthetic', exc=exc))
                exc._memvane_event_logged = True
                raise
        self.assertEqual(self.recall(serve), {'context': REFUSAL})
        rows = self.rows()
        self.assertEqual(len(self.headers(rows)), 1, rows)
        ids = {r.get('diagnostic_id') for r in rows if r.get('diagnostic_id')}
        self.assertEqual(len(ids), 1)

    def test_diagnostic_write_failure_keeps_refusal(self):
        def serve(*_args, **_kwargs):
            raise ValueError(SENTINEL)
        self.assertEqual(self.recall(serve, capture_error=True), {'context': REFUSAL})
        self.assertNotIn(SENTINEL, json.dumps(self.rows()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
