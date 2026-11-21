"""1.0.23: rotating retention of closed cases in a default-teacher namespace.

Rows, jobs and cycles are real tables of the worker fixture; jobs come from
the real ``LearningQueue.plan``/``complete``. Every assertion is a row count
or a set of request ids.
"""
from __future__ import annotations

import json
import time
from unittest import mock

from memvane.learning.context_learning import case_rotation, prequential, shared
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.learning_queue import LearningQueue
from memvane.learning.context_learning.store import canonical
from memvane.learning.tests.fake_teacher_cli_121 import edit_sample
from memvane.learning.tests.integration.test_worker_teacher_121 import WorkerFixture


class RotationFixture(WorkerFixture):
    def setUp(self):
        super().setUp()
        self.clock = 1000.0
        self.queue = LearningQueue(self.root)

    def fill(self, prefix, count, status, *, use_state='active', text=None, key=None, gold='valid'):
        """Insert ``count`` cases in capture order; returns their request ids."""
        ids = []
        with shared._db(self.root) as db:
            for index in range(count):
                rid = f'{prefix}-{index:04d}'
                sample, bodies = edit_sample(rid, **({'text': text} if text else {}))
                encoded = canonical(sample)
                value = None
                if status == 'gold':
                    value = (canonical({'selected_ids': [sample['candidates'][0]['candidate_id']],
                                        'input_sha256': digest(encoded), 'review': 'single_pass'})
                             if gold == 'valid' else gold)
                self.clock += 1
                db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,'
                           'bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status,gold) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (key or shared._project_key(self.project), str(self.project), rid,
                            digest(encoded), encoded, canonical(bodies), 'corpus', self.clock, 'train',
                            '[]', use_state, status, value))
                ids.append(rid)
        return ids

    def gold_rows(self, ids=None):
        with shared._db(self.root) as db:
            rows = prequential.training_rows(db, self.project, self.grant)
        return rows if ids is None else [row for row in rows if row[1] in set(ids)]

    def job(self, ids, *, approval='approval-a', maximum=128, complete=True, cycle=None,
            recent_replay=False):
        rows = self.gold_rows(ids)
        job = self.queue.plan(rows, approval=approval, cohort='default-v1', family='family-a',
                              minimum=2, maximum=maximum, roles=['train'] * len(rows),
                              cumulative=True, recent_replay=recent_replay)
        if complete:
            self.queue.complete(job['id'])
        if cycle is not None:
            with shared._db(self.root) as db:
                db.execute('INSERT INTO cycles VALUES (?,?,?,?,NULL)', (job['id'], cycle, '{}', time.time()))
        return self.queue.get(job['id'])

    def ids(self):
        with shared._db(self.root) as db:
            return {row[0] for row in db.execute('SELECT request_id FROM cases')}

    def count(self):
        with shared._db(self.root) as db:
            return db.execute('SELECT COUNT(*) FROM cases').fetchone()[0]

    def rotations(self):
        return [row for row in self.recorded if row['event'] == 'learning.rotation']

    def rotate(self, **options):
        return case_rotation.rotate(self.root, self.project, self.grant, **options)


