"""M1 (case) and M2 (gold ledger): every state under every teacher-pass stimulus.

A stimulus is one real ``process_pending_default`` pass made to take one
branch (fake host CLIs, a scope check that defers at a chosen ledger stage).
It names the model events the pass attempts, in order. The expected state is
the model's ``next`` folded over those events from the starting state, so an
event that is not listed for a state must leave the state as it is. The number
of cells run equals states x stimuli.

A stimulus evaluates the (state, event) pairs its events pass through, not
every pair. The pairs of the model are accounted for one by one: evaluated by
a stimulus, driven directly (``capture`` and the two rotations, each state
under the real writer), unreachable in any pass order (listed by rule), or
evaluated by another module (listed with that module).
"""
from __future__ import annotations

import json
from unittest import mock

from memvane.learning.context_learning import general_gold_ledger as ledger
from memvane.learning.context_learning import case_rotation, edit_git_migration, general_teacher, shared
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.state_model import ABSENT, M1, M1U, M2
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture, edit_sample
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
# Events no pass stimulus attempts: driven directly below or evaluated elsewhere.
DIRECT = ('capture', 'rotation', 'rotation_historical')
DELETIONS = ('prompt_retired', 'git_migrated')
NOT_DRIVEN = set(DIRECT) | set(DELETIONS)
_CALL_ENDS = ('answer_committed', 'answer_kept', 'not_sent', 'not_sent_exhausted',
              'provider_refused', 'content_refused', 'uncertain')
# Pairs no order of a pass reaches.
UNREACHABLE = (
    # A call ends only for the case the same pass claimed.
    {('queued', event) for event in _CALL_ENDS}
    # A pass settles a kept answer at its start, before it reads the queue.
    | {('first_done', event) for event in (*_CALL_ENDS, 'claim', 'local_check_failed')}
    # A pass closes an interrupted call at its start, before anything else.
    | {('first_running', event) for event in ('claim', 'local_check_failed',
                                              'kept_confirmed', 'kept_rejected')})
# Pairs another module evaluates with the rows that writer needs.
ELSEWHERE = {
    # A consumed gold outside the kept window, with its job and cycle rows.
    ('gold', 'rotation'): 'memvane/learning/tests/integration/test_rotation_123.py',
    **{(state, 'prompt_retired'): 'memvane/learning/tests/integration/test_prompt_retirement_118.py'
       for state in M1.states},
    **{(state, 'git_migrated'): 'memvane/learning/tests/integration/test_edit_git_119.py'
       for state in M1.states},
}


def visited():
    """The (state, event) pairs of M1 the stimuli pass through."""
    seen = set()
    for state in M1.states:
        for case_events, _ledger_events in STIMULI.values():
            at = state
            for event in case_events:
                seen.add((at, event))
                at = M1.next(at, event)
    return seen


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
        # Every listed case transition is taken by some cell, except those of the events
        # no stimulus attempts.
        missed = {pair for pair in M1.transitions if pair not in driven['M1']}
        self.assertEqual({event for _state, event in missed}, NOT_DRIVEN)
        self.assertEqual({event for stimulus in STIMULI.values() for event in stimulus[0]},
                         set(M1.events) - NOT_DRIVEN)
        self.assertEqual({event for stimulus in STIMULI.values() for event in stimulus[1]},
                         set(M2.events))

    # -- the pairs no stimulus evaluates ----------------------------------------
    def test_every_case_cell_is_evaluated_driven_unreachable_or_listed_elsewhere(self):
        cells = set(M1.cells())
        direct = {(state, event) for state in M1.states for event in DIRECT} - set(ELSEWHERE)
        self.assertEqual(cells - visited() - direct, UNREACHABLE | set(ELSEWHERE))
        self.assertFalse(visited() & UNREACHABLE)
        self.assertEqual((len(cells), len(visited()), len(direct), len(UNREACHABLE), len(ELSEWHERE)),
                         (108, 58, 17, 20, 13))
        from pathlib import Path
        repo = Path(__file__).resolve().parents[4]
        for name in set(ELSEWHERE.values()):
            self.assertTrue((repo / name).is_file(), name)

    def capture(self):
        """One capture of RID through the real writer; its answer."""
        sample, _bodies = edit_sample(RID)
        sources = {}
        for candidate in sample['candidates']:
            text = '---\ntitle: card\n---\n%s body\n' % candidate['candidate_id']
            candidate['revision_sha256'] = digest(text)
            sources[candidate['candidate_id']] = text
        with mock.patch.object(shared, '_safe_root', return_value=self.root), \
                mock.patch.object(shared, '_policy', return_value={}), \
                mock.patch.object(shared, '_consent', return_value=True):
            return shared.enqueue({}, self.project, sample, sources)

    def leave_use(self):
        """The case leaves use the way a pass finds its consent or source changed."""
        with shared._db(self.root) as db:
            db.execute("UPDATE cases SET use_state='historical',error='consent_or_source_changed' "
                       "WHERE request_id=? AND use_state='active'", (RID,))

    def drive(self, event):
        if event == 'capture':
            return self.capture()
        if event == 'rotation_historical':
            self.leave_use()
        return case_rotation.rotate(self.root, self.project, self.grant, limit=0)['status']

    def test_capture_and_rotation_on_every_case_state(self):
        cells = 0
        for state in M1.states:
            for event in DIRECT:
                if (state, event) in ELSEWHERE:
                    continue
                with self.subTest(state=state, event=event):
                    self.tearDown_cell()
                    self.build(state)
                    self.assertEqual(self.observed()[0], state)
                    answer = self.drive(event)
                    self.assertEqual(self.observed()[0], M1.next(state, event), answer)
                    if event == 'capture':
                        # A second capture of the same request never replaces the row.
                        self.assertEqual(answer in ('duplicate', 'conflict'), state != ABSENT, answer)
                cells += 1
        self.assertEqual(cells, len(M1.states) * len(DIRECT) - 1)

    # -- M1U: whether the case is still used -------------------------------------
    def use(self):
        row = self.cases().get(RID)
        return row[2] if row else ABSENT

    def test_every_use_state_under_every_event_ends_where_the_model_says(self):
        cells = 0
        for state, event in M1U.cells():
            with self.subTest(state=state, event=event):
                self.tearDown_cell()
                if state != ABSENT:
                    self.add(RID)
                if state == 'historical':
                    self.leave_use()
                self.assertEqual(self.use(), state)
                if event == 'capture':
                    self.capture()
                elif event == 'captured_before_grant':
                    self.proposal['grantedAtUnixMs'] = 10 ** 15
                    with mock.patch.object(edit_git_migration, 'affected', return_value=True), \
                            mock.patch.object(edit_git_migration, '_unchecked', return_value=True):
                        self.run_pass()
                elif event == 'scope_changed':
                    with mock.patch.object(general_teacher, '_case_active', return_value=False):
                        self.run_pass()
                self.assertEqual(self.use(), M1U.next(state, event))
                if state == 'active' and event != 'capture':
                    # A case that left use is neither asked about nor answered.
                    self.assertEqual((self.runs(), self.cases()[RID][0]), (0, 'queued'))
            cells += 1
        self.assertEqual(cells, len(M1U.states) * len(M1U.events))
        self.assertEqual((len(M1U.states), len(M1U.events)), (3, 3))

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
        self.proposal.update(maxInputBytes=65536, enabled=True, grantedAtUnixMs=0)
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
