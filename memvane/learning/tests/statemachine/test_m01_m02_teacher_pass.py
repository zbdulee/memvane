"""M1 (case) and M2 (gold ledger): every state under every teacher-pass stimulus.

A stimulus is one real ``process_pending_default`` pass made to take one
branch (fake host CLIs, a scope check that defers at a chosen ledger stage).
It names the model events the pass attempts, in order. The expected state is
the model's ``next`` folded over those events from the starting state, so an
event that is not listed for a state must leave the state as it is. The number
of cells run equals states x stimuli, and the events driven are exactly the
model's events (``capture`` and ``rotation`` have their own integration tests).
"""
from __future__ import annotations

import json
from unittest import mock

from memvane.learning.context_learning import general_gold_ledger as ledger
from memvane.learning.context_learning import general_teacher, shared, state_model
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.state_model import ABSENT, M1, M2
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture
from memvane.learning.tests.integration.test_teacher_parked_122 import ParkMixin
from memvane.learning.tests.support import invariants

RID = 'a1'
# The ledger stage each case state is built with.
LEDGER_OF = {ABSENT: ABSENT, 'queued': ABSENT, 'first_running': 'reserved',
             'first_done': 'answer_parked', 'gold': 'committed',
             'pending_review': 'dispatched_uncertain'}
_START = (['pass_start', 'kept_confirmed'], ['recover', 'confirm'])
_SENT = ['reserve', 'before_launch', 'after_launch']
# stimulus -> (M1 events, M2 events) the pass attempts in order.
STIMULI = {
    'answered': (_START[0] + ['claim', 'answer_committed'], _START[1] + _SENT + ['answer', 'commit']),
    'answer_kept': (_START[0] + ['claim', 'answer_kept'], _START[1] + _SENT + ['answer', 'park']),
    'launch_deferred': (_START[0] + ['claim', 'not_sent'], _START[1] + ['reserve', 'settle_not_sent']),
    'not_sent_fifth_time': (_START[0] + ['claim', 'not_sent_exhausted'],
                            _START[1] + ['reserve', 'settle_not_sent']),
    'provider_refused': (_START[0] + ['claim', 'provider_refused'], _START[1] + _SENT + ['settle_refused']),
    'content_refused': (_START[0] + ['claim', 'content_refused'], _START[1] + _SENT + ['settle_refused']),
    'cli_failed': (_START[0] + ['claim', 'uncertain'], _START[1] + _SENT + ['settle_uncertain']),
    'input_over_budget': (_START[0] + ['local_check_failed'], _START[1]),
    'kept_answer_changed': (['pass_start', 'kept_rejected', 'claim', 'answer_committed'],
                            ['recover', 'reject_kept'] + _SENT + ['answer', 'commit']),
    'consent_withdrawn': (['consent_withdrawn'], ['reject_kept']),
}
NOT_DRIVEN = {'capture', 'rotation'}


def fold(machine, state, events):
    for event in events:
        state = machine.next(state, event)
    return state


def fold_pair(case, stage, case_events, ledger_events):
    """(case state, ledger stage) after a pass. The ledger steps that belong to a
    call (from ``reserve`` on) happen only when the pass claims the case."""
    claimed = False
    for event in case_events:
        claimed = claimed or (event == 'claim' and (case, event) in M1.transitions)
        case = M1.next(case, event)
    for event in ledger_events:
        if event in CALL_STEPS and not claimed:
            continue
        stage = M2.next(stage, event)
    return case, stage


CALL_STEPS = {'reserve', 'before_launch', 'after_launch', 'answer', 'commit', 'park',
              'settle_not_sent', 'settle_refused', 'settle_uncertain'}


