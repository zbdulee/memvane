"""Every status a worker pass can end with is classified (design L-6).

The statuses are read from the source of the cycle path: every ``status``
literal a result dict is built with. None may be missing from
``state_model.CYCLE_RESULTS``, the ``retry`` class is exactly the part of the
runtime's retry set the cycle returns, and the worker's ``HELD`` tuple is the
``held`` class. A held result writes one ``learning.cycle`` row when a pass
first ends with it.
"""
from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import json

from memvane.learning.context_learning import project_worker, state_model, worker_retry
from memvane.learning.context_learning.state_model import CYCLE_RESULTS, PASS_ENDS
from memvane.runtime.types import RETRY

SOURCE = Path(project_worker.__file__).resolve().parent
# Literals that are a protocol answer or an inner record, never a pass result.
NOT_RESULTS = {
    'trained',                                   # the trainer's answer
    'durable_wake_pending',                      # the retry receipt of a result
    'not_applicable_default_teacher', 'not_applicable_local_only',      # the quota part
    'disabled_local_only', 'disabled_synthetic_local_only', 'teacher_failed',  # the teacher part
}
# Results of the teacher step that end a pass before the cycle (their own tests: M1, M2, M15).
TEACHER_STEP = {'consent_required'}
# A dict literal's status, or the keyword of ``dict(result, status=...)`` (not SQL text,
# not a telemetry argument on its own line).
STATUS = re.compile(r"""['"]status['"]\s*:\s*['"]([a-z_]+)['"]|(?<=[,(])status=['"]([a-z_]+)['"]""")
ALTERNATIVE = re.compile(r"""else\s+['"]([a-z_]+)['"]""")


def statuses(name, start=None):
    """Status literals of result dicts in ``name`` (from ``start`` on), with the other arm of a conditional."""
    text = (SOURCE / name).read_text(encoding='utf-8')
    if start:
        text = text[text.index(start):]
    found = set()
    for line in text.splitlines():
        hits = [a or b for a, b in STATUS.findall(line)]
        found.update(hits)
        if hits and "'status'" in line:
            found.update(ALTERNATIVE.findall(line.split("'status'", 1)[1].split(',', 1)[0]))
    return found


