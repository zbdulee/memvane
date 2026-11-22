"""LP-1: 160 edits become one applied candidate, through real entry points only.

Edits enter through ``hooks/run-hook posttool``; every pass is the worker
command (``memvane/learning/tests/lp1.py``). The table below fixes what each
pass leaves: counts of case, job and cycle rows, the teacher calls and the
cycle result. The invariants hold after every pass. A pass asks the teacher
about eight cases, so the first job is pinned when the gold reaches 104 and
the evaluation opens when 56 later gold cases exist (at least 50 are needed);
150 edits would stop at 46.
"""
from __future__ import annotations

import json

from memvane.learning.tests.lp1 import Lp1Fixture
from memvane.learning.tests.old_bundle_121 import ROOT
from memvane.learning.tests.support import invariants

EDITS = 160
PER_PASS = 8
TRAINED_AT, APPLIED_AT = 13, 20


def expected(number):
    """What pass ``number`` leaves: (cases, jobs, cycles, cycle result)."""
    gold = min(EDITS, PER_PASS * number)
    cases = {'gold': gold, **({'queued': EDITS - gold} if gold < EDITS else {})}
    if number < TRAINED_AT:
        return cases, {}, {}, 'waiting_fresh_train'
    if number < APPLIED_AT:
        return cases, {'staged': 1}, {'staged': 1}, 'staged_waiting_quality_cohort'
    return cases, {'staged': 1}, {'applied': 1}, 'applied'


class LearningPath(Lp1Fixture):
    def test_edits_are_captured_labelled_trained_evaluated_and_applied(self):
        # Every fifth answer is an empty selection: the evaluation needs both kinds.
        (self.logs / 'claude-queue.json').write_text(json.dumps(
            [{'answer': 'empty'} if index % 5 == 0 else {} for index in range(EDITS)]))
        self.assertEqual(self.edit(EDITS), ['stored'] * EDITS)
        self.assertEqual(self.counts(), {'cases': {'queued': EDITS}, 'jobs': {}, 'cycles': {}})
        base = self.pointer()
        for number in range(1, APPLIED_AT + 1):
            with self.subTest(pass_number=number):
                result = self.run_project()
                cases, jobs, cycles, status = expected(number)
                self.assertEqual(result['teacher']['calls'], PER_PASS, result['teacher'])
                self.assertEqual(result['cycle']['status'], status, result['cycle'])
                self.assertEqual(self.counts(), {'cases': cases, 'jobs': jobs, 'cycles': cycles})
                self.assertEqual(invariants.check(self.root), [])
                self.assertEqual(self.pointer() == base, number < APPLIED_AT)
        # One training of the 104 gold cases of pass 13, one evaluation of the 56 later ones.
        self.assertEqual((self.trained, self.predicted), ([PER_PASS * TRAINED_AT], [56, 56]))
        applied = self.pointer()
        with_job = self.jobs()
        self.assertEqual((applied['cycle_id'], len(with_job)), (with_job[0], 1))
        self.assertTrue(applied['artifact_path'].endswith('/cycles/%s/candidate.artifact' % with_job[0]))
        self.assertEqual(result['cycle']['artifactSha256'], applied['artifact_sha256'])
        # The host CLI was started once per case and no more.
        launches = [row for row in self.launches() if row['stage'] == 'launch']
        self.assertEqual(len(launches), EDITS)
        # A recall after the application answers (no resident runs in this fixture, so by rank).
        code, out, err = self.hook(ROOT, 'recall', {'hook_event_name': 'UserPromptSubmit',
                                                     'session_id': 'lp1-recall',
                                                     'prompt': 'Which marker does the emerald heron keep?'})
        self.assertEqual(code, 0, err)
        self.assertIn('zqheronx', json.loads(out)['hookSpecificOutput']['additionalContext'])
        # The next pass asks no one and pins the next job from all gold; the pointer stays.
        result = self.run_project()
        self.assertEqual((result['teacher']['calls'], self.pointer()), (0, applied))
        self.assertEqual(self.counts()['cycles'], {'applied': 1, 'staged': 1})
        self.assertEqual(invariants.check(self.root), [])
