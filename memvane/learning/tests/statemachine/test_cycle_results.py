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

from memvane.learning.context_learning import project_worker
from memvane.learning.context_learning.state_model import CYCLE_RESULTS
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
                | statuses('shared.py', 'def run_training_pending') | statuses('project_worker.py')) - NOT_RESULTS

    def test_no_pass_result_is_unclassified(self):
        found = self.found()
        self.assertEqual(sorted(found - set(CYCLE_RESULTS) - TEACHER_STEP), [])
        # And nothing is classified that the source no longer returns.
        text = ''.join((SOURCE / name).read_text(encoding='utf-8')
                       for name in ('automatic_staging.py', 'candidate_apply.py', 'shared.py', 'project_worker.py'))
        self.assertEqual([status for status in CYCLE_RESULTS if "'%s'" % status not in text], [])
        self.assertGreaterEqual(len(found), 25)

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