class CycleResults(unittest.TestCase):
    def found(self):
        return (statuses('automatic_staging.py') | statuses('candidate_apply.py')
                | statuses('shared_run.py', 'def run_training_pending') | statuses('shared.py', 'def _profile_apply')
                | statuses('project_worker.py')) - NOT_RESULTS

    def test_no_pass_result_is_unclassified(self):
        found = self.found()
        self.assertEqual(sorted(found - set(CYCLE_RESULTS) - TEACHER_STEP), [])
        # And nothing is classified that the source no longer returns.
        text = ''.join((SOURCE / name).read_text(encoding='utf-8')
                       for name in ('automatic_staging.py', 'candidate_apply.py', 'shared.py', 'shared_run.py',
                                    'project_worker.py'))
        self.assertEqual([status for status in CYCLE_RESULTS if "'%s'" % status not in text], [])
        self.assertGreaterEqual(len(found), 25)

    def test_the_ends_that_are_not_cycle_results_are_listed_with_where_they_are_made(self):
        # W14 review F-3: a raised pass, the owned worker without a request, the synthetic budget.
        for name, (status, source, leaves) in PASS_ENDS.items():
            text = (SOURCE / source).read_text(encoding='utf-8')
            self.assertRegex(text, r"""['"]status['"]\s*:\s*['"]%s['"]""" % status, name)
            self.assertTrue(leaves.startswith('nothing'), name)
        # Only the raised pass shares its status with a cycle result (a failed training attempt).
        self.assertEqual({row[0] for row in PASS_ENDS.values()} & set(CYCLE_RESULTS), {'failed'})

    def test_a_deferred_evaluation_is_waiting_not_closed(self):
        # W14 review F-8: nothing is closed by these two; a later pass evaluates again.
        for status in ('staged_cohort_changed', 'staged_authorization_expired'):
            self.assertEqual(CYCLE_RESULTS[status][:2], ('waiting', None))

    def test_every_event_of_the_cycle_machines_is_classified_as_progress_or_not(self):
        # Review 10 r2 (W15 F-13): one definition; a new event fails here until it is classified.
        for machine in state_model.CYCLE_MACHINES:
            forward, other = state_model.PROGRESS[machine.name], state_model.NOT_PROGRESS[machine.name]
            self.assertEqual((forward | other, forward & other), (set(machine.events), set()), machine.name)

    def transitions(self):
        return [(machine, state, event, target) for machine in state_model.CYCLE_MACHINES
                for (state, event), target in sorted(machine.transitions.items())]

    def test_the_products_forward_table_is_the_one_the_model_derives(self):
        derived = {}
        for machine in state_model.CYCLE_MACHINES:
            if machine.name == 'M7':  # Kept as the pointer's digest: any change is one of its two events.
                self.assertEqual(state_model.NOT_PROGRESS['M7'], set())
                continue
            for old, new in state_model.forward_pairs(machine):
                derived.setdefault(machine.name, {}).setdefault(new, set()).add(old)
        self.assertEqual({name: {new: set(olds) for new, olds in table.items()}
                          for name, table in worker_retry.FORWARD.items()}, derived)
        self.assertEqual(worker_retry.ABSENT, state_model.ABSENT)

    def test_each_transition_moves_the_state_exactly_when_its_event_is_progress(self):
        for machine, state, event, target in self.transitions():
            if machine.name == 'M7':
                continue
            moved = worker_retry.moved({machine.name: {'row': state}}, {machine.name: {'row': target}})
            self.assertEqual(moved, event in state_model.PROGRESS[machine.name], (machine.name, state, event, target))
        # Several steps in one pass, a change the machine does not model, the pointer and saved groups.
        for name, old, new, moved in (('M4', 'training', 'applied', True), ('M4', 'staged_evaluated', 'staged', False),
                                      ('M6', state_model.ABSENT, 'prepared', False),
                                      ('M6', state_model.ABSENT, 'activated', True), ('M5', 'queued', 'unknown', False),
                                      ('M7', 'a' * 64, 'b' * 64, True), ('M7', 'a' * 64, 'a' * 64, False)):
            key = 'pointer' if name == 'M7' else 'row'
            self.assertEqual(worker_retry.moved({name: {key: old}}, {name: {key: new}}), moved, (name, old, new))
        for old, new, moved in ((3, 4, True), (3, 3, False), ('new', 0, True), (4, -1, False),
                                (None, 4, False), (3, None, False)):  # None: the cursor could not be read
            before = {'groups': {} if old == 'new' else {'c': old}}
            self.assertEqual(worker_retry.moved(before, {'groups': {'c': new}}), moved, (old, new))

    def test_after_287_retries_each_transition_keeps_or_settles_the_request_by_its_class(self):
        limit = worker_retry.RETRY_RESULT_LIMIT
        self.assertEqual(limit, 288)
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch('memvane.diagnostics.api.emit') as emitted, \
                mock.patch('memvane.journal.api.select_route', return_value={}):
            root = Path(folder)
            counted = 0
            for machine, state, event, target in self.transitions():
                before, after = {machine.name: {'row': state}}, {machine.name: {'row': target}}
                if machine.name == 'M7':  # The pointer is kept as itself: any change is one of M7's two events.
                    before, after = {'M7': {'pointer': 'a' * 64}}, {'M7': {'pointer': 'b' * 64}}
                (root / worker_retry.NAME).write_text(json.dumps(
                    {'schema': 1, 'status': 'busy', 'count': limit - 1, 'state': before}))
                (root / worker_retry.NAME).chmod(0o600)
                emitted.reset_mock()
                with mock.patch.object(worker_retry, 'snapshot', return_value=after):
                    kept = worker_retry.again(root, root, 'apply_deferred', True)
                progress = event in state_model.PROGRESS[machine.name]
                # Progress: the next wake is asked for and the count is one. Otherwise: settled, one row.
                self.assertEqual((kept, emitted.call_count), (progress, 0 if progress else 1),
                                 (machine.name, state, event, target))
                if progress:
                    self.assertEqual(json.loads((root / worker_retry.NAME).read_text())['count'], 1)
                counted += 1
            self.assertEqual(counted, sum(len(machine.transitions) for machine in state_model.CYCLE_MACHINES))

    def test_the_classes_are_the_known_five_and_every_row_says_what_stays(self):
        self.assertEqual({row[0] for row in CYCLE_RESULTS.values()},
                         {'retry', 'waiting', 'done', 'closed', 'held'})
        for status, (_kind, event, leaves) in CYCLE_RESULTS.items():
            self.assertTrue(leaves, status)
            self.assertTrue(event is None or event.startswith('learning.'), status)

    def test_the_retry_class_is_the_runtime_retry_set(self):
        retried = {status for status, row in CYCLE_RESULTS.items() if row[0] == 'retry'}
        self.assertEqual(retried, RETRY & set(CYCLE_RESULTS))
        # ``setup_required`` is a selector maintenance result, not a pass result.
        self.assertEqual(RETRY - retried, {'setup_required'})

    def test_the_held_class_is_what_the_worker_reports_and_each_has_its_event(self):
        held = {status for status, row in CYCLE_RESULTS.items() if row[0] == 'held'}
        self.assertEqual(held, set(project_worker.HELD))
        self.assertEqual({CYCLE_RESULTS[status][1] for status in held}, {'learning.cycle held'})
        # The four results the audit found unclassified (A-18).
        self.assertLessEqual({'staged_artifact_changed', 'cohort_cycle_conflict',
                              'evaluation_stage_unavailable', 'experiment_paused'}, held)

    def test_a_held_result_is_written_once_until_the_result_changes(self):
        rows = []
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch('memvane.diagnostics.api.emit', side_effect=lambda event, **f: rows.append((event, f))), \
                mock.patch('memvane.journal.api.select_route', return_value={}):
            root = Path(folder)
            status = root / 'project-last-status.json'
            cycle = {'status': 'staged_artifact_changed', 'cycle_id': 'cohort-' + 'a' * 20}
            project_worker._record_held(root, root, cycle)
            self.assertEqual([(event, f['outcome'], f['reason'], f['cycle']) for event, f in rows],
                             [('learning.cycle', 'held', 'staged_artifact_changed', cycle['cycle_id'])])
            status.write_text('{"status": "staged_artifact_changed"}')
            status.chmod(0o600)
            project_worker._record_held(root, root, cycle)
            self.assertEqual(len(rows), 1)
            project_worker._record_held(root, root, {'status': 'experiment_paused', 'reason': 'x' * 200})
            self.assertEqual((len(rows), rows[-1][1]['reason'], len(rows[-1][1]['detail'])),
                             (2, 'experiment_paused', 80))


if __name__ == '__main__':
    unittest.main()