class FullTable(RotationFixture):
    """1,900 rows: the 100 oldest closed cases go, every protected set stays."""

    def build(self):
        self.waiting = self.fill('q', 20, 'queued') + self.fill('f', 5, 'first_done')
        self.old_review = self.fill('po', 30, 'pending_review')
        self.old_history = self.fill('ho', 30, 'queued', use_state='historical')
        self.consumed = self.fill('c', 384, 'gold')
        self.review = self.fill('p', 1090, 'pending_review')
        self.history = self.fill('h', 181, 'queued', use_state='historical')
        self.members = self.fill('m', 100, 'gold')
        for start in (0, 128, 256):
            self.job(self.consumed[start:start + 128], cycle='applied')
        # The open candidate: 100 fresh members and the 28 oldest consumed cases (replay).
        self.candidate = self.job(self.consumed + self.members, cycle='staged')
        self.evaluation = self.fill('e', 60, 'gold')
        self.assertEqual(self.count(), 1900)
        member_ids = {m['request_id'] for m in self.candidate['members']}
        self.assertEqual(member_ids, set(self.members) | set(self.consumed[:28]))
        return member_ids

    def test_oldest_closed_cases_go_and_every_protected_set_stays(self):
        member_ids = self.build()
        before_rows = self.queue.rows(self.candidate, self.gold_rows())
        before_split = [row[1] for row in prequential.split(self.gold_rows(), self.candidate)]
        self.assertEqual(before_split, self.evaluation)
        before = self.ids()
        result = self.rotate()
        self.assertEqual((result['status'], result['count'], result['caseRows']), ('deleted', 100, 1800))
        self.assertEqual(self.count(), 1800)
        gone = before - self.ids()
        # Oldest first among the closed cases; the 28 oldest consumed cases are members.
        self.assertEqual(gone, set(self.old_review) | set(self.old_history) | set(self.consumed[28:68]))
        left = self.ids()
        self.assertLessEqual(set(self.waiting), left)                       # not closed
        self.assertLessEqual(member_ids, left)                              # open candidate's members
        self.assertLessEqual(set(self.evaluation), left)                    # its evaluation cases
        self.assertLessEqual(set(self.consumed[-28:]) | set(self.members), left)  # newest 128 consumed
        self.assertEqual(self.queue.rows(self.candidate, self.gold_rows()), before_rows)
        self.assertEqual([row[1] for row in prequential.split(self.gold_rows(), self.candidate)], before_split)
        self.assertEqual([(row['outcome'], row['count'], row['caseRows'], row['limit'])
                          for row in self.rotations()], [('deleted', 100, 1800, 1800)])
        # Re-entry: nothing more goes and no second event.
        again = self.rotate()
        self.assertEqual((again['status'], again['count']), ('within_limit', 0))
        self.assertEqual(self.ids(), left)
        self.assertEqual(len(self.rotations()), 1)

    def test_a_changed_row_rolls_the_whole_delete_back(self):
        self.build()
        before = self.ids()
        real = case_rotation._plan

        def stale(*args):
            doomed, protected = real(*args)
            # The last planned row is no longer what the plan saw.
            return doomed[:-1] + [(doomed[-1][0], 'queued', 'active')], protected
        with mock.patch.object(case_rotation, '_plan', side_effect=stale):
            with self.assertRaisesRegex(ValueError, 'case_rotation_changed'):
                self.rotate()
        self.assertEqual(self.ids(), before)
        with mock.patch.object(case_rotation, '_plan', side_effect=stale):
            self.assertEqual(case_rotation.rotate_quietly(self.root, self.project, self.grant),
                             {'status': 'rotation_deferred', 'count': 0, 'reason': 'case_rotation_changed'})
        self.assertEqual(self.ids(), before)
        self.assertEqual([(row['outcome'], row['reason']) for row in self.rotations()],
                         [('deferred', 'case_rotation_changed')])

    def test_an_interruption_after_the_deletes_leaves_every_row(self):
        self.build()
        before = self.ids()
        real = prequential._has

        def interrupted(db, table):
            if table == 'prequential_selection':
                raise RuntimeError('synthetic interruption')
            return real(db, table)
        with mock.patch.object(prequential, '_has', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'synthetic interruption'):
                self.rotate()
        self.assertEqual(self.ids(), before)
        self.assertEqual(self.rotate()['count'], 100)
        self.assertEqual(self.count(), 1800)

    def test_worker_pass_rotates_before_the_teacher_and_the_cycle(self):
        self.build()
        self.teacher.side_effect = None
        self.teacher.return_value = {'status': 'drained', 'outcomes': [], 'hosts': {},
                                     'quotaRemaining': None, 'calls': 0}
        self.training.side_effect = None
        self.training.return_value = {'status': 'waiting_fresh_batch', 'threshold': 5}
        seen = []
        self.teacher.side_effect = lambda *a, **k: seen.append(self.count()) or self.teacher.return_value
        self.worker()
        self.assertEqual(seen, [1800])
        self.assertEqual(self.count(), 1800)
        self.assertEqual(len(self.rotations()), 1)


