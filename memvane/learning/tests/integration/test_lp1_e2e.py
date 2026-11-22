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

from memvane.learning.context_learning import shared
from memvane.learning.tests.lp1 import Lp1Fixture
from memvane.learning.tests.old_bundle_121 import ROOT
from memvane.learning.tests.support import invariants

EDITS = 160
# The three releases before this one (``old_bundle_121.TAGS``).
PREVIOUS = ('v1.0.22', 'v1.0.23', 'v1.0.24')
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
        # The capture's launch left the worker request the wake dispatcher reads.
        request = shared._full_worker_request(self.root)
        self.assertEqual((request['schema'], sorted(request)), (shared.FULL_WORKER_SCHEMA,
                                                               ['afterIndex', 'job', 'nonce', 'schema']))
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
        # The next pass runs the way the wake dispatcher runs it: the worker step starts the
        # owned worker child on the request, the child runs the pass under the inherited
        # lease and settles the request. It asks no one and pins the next job from all
        # gold; the pointer stays.
        step, codes, state = self.owned_pass(1)
        self.assertEqual((step, codes), ({'status': 'started', 'mode': 'full', 'starts': 1}, [0]))
        result = json.loads((self.root / 'project-last-status.json').read_text())
        self.assertEqual((result['teacher']['calls'], self.pointer()), (0, applied))
        self.assertEqual(self.counts()['cycles'], {'applied': 1, 'staged': 1})
        # The child trained the next job: the newest 128 gold cases (the batch maximum).
        self.assertEqual(self.child_calls('trained'), [128])
        # The child's result is the request's outcome, and the next step acknowledges it.
        self.assertEqual((state['nonce'], state['outcome'], state['acked']),
                         (request['nonce'], result['cycle']['status'], False))
        self.assertEqual(shared.learning_worker_step(self.root, self.project, 2),
                         {'status': 'settled', 'outcome': result['cycle']['status']})
        self.assertEqual(invariants.check(self.root), [])
        self.old_workers_meet_the_open_job(with_job[0])

    def old_workers_meet_the_open_job(self, applied_job):
        """The three previous releases' workers on this bundle's open job states (design L-5).

        The namespace holds 160 real gold cases, so an old worker passes its first gate
        (enough training cases) and reads the job, the cycle and the evaluation. The second
        job is put in each state this bundle leaves open; every old worker answers from
        that row and changes no row of any table, and starts no host CLI.
        """
        import time
        from memvane.learning.tests.old_bundle_121 import old_bundle
        second, = [job for job in self.jobs() if job != applied_job]
        self.keep()
        later = time.time() + 86400
        # state -> ((job status, reason, attempts, failures, retry_at, cycle status) or None, the old worker's answer)
        states = {'staged, its evaluation not run': (None, 'staged_evaluation_deferred'),
                  'blocked, waiting to be rearmed': (('blocked', 'trainer_failed', 3, 3, later, 'failed'),
                                                     'learning_job_blocked'),
                  'blocked for good': (('blocked', 'learning_cursor_changed', 1, 1, 0, 'failed'),
                                       'learning_job_blocked'),
                  'deferred after progress': (('deferred', 'trainer_progress', 1, 0, later, 'deferred'),
                                              'deferred_resource')}
        launches = len(self.launches())
        for name, (row, answer) in states.items():
            for tag in PREVIOUS:
                with self.subTest(state=name, tag=tag):
                    self.restore()
                    if row:
                        with shared._db(self.root) as db:
                            db.execute('UPDATE learning_jobs SET status=?,reason=?,attempts=?,failures=?,retry_at=? '
                                       'WHERE job_id=?', (*row[:5], second))
                            db.execute('UPDATE cycles SET status=? WHERE cycle_id=?', (row[5], second))
                    before = self.rows()
                    code, out, err = self.entry(old_bundle(tag), 'learning-shared', 'run-project', str(self.project),
                                                patched=True, resource_ready=True)
                    self.assertEqual(code, 0, err)
                    result = json.loads(out)
                    self.assertEqual((result['teacher']['calls'], result['cycle']['status'], result['cycle']['cycle_id']),
                                     (0, answer, second), result)
                    self.assertEqual(self.rows(), before)
                    self.assertEqual(len(self.launches()), launches)
        self.restore()
