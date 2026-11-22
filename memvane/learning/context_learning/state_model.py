"""The learning state machines as data: states, events, transitions and owners.

Pure data with no import. No hook path and no worker reads it to decide a
transition: the writers stay where they are, and the transition-table tests
(``memvane/learning/tests/statemachine``) drive every listed transition
through its real writer and assert that every other (state, event) pair leaves
the state unchanged. ``docs/dev/learning-state.md`` is rendered from here.

A machine is a ``Machine``: ``states`` (``ABSENT`` is "no row"), ``terminal``
(no listed transition leaves it, except a deletion), ``events``,
``transitions`` (``(state, event) -> next state``), ``owner`` (who writes) and
``where`` (the writer of each event, ``module.function``). Only the product
path (the default teacher of a ``default-teacher`` profile) is modelled; the
explicit, synthetic and two-opinion paths are development and QA paths.
"""

ABSENT = '(absent)'


class Machine:
    def __init__(self, name, title, *, states, terminal, transitions, owner, where, store, lock):
        self.name, self.title = name, title
        self.states, self.terminal = tuple(states), frozenset(terminal)
        self.transitions = dict(transitions)
        self.events = tuple(sorted({event for _state, event in self.transitions}))
        self.owner, self.where, self.store, self.lock = owner, dict(where), store, lock
        unknown = ({state for state, _event in self.transitions} | set(self.transitions.values())
                   | self.terminal) - set(self.states)
        if unknown or set(self.where) != set(self.events):
            raise ValueError('state_model_inconsistent:' + name)

    def cells(self):
        """Every (state, event) pair: the table the transition tests cover."""
        return [(state, event) for state in self.states for event in self.events]

    def next(self, state, event):
        """The state after ``event`` in ``state``; the same state when not listed."""
        return self.transitions.get((state, event), state)


_TEACHER = 'memvane.learning.context_learning.default_teacher.'
_LEDGER = 'memvane.learning.context_learning.general_gold_ledger.'

# M1: one captured edit case (learning DB ``cases.status``). ``use_state``
# (``active`` -> ``historical``) and ``trained_cycle`` are attributes of the
# row with their own one-way steps (``M1_USE``, ``M1_TRAINED``).
_RUNNING, _KEPT = 'first_running', 'first_done'
M1 = Machine(
    'M1', 'case', store='learning DB cases.status', lock='project-worker.lock',
    owner='hook capture child (insert), teacher pass, rotation and migration (delete)',
    states=(ABSENT, 'queued', _RUNNING, _KEPT, 'gold', 'pending_review'),
    terminal=('gold', 'pending_review'),
    transitions={
        (ABSENT, 'capture'): 'queued',
        ('queued', 'local_check_failed'): 'pending_review',
        ('queued', 'claim'): _RUNNING,
        (_RUNNING, 'answer_committed'): 'gold',
        (_RUNNING, 'answer_kept'): _KEPT,
        (_RUNNING, 'not_sent'): 'queued',
        (_RUNNING, 'not_sent_exhausted'): 'pending_review',
        (_RUNNING, 'provider_refused'): 'queued',
        (_RUNNING, 'content_refused'): 'pending_review',
        (_RUNNING, 'uncertain'): 'pending_review',
        (_RUNNING, 'pass_start'): 'pending_review',
        (_KEPT, 'kept_confirmed'): 'gold',
        (_KEPT, 'kept_rejected'): 'pending_review',
        (_KEPT, 'consent_withdrawn'): 'pending_review',
        ('gold', 'rotation'): ABSENT,
        ('pending_review', 'rotation'): ABSENT,
    },
    where={
        'capture': 'memvane.learning.context_learning.shared.enqueue',
        'local_check_failed': _TEACHER + '_default_case',
        'claim': _TEACHER + '_default_case',
        'answer_committed': _TEACHER + '_call',
        'answer_kept': _TEACHER + '_park',
        'not_sent': _TEACHER + '_failed',
        'not_sent_exhausted': _TEACHER + '_failed',
        'provider_refused': _TEACHER + '_failed',
        'content_refused': _TEACHER + '_failed',
        'uncertain': _TEACHER + '_failed',
        'pass_start': _TEACHER + 'process_pending_default',
        'kept_confirmed': _TEACHER + '_confirm_parked',
        'kept_rejected': _TEACHER + '_confirm_parked',
        'consent_withdrawn': _TEACHER + '_review_parked',
        'rotation': 'memvane.learning.context_learning.case_rotation.rotate',
    })