class Protected(RotationFixture):
    """Small tables with ``WINDOW`` 1 and an explicit limit: one rule each."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(case_rotation, 'WINDOW', 1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_within_the_limit_nothing_is_read_or_deleted(self):
        self.fill('p', 5, 'pending_review')
        with mock.patch.object(case_rotation, '_plan') as plan:
            self.assertEqual(self.rotate(limit=5), {'status': 'within_limit', 'count': 0, 'caseRows': 5})
        plan.assert_not_called()
        self.assertEqual(self.rotations(), [])

    def test_protected_rows_above_the_limit_stay_and_only_closed_ones_go(self):
        waiting = self.fill('q', 15, 'queued')
        closed = self.fill('p', 5, 'pending_review')
        result = self.rotate(limit=10)
        self.assertEqual((result['status'], result['count'], result['caseRows']), ('deleted', 5, 15))
        self.assertEqual(self.ids(), set(waiting))
        self.assertEqual(self.rotate(limit=10)['status'], 'nothing_closed')
        self.assertEqual(self.ids(), set(waiting))
        self.assertEqual([(row['outcome'], row['count'], row['caseRows']) for row in self.rotations()],
                         [('deleted', 5, 15), ('nothing_closed', 0, 15)])
        self.assertEqual(closed, sorted(closed))

    def test_untrained_job_members_and_its_evaluation_cases_stay(self):
        gold = self.fill('g', 15, 'gold')
        self.job(gold[:5], maximum=5, cycle='applied')                      # consumes g0-4
        queued = self.job(gold[:10], maximum=8, complete=False)             # g5-9 fresh, g0-2 replay
        self.assertEqual(queued['status'], 'queued')
        newest = self.job(gold[10:], approval='approval-b', maximum=5, cycle='applied')
        self.assertEqual({m['request_id'] for m in newest['members']}, set(gold[10:]))
        self.assertEqual({m['request_id'] for m in queued['members']}, set(gold[5:10]) | set(gold[:3]))
        result = self.rotate(limit=5)
        # Only g3 and g4: consumed, in no live job and outside the window.
        self.assertEqual(result['count'], 2)
        self.assertEqual(self.ids(), set(gold) - {gold[3], gold[4]})
        self.assertEqual(self.queue.rows(queued, self.gold_rows()),
                         self.gold_rows(gold[5:10]) + self.gold_rows(gold[:3]))

    def test_active_checkpoint_and_journal_cycles_keep_their_members(self):
        from memvane.learning.context_learning import candidate_apply
        gold = self.fill('g', 12, 'gold')
        first = self.job(gold[:4], maximum=4, cycle='applied')
        second = self.job(gold[4:8], approval='approval-b', maximum=4, cycle='applied')
        self.job(gold[8:], approval='approval-c', maximum=4, cycle='applied')
        pointer = json.loads((self.root / 'active-checkpoint.json').read_text())
        self.write_json(self.root / 'active-checkpoint.json', dict(pointer, cycle_id=first['id']))
        self.write_json(self.root / candidate_apply.JOURNAL, {'cycle_id': second['id'], 'phase': 'activated'})
        self.assertEqual(self.rotate(limit=2)['status'], 'nothing_closed')
        self.assertEqual(self.ids(), set(gold))
        (self.root / candidate_apply.JOURNAL).unlink()
        self.assertEqual(self.rotate(limit=2)['count'], 4)
        self.assertEqual(self.ids(), set(gold) - set(gold[4:8]))
        # An unreadable state file: the protected set is unknown, nothing goes.
        (self.root / 'active-checkpoint.json').write_text('{')
        self.assertEqual(case_rotation.rotate_quietly(self.root, self.project, self.grant)['status'],
                         'within_limit')
        with mock.patch.object(case_rotation, 'ROTATE_LIMIT', 2):
            self.assertEqual(case_rotation.rotate_quietly(self.root, self.project, self.grant)['status'],
                             'rotation_deferred')
        self.assertEqual(self.ids(), set(gold) - set(gold[4:8]))

    def test_unconsumed_and_repeated_gold(self):
        gold = self.fill('g', 4, 'gold', text='the same edit text')         # one semantic key
        other = self.fill('u', 3, 'gold')
        job = self.job(gold + other[:1], maximum=4, cycle='applied')
        self.assertEqual({m['request_id'] for m in job['members']}, {gold[0], other[0]})
        self.job(other[1:2] + self.fill('x', 1, 'gold'), approval='approval-b', maximum=4, cycle='applied')
        last = self.fill('z', 2, 'gold')                                     # never in a job
        result = self.rotate(limit=1)
        # The repeats of a consumed case go with it; unconsumed gold stays whatever its age,
        # and the newest job keeps its members.
        self.assertEqual(result['count'], 5)
        self.assertEqual(self.ids(), {other[1], other[2], 'x-0000'} | set(last))

    def test_only_closed_states_of_this_project_go(self):
        foreign = self.fill('other', 2, 'pending_review', key='f' * 64)
        kept = (self.fill('bad', 1, 'gold', gold='{"selected_ids":"x"}')          # active, not admitted
                + self.fill('hf', 1, 'first_done', use_state='historical')
                + self.fill('q', 1, 'queued'))
        gone = (self.fill('hg', 1, 'gold', use_state='historical')
                + self.fill('hq', 1, 'queued', use_state='historical')
                + self.fill('p', 1, 'pending_review'))
        with shared._db(self.root) as db:
            prequential._selection_table(db)
            db.executemany('INSERT INTO prequential_selection VALUES (?,?,?,?)',
                           [(shared._project_key(self.project), rid, 'sha', 'selector') for rid in gone + kept])
        result = self.rotate(limit=0)
        self.assertEqual(result['count'], 3)
        self.assertEqual(self.ids(), set(foreign) | set(kept))
        with shared._db(self.root) as db:
            self.assertEqual({row[0] for row in db.execute('SELECT request_id FROM prequential_selection')},
                             set(kept))

    def test_another_profile_is_not_rotated(self):
        self.fill('p', 5, 'pending_review')
        grant = dict(self.grant, operations=['local_train', 'quality_evaluate'])
        self.assertEqual(case_rotation.rotate(self.root, self.project, grant, limit=0),
                         {'status': 'not_applicable', 'count': 0})
        self.assertEqual(self.count(), 5)
