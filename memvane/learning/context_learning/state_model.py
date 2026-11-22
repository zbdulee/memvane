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

# M1: one captured edit case (learning DB ``cases.status``). ``use_state`` is an
# attribute of the same row with its own one-way step (``M1U`` below). Rotation
# deletes closed cases (``gold`` whose training use is over, ``pending_review``)
# and, of the cases that left use (``historical``), the ``queued`` and ``gold``
# ones. Two deletions run before every pass, whatever the status: prompt cases of
# an older sample schema, and edit cases captured unchecked in a project inside a
# parent work tree.
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
        **{(status, 'rotation_historical'): ABSENT for status in ('queued', 'gold', 'pending_review')},
        **{(status, event): ABSENT for event in ('prompt_retired', 'git_migrated')
           for status in ('queued', _RUNNING, _KEPT, 'gold', 'pending_review')},
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
        'rotation_historical': 'memvane.learning.context_learning.case_rotation.rotate',
        'prompt_retired': 'memvane.learning.context_learning.prompt_retirement.retire',
        'git_migrated': 'memvane.learning.context_learning.edit_git_migration.migrate',
    })

# M1U: whether a case is still used (``cases.use_state``). One way: a case that
# left use never returns, and no teacher call or training reads it again. The
# explicit and synthetic paths write the same step from their own reconcile.
M1U = Machine(
    'M1U', 'case use', store='learning DB cases.use_state', lock='project-worker.lock',
    owner='hook capture child (insert), teacher pass',
    states=(ABSENT, 'active', 'historical'),
    terminal=('historical',),
    transitions={
        (ABSENT, 'capture'): 'active',
        ('active', 'captured_before_grant'): 'historical',
        ('active', 'scope_changed'): 'historical',
    },
    where={
        'capture': 'memvane.learning.context_learning.shared.enqueue',
        'captured_before_grant': _TEACHER + '_migration_ready',
        'scope_changed': _TEACHER + '_default_case',
    })

# M1T: whether a gold case was a member of a finished training (``cases.trained_cycle``).
# Set once, with the cycle id, in the transaction that stages the cycle.
M1T = Machine(
    'M1T', 'case training mark', store='learning DB cases.trained_cycle',
    lock='learning-claim.lock', owner='learning cycle of the project worker',
    states=('unmarked', 'marked'),
    terminal=('marked',),
    transitions={('unmarked', 'cycle_staged'): 'marked'},
    where={'cycle_staged': 'memvane.learning.context_learning.automatic_staging._train_pinned'})

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
    lock='teacher-host-state.lock', owner='teacher pass (a SessionStart hook writes notice times only)',
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

# M3: one pinned training batch (``learning_jobs.status``). A claim is held for the
# length of one attempt; the block that held it leaves the job ``deferred`` unless
# the attempt completed it. ``blocked`` by repeated trainer failures is queued again
# a day later (``rearm``); ``blocked`` because the members themselves changed
# (``already_consumed``, ``source_revoked``) stays: the next plan pins other members, or answers with it when they are the same.
_QUEUE = 'memvane.learning.context_learning.learning_queue.LearningQueue'
_OPEN_JOB = ('queued', 'running', 'deferred')
M3 = Machine(
    'M3', 'learning job', store='learning DB learning_jobs.status',
    lock='learning-claim.lock (one attempt), database transaction (each step)',
    owner='learning cycle of the project worker',
    states=(ABSENT, *_OPEN_JOB, 'staged', 'blocked'),
    terminal=('staged',),
    transitions={
        (ABSENT, 'plan'): 'queued',
        ('queued', 'claim'): 'running',
        ('deferred', 'claim'): 'running',
        ('running', 'claim_released'): 'deferred',
        **{(status, 'complete'): 'staged' for status in _OPEN_JOB},
        ('queued', 'defer'): 'deferred',
        ('running', 'defer'): 'deferred',
        ('queued', 'fail'): 'deferred',
        ('running', 'fail'): 'deferred',
        **{(status, 'fail_limit'): 'blocked' for status in _OPEN_JOB},
        **{(status, 'consumed_elsewhere'): 'blocked' for status in _OPEN_JOB},
        ('running', 'source_revoked'): 'blocked',
        ('blocked', 'rearm'): 'queued',
    },
    where={
        'plan': _QUEUE, 'claim': _QUEUE, 'claim_released': _QUEUE, 'complete': _QUEUE,
        'defer': _QUEUE, 'fail': _QUEUE, 'fail_limit': _QUEUE, 'consumed_elsewhere': _QUEUE,
        'source_revoked': 'memvane.learning.context_learning.automatic_staging._train_pinned',
        'rearm': _QUEUE,
    })