class TeacherPassTable(ParkMixin, TeacherFixture):
    def setUp(self):
        super().setUp()
        self.install_scope()
        self.spare = 0
        self.key = shared._project_key(self.project)

    # -- states, made by the real writers ------------------------------------
    def build(self, state):
        if state == ABSENT:
            return
        if state == 'first_done':
            self.park()
            return
        sample, _bodies = self.add(RID)
        if state == 'first_running':
            # A pass that stopped after its claim committed (the two statements of the claim).
            with shared._db(self.root) as db:
                ledger.reserve(db, self.key, RID, 'first', digest(json.dumps(sample)), None, rearm=True)
                db.execute("UPDATE cases SET status='first_running' WHERE request_id=?", (RID,))
        elif state == 'gold':
            self.run_pass()
        elif state == 'pending_review':
            self.scenario('claude', {'mode': 'invalid'})
            self.run_pass()
            self.scenario('claude', {})
        self.expire_waits()

    def observed(self):
        row = self.cases().get(RID)
        return (row[0] if row else ABSENT), (self.stage(RID) or ABSENT)

    # -- stimuli ---------------------------------------------------------------
    def stimulate(self, name):
        monitor = mock.patch.object(general_teacher, '_process_usage', return_value=None)
        if name == 'answer_kept':
            self.defer_at = ('answer_received',)
        elif name == 'launch_deferred':
            self.defer_at = ('reserved',)
        elif name == 'not_sent_fifth_time':
            with shared._db(self.root) as db:
                for _ in range(ledger.RETRY_LIMIT - 1):
                    ledger.add_retry(db, self.key, RID)
            with monitor:  # No resource monitor: the call fails before anything is sent.
                self.run_pass()
            return
        elif name == 'provider_refused':
            self.scenario('claude', {'mode': 'quota'})
        elif name == 'content_refused':
            self.scenario('claude', {'mode': 'content'})
        elif name == 'cli_failed':
            self.scenario('claude', {'mode': 'exit'})
        elif name == 'input_over_budget':
            self.proposal['maxInputBytes'] = 16
        elif name == 'kept_answer_changed':
            with shared._db(self.root) as db:
                db.execute("UPDATE cases SET first='{}' WHERE request_id=? AND status='first_done'", (RID,))
        elif name == 'consent_withdrawn':
            with mock.patch.object(general_teacher, '_consent_file_v2',
                                   side_effect=ValueError('teacher_consent_required')):
                self.run_pass()
            return
        self.run_pass()

    def when(self):
        # A claim left by a stopped pass already sits at ``reserved`` when the pass
        # reads its scope, before the recovery: that first read is let through.
        if self.spare:
            self.spare -= 1
            return False
        return True

    def test_every_state_under_every_stimulus_ends_where_the_model_says(self):
        cells, driven = 0, {'M1': set(), 'M2': set()}
        for state in M1.states:
            for name, (case_events, ledger_events) in STIMULI.items():
                with self.subTest(state=state, stimulus=name):
                    self.tearDown_cell()
                    self.build(state)
                    self.spare = 1 if state == 'first_running' else 0
                    self.assertEqual(self.observed(), (state, LEDGER_OF[state]))
                    self.assertEqual(invariants.check(self.root), [])
                    self.stimulate(name)
                    self.assertEqual(invariants.check(self.root), [])
                    self.assertEqual(self.observed(),
                                     fold_pair(state, LEDGER_OF[state], case_events, ledger_events))
                cells += 1
                at = state
                for event in case_events:
                    if (at, event) in M1.transitions:
                        driven['M1'].add((at, event))
                    at = M1.next(at, event)
                at = LEDGER_OF[state]
                for event in ledger_events:
                    if (at, event) in M2.transitions:
                        driven['M2'].add((at, event))
                    at = M2.next(at, event)
        self.assertEqual(cells, len(M1.states) * len(STIMULI))
        self.assertEqual((len(M1.states), len(STIMULI)), (6, 10))
        # Every listed case transition is taken by some cell, except the two with their own tests.
        missed = {pair for pair in M1.transitions if pair not in driven['M1']}
        self.assertEqual({event for _state, event in missed}, NOT_DRIVEN)
        self.assertEqual({event for stimulus in STIMULI.values() for event in stimulus[0]},
                         set(M1.events) - NOT_DRIVEN)
        self.assertEqual({event for stimulus in STIMULI.values() for event in stimulus[1]},
                         set(M2.events))

    def test_each_invariant_names_the_state_that_breaks_it(self):
        self.assertEqual(invariants.check(self.home / 'no-namespace'), [])
        sample, _bodies = self.add(RID)
        payload = digest(json.dumps(sample))
        with shared._db(self.root) as db:
            ledger.ensure(db)
            db.execute("UPDATE cases SET status='first_running' WHERE request_id=?", (RID,))
        self.assertEqual(invariants.check(self.root), ['I-1 a1 first_running with ledger None'])
        self.tearDown_cell()
        self.park()
        with shared._db(self.root) as db:
            db.execute("UPDATE cases SET first=? WHERE request_id=?",
                       (json.dumps({'choice': {'selected_ids': []}}), RID))
        self.assertEqual(invariants.check(self.root), ['I-2 a1 kept answer does not match the ledger'])
        with shared._db(self.root) as db:
            db.execute("UPDATE project_gold_attempts SET stage='dispatched_uncertain'")
        self.assertEqual(invariants.check(self.root), ['I-2 a1 first_done with ledger dispatched_uncertain'])
        self.tearDown_cell()
        self.add(RID)
        self.run_pass()
        with shared._db(self.root) as db:
            db.execute("UPDATE project_gold_attempts SET stage='answer_received'")
        self.assertEqual(invariants.check(self.root), ['I-3 a1 gold with ledger answer_received'])
        with shared._db(self.root) as db:
            db.execute("UPDATE project_gold_attempts SET stage='committed'")
            db.execute('INSERT INTO project_gold_attempts VALUES (?,?,?,?,?,NULL,NULL,NULL,1,1)',
                       (self.key, 'again', 'first', payload, 'dispatched'))
            sha = db.execute("SELECT input_sha256 FROM project_gold_attempts WHERE request_id=?", (RID,)).fetchone()[0]
            db.execute("UPDATE project_gold_attempts SET input_sha256=? WHERE request_id='again'", (sha,))
        self.assertEqual(invariants.check(self.root), ['G-1 input %s asked 2 times' % sha[:12]])
        # A proven non-transmission may be asked again.
        with shared._db(self.root) as db:
            db.execute("UPDATE project_gold_attempts SET stage='pre_send_rejected' WHERE request_id='again'")
        self.assertEqual(invariants.check(self.root), [])

    def tearDown_cell(self):
        """Back to an empty namespace between cells (same fixture, new rows)."""
        with shared._db(self.root) as db:
            ledger.ensure(db)
            ledger.ensure_retries(db)
            for table in ('cases', 'project_gold_attempts', 'project_gold_retries'):
                db.execute('DELETE FROM ' + table)
        for path in self.logs.glob('claude-*.json'):
            path.unlink()
        self.defer_at, self.fail_at, self.spare = (), {}, 0
        self.proposal.update(maxInputBytes=65536, enabled=True)
        # Host waits of the previous cell would keep this cell's pass from calling.
        from memvane.learning.context_learning import teacher_state
        (self.root / teacher_state.STATE_NAME).unlink(missing_ok=True)