# M2: the dispatch identity of one case's teacher call (``project_gold_attempts.stage``).
# A stage that proves nothing was answered (``pre_send_rejected``, ``provider_refused``)
# is reserved again by a later pass; ``answer_parked`` is never touched by ``recover``.
_OPEN = ('reserved', 'dispatch_unknown', 'dispatched', 'answer_received')
M2 = Machine(
    'M2', 'gold ledger', store='learning DB project_gold_attempts.stage',
    lock='project-worker.lock', owner='teacher pass',
    states=(ABSENT, *_OPEN, 'answer_parked', 'committed', 'pre_send_rejected',
            'provider_refused', 'dispatched_uncertain'),
    terminal=('committed', 'dispatched_uncertain'),
    transitions={
        (ABSENT, 'reserve'): 'reserved',
        ('pre_send_rejected', 'reserve'): 'reserved',
        ('provider_refused', 'reserve'): 'reserved',
        ('reserved', 'before_launch'): 'dispatch_unknown',
        ('dispatch_unknown', 'after_launch'): 'dispatched',
        ('dispatched', 'answer'): 'answer_received',
        ('answer_received', 'commit'): 'committed',
        ('answer_received', 'park'): 'answer_parked',
        ('answer_parked', 'confirm'): 'committed',
        ('answer_parked', 'reject_kept'): 'dispatched_uncertain',
        **{(stage, 'settle_not_sent'): 'pre_send_rejected' for stage in _OPEN},
        **{(stage, 'settle_refused'): 'provider_refused' for stage in _OPEN},
        **{(stage, 'settle_uncertain'): 'dispatched_uncertain' for stage in _OPEN},
        **{(stage, 'recover'): 'dispatched_uncertain' for stage in _OPEN},
    },
    where={
        'reserve': _LEDGER + 'reserve',
        'before_launch': _TEACHER + '_call',
        'after_launch': _TEACHER + '_call',
        'answer': _TEACHER + '_call',
        'commit': _TEACHER + '_call',
        'park': _TEACHER + '_park',
        'confirm': _TEACHER + '_confirm_parked',
        'reject_kept': _TEACHER + '_review_parked',
        'settle_not_sent': _TEACHER + '_failed',
        'settle_refused': _TEACHER + '_failed',
        'settle_uncertain': _TEACHER + '_failed',
        'recover': _LEDGER + 'recover',
    })

# M15: one host's entry of ``teacher-host-state.json``, read at a pass. ``resting``
# is a reason without an expiry (the CLI is missing, the consent does not name the
# host, the isolation check refused): the next pass asks again. The two timed waits
# end by their expiry, and the pass after it finds the host ready. Fewer than three
# consecutive failures only move the counter; an answer resets it.
M15 = Machine(
    'M15', 'teacher host', store='teacher-host-state.json hosts.<host>',
    lock='teacher-host-state.lock (under project-worker.lock)', owner='teacher pass',
    states=('ready', 'resting', 'provider_waiting', 'failing'),
    terminal=(),
    transitions={
        ('ready', 'host_not_ready'): 'resting',
        ('resting', 'host_ready'): 'ready',
        ('ready', 'provider_refused'): 'provider_waiting',
        ('ready', 'failure_limit'): 'failing',
        ('provider_waiting', 'wait_expired'): 'ready',
        ('failing', 'wait_expired'): 'ready',
    },
    where={
        'host_not_ready': _TEACHER + '_default_pass',
        'host_ready': _TEACHER + '_default_pass',
        'provider_refused': _TEACHER + '_failed',
        'failure_limit': _TEACHER + '_failed',
        'wait_expired': 'memvane.learning.context_learning.teacher_state.host_waiting',
    })

MACHINES = (M1, M2, M15)