# M4: one training cycle (``cycles.status``), keyed by its job id. ``training`` is an
# attempt in progress or one whose process ended without a word; ``failed`` and
# ``deferred`` say how the last attempt ended, and every one of the three is trained
# again by the next attempt. A cycle row has no closed state: a rejected candidate
# is closed in the evaluation machine.
_STAGING = 'memvane.learning.context_learning.automatic_staging.'
_RETRAINED = ('training', 'failed', 'deferred')
M4 = Machine(
    'M4', 'cycle', store='learning DB cycles.status',
    lock='learning-claim.lock (training), evaluation claim (evaluation), apply journal (apply)',
    owner='learning cycle of the project worker',
    states=(ABSENT, *_RETRAINED, 'staged', 'staged_evaluated', 'applied'),
    terminal=('applied',),
    transitions={
        (ABSENT, 'train_started'): 'training',
        **{(status, 'trained'): 'staged' for status in _RETRAINED},
        ('training', 'train_failed'): 'failed',
        ('deferred', 'train_failed'): 'failed',
        ('training', 'train_deferred'): 'deferred',
        ('failed', 'train_deferred'): 'deferred',
        ('staged', 'evaluated'): 'staged_evaluated',
        ('staged_evaluated', 'reevaluation_needed'): 'staged',
        ('staged_evaluated', 'carried'): 'staged',
        ('staged_evaluated', 'applied'): 'applied',
    },
    where={
        'train_started': _STAGING + '_train_pinned',
        'trained': _STAGING + '_train_pinned',
        'train_failed': _STAGING + '_train_pinned',
        'train_deferred': _STAGING + '_train_pinned',
        'evaluated': _STAGING + '_evaluate_impl',
        'reevaluation_needed': _STAGING + '_prepare_evaluation',
        'carried': 'memvane.learning.context_learning.candidate_close.carry_candidate',
        'applied': 'memvane.learning.context_learning.candidate_apply._finish_cycle',
    })

# M5: the quality evaluation of one staged cycle (``learning_evaluations.status``).
# ``completed`` is not final: a stale or changed evaluation is queued again, and a
# judged candidate is closed. ``blocked`` always carries its reason. A candidate
# carried to a renewed approval loses its row; the next pass makes a new one.
_EVALUATIONS = 'memvane.learning.context_learning.evaluation_queue.EvaluationQueue'
_OPEN_EVALUATION = ('queued', 'running', 'deferred')
_UNJUDGED = (*_OPEN_EVALUATION, 'waiting_quality')
M5 = Machine(
    'M5', 'evaluation', store='learning DB learning_evaluations.status',
    lock='evaluation-claim.lock (one attempt), database transaction (each step)',
    owner='learning cycle of the project worker',
    states=(ABSENT, *_OPEN_EVALUATION, 'waiting_quality', 'completed', 'blocked'),
    terminal=('blocked',),
    transitions={
        (ABSENT, 'ensure'): 'queued',
        ('waiting_quality', 'validation_changed'): 'queued',
        ('queued', 'claim'): 'running',
        ('deferred', 'claim'): 'running',
        ('running', 'claim_released'): 'deferred',
        **{(status, 'evaluated'): 'completed' for status in _UNJUDGED},
        **{(status, 'too_few_cases'): 'waiting_quality' for status in _OPEN_EVALUATION},
        ('queued', 'defer'): 'deferred',
        ('running', 'defer'): 'deferred',
        **{(status, 'fail_limit'): 'blocked' for status in _OPEN_EVALUATION},
        ('completed', 'refresh'): 'queued',
        ('waiting_quality', 'refresh'): 'queued',
        **{(status, 'reject'): 'blocked' for status in (ABSENT, *_UNJUDGED, 'completed')},
        **{(status, 'block'): 'blocked' for status in _UNJUDGED},
        **{(status, 'carried'): ABSENT for status in (*_UNJUDGED, 'completed', 'blocked')},
    },
    where={
        **{event: _EVALUATIONS for event in (
            'ensure', 'validation_changed', 'claim', 'claim_released', 'evaluated', 'too_few_cases',
            'defer', 'fail_limit', 'refresh', 'reject', 'block')},
        'carried': 'memvane.learning.context_learning.candidate_close.carry_candidate',
    })

# M6: the application of one evaluated candidate (``candidate-apply.json`` ``phase``).
# Nothing is committed under ``intent``, so every attempt forms it again. A failure
# ends the application only when it judges the candidate, is the resident's own
# refusal, leaves the journal unusable, or is the fifth other failure in a row; the
# pointer is then still the incumbent's. A momentary resource state and a stale
# evaluation change no phase. A finished journal is copied to the archive and
# replaced when the next candidate's intent is written.
_APPLY = 'memvane.learning.context_learning.candidate_apply.apply_candidate'
M6 = Machine(
    'M6', 'application', store='candidate-apply.json phase',
    lock='candidate-apply.lock, cycles/cycle.lock', owner='learning cycle of the project worker',
    states=(ABSENT, 'intent', 'prepared', 'pointer_committed', 'activated', 'rejected'),
    terminal=('activated', 'rejected'),
    transitions={
        (ABSENT, 'intent_formed'): 'intent',
        ('intent', 'prepared_by_service'): 'prepared',
        ('prepared', 'pointer_committed'): 'pointer_committed',
        ('pointer_committed', 'activated_by_service'): 'activated',
        ('intent', 'ended'): 'rejected',
        ('prepared', 'ended'): 'rejected',
    },
    where={event: _APPLY for event in ('intent_formed', 'prepared_by_service', 'pointer_committed',
                                       'activated_by_service', 'ended')})

# M7: the artifact recall is scored with (``active-checkpoint.json``). It changes in
# one place, between ``prepared`` and ``pointer_committed`` of M6, and never back.
M7 = Machine(
    'M7', 'active checkpoint', store='active-checkpoint.json',
    lock='cycles/cycle.lock', owner='learning cycle of the project worker',
    states=('incumbent', 'candidate'),
    terminal=(),
    transitions={('incumbent', 'pointer_written'): 'candidate'},
    where={'pointer_written': _APPLY})

MACHINES = (M1, M1U, M1T, M2, M3, M4, M5, M6, M7, M15)