class LedgerTable(TeacherFixture):
    """M2 alone: every stage under every ledger operation, as its caller issues it."""
    OPEN = ('reserved', 'dispatch_unknown', 'dispatched', 'answer_received')

    def setUp(self):
        super().setUp()
        self.key = shared._project_key(self.project)
        from memvane.learning.context_learning import default_teacher
        settle = lambda new: lambda db: default_teacher._settle(db, self.key, RID, new, 'fixture_code')
        move = lambda expected, new: lambda db: ledger.transition(db, self.key, RID, 'first', expected, new)
        self.operations = {
            'reserve': lambda db: ledger.reserve(db, self.key, RID, 'first', 'a' * 64, None, rearm=True),
            'before_launch': move(('reserved',), 'dispatch_unknown'),
            'after_launch': move(('dispatch_unknown',), 'dispatched'),
            'answer': move(('dispatched',), 'answer_received'),
            'commit': move(('answer_received',), 'committed'),
            'park': move(('answer_received',), default_teacher.PARKED),
            'confirm': move((default_teacher.PARKED,), 'committed'),
            'reject_kept': move((default_teacher.PARKED,), 'dispatched_uncertain'),
            'settle_not_sent': settle('pre_send_rejected'),
            'settle_refused': settle('provider_refused'),
            'settle_uncertain': settle('dispatched_uncertain'),
            'recover': lambda db: ledger.recover(db, self.key),
        }

    def at(self, stage):
        with shared._db(self.root) as db:
            ledger.ensure(db)
            db.execute('DELETE FROM project_gold_attempts')
            if stage != ABSENT:
                db.execute('INSERT INTO project_gold_attempts VALUES (?,?,?,?,?,NULL,NULL,NULL,1,1)',
                           (self.key, RID, 'first', 'a' * 64, stage))

    def stage(self):
        with shared._db(self.root) as db:
            return ledger.stage(db, self.key, RID, 'first') or ABSENT

    def test_every_stage_under_every_operation_ends_where_the_model_says(self):
        self.assertEqual(set(self.operations), set(M2.events))
        cells = 0
        for stage, event in M2.cells():
            with self.subTest(stage=stage, event=event):
                self.at(stage)
                try:
                    with shared._db(self.root) as db:
                        self.operations[event](db)
                except ValueError as exc:
                    # A refused operation names its conflict and changes nothing.
                    self.assertIn(str(exc), ('gold_transition_conflict', 'gold_attempt_already_reserved'))
                    self.assertNotIn((stage, event), M2.transitions)
                self.assertEqual(self.stage(), M2.next(stage, event))
            cells += 1
        self.assertEqual(cells, len(M2.states) * len(M2.events))
        self.assertEqual((len(M2.states), len(M2.events)), (10, 12))
