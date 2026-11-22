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
# a day later (``rearm``); ``blocked`` by a failure another attempt would repeat, or
# because the members themselves changed
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
        **{(status, 'source_revoked'): 'blocked' for status in _OPEN_JOB},
        **{(status, 'unrepeatable_failure'): 'blocked' for status in _OPEN_JOB},
        ('blocked', 'rearm'): 'queued',
    },
    where={
        'plan': _QUEUE, 'claim': _QUEUE, 'claim_released': _QUEUE, 'complete': _QUEUE,
        'defer': _QUEUE, 'fail': _QUEUE, 'fail_limit': _QUEUE, 'consumed_elsewhere': _QUEUE,
        'source_revoked': _QUEUE, 'unrepeatable_failure': _QUEUE,
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
        ('completed', 'apply_failure_limit'): 'blocked',
        **{(status, 'carried'): ABSENT for status in (*_UNJUDGED, 'completed', 'blocked')},
    },
    where={
        **{event: _EVALUATIONS for event in (
            'ensure', 'validation_changed', 'claim', 'claim_released', 'evaluated', 'too_few_cases',
            'defer', 'fail_limit', 'refresh', 'reject', 'block', 'apply_failure_limit')},
        'carried': 'memvane.learning.context_learning.candidate_close.carry_candidate',
    })

# M6: the application of one evaluated candidate (``candidate-apply.json`` ``phase``).
# Nothing is committed while the pointer is the incumbent's, so every attempt forms
# the intent again (also from a ``prepared`` whose pointer was never written). A failure
# ends the application only when it judges the candidate, is the resident's own
# refusal, leaves the journal unusable, or is the fifth other failure in a row; the
# pointer is then still the incumbent's. A momentary resource state and a stale
# evaluation change no phase. A finished journal is copied to the archive and
# replaced when the next candidate's intent is written.
_APPLY = 'memvane.learning.context_learning.candidate_apply.apply_candidate'
_ROLLBACK = 'memvane.learning.context_learning.candidate_rollback.restore'
M6 = Machine(
    'M6', 'application', store='candidate-apply.json phase',
    lock='candidate-apply.lock, cycles/cycle.lock', owner='learning cycle of the project worker',
    states=(ABSENT, 'intent', 'prepared', 'pointer_committed', 'activated', 'rejected'),
    # ``activated`` ends the application for the worker; only the owner's command leaves it.
    terminal=('rejected',),
    transitions={
        (ABSENT, 'intent_formed'): 'intent',
        ('intent', 'prepared_by_service'): 'prepared',
        # The pointer was never written under it: the next attempt forms the intent again.
        ('prepared', 'intent_formed'): 'intent',
        ('prepared', 'pointer_committed'): 'pointer_committed',
        ('pointer_committed', 'activated_by_service'): 'activated',
        ('intent', 'ended'): 'rejected',
        ('prepared', 'ended'): 'rejected',
        # The owner's maintenance command: the previous pointer is put back (reason ``rolled_back``).
        ('pointer_committed', 'rolled_back'): 'rejected',
        ('activated', 'rolled_back'): 'rejected',
    },
    where={**{event: _APPLY for event in ('intent_formed', 'prepared_by_service', 'pointer_committed',
                                          'activated_by_service', 'ended')},
           'rolled_back': _ROLLBACK})

# M7: the artifact recall is scored with (``active-checkpoint.json``). It changes in
# one place, between ``prepared`` and ``pointer_committed`` of M6, and goes back only by the
# owner's maintenance command (M6 ``rolled_back``).
M7 = Machine(
    'M7', 'active checkpoint', store='active-checkpoint.json',
    lock='cycles/cycle.lock', owner='learning cycle of the project worker',
    states=('incumbent', 'candidate'),
    terminal=(),
    transitions={('incumbent', 'pointer_written'): 'candidate', ('candidate', 'pointer_restored'): 'incumbent'},
    where={'pointer_written': _APPLY, 'pointer_restored': _ROLLBACK})

# M8: the newest full (teacher and trainer) worker request of a namespace
# (``full-worker-request.json`` and the state of its nonce in ``full-worker-state.json``).
# A hook's ``launch`` replaces the request, and with it the state, whatever it was.
# The dispatcher's step counts a start before it spawns the owned child; three starts
# without an outcome are abandoned. A result of the retry set keeps the request open
# with its starts at zero; any other result is the outcome, acknowledged by the next step.
_SHARED = 'memvane.learning.context_learning.shared.'
M8 = Machine(
    'M8', 'worker request', store='full-worker-request.json, full-worker-state.json',
    lock='full-worker.lock', owner='hook launch (request), dispatcher step and owned child (state)',
    states=(ABSENT, 'open', 'last_start', 'settled', 'acknowledged'),
    terminal=(),
    transitions={
        **{(state, 'launch'): 'open' for state in (ABSENT, 'last_start', 'settled', 'acknowledged')},
        ('open', 'third_start'): 'last_start',
        ('last_start', 'step'): 'acknowledged',
        ('settled', 'step'): 'acknowledged',
        ('open', 'child_result'): 'settled',
        ('last_start', 'child_result'): 'settled',
        ('last_start', 'child_retry_result'): 'open',
    },
    where={'launch': _SHARED + 'launch', 'third_start': _SHARED + '_full_step', 'step': _SHARED + '_full_step',
           'child_result': _SHARED + '_settle_owned', 'child_retry_result': _SHARED + '_settle_owned'})

# M9: the ``learning`` wake row. A request while the row runs raises its revision, and
# the acknowledgement of the older revision leaves it pending. A pass that was not done
# counts an attempt and backs off (5 s doubling to 300 s); the 288th in a row drops the
# row with ``retry_cap``. A worker that starts finds a row its predecessor left running.
_WAKES = 'memvane.runtime.runtime_work.'
M9 = Machine(
    'M9', 'learning wake', store='runtime-wakes.sqlite3 (kind learning)',
    lock='runtime-worker.lock', owner='hook and worker requests (offer), runtime dispatcher (drain)',
    states=(ABSENT, 'pending', 'running'),
    terminal=(),
    transitions={
        (ABSENT, 'request'): 'pending',
        ('pending', 'claim'): 'running',
        ('running', 'done'): ABSENT,
        ('running', 'not_done'): 'pending',
        ('running', 'retry_cap'): ABSENT,
        ('running', 'worker_restart'): 'pending',
    },
    where={'request': _WAKES + 'request', 'claim': _WAKES + 'drain', 'done': _WAKES + 'drain',
           'not_done': _WAKES + 'drain', 'retry_cap': 'memvane.runtime.runtime_wake_policy.settle',
           'worker_restart': _WAKES + 'drain'})

# M10: the selector maintenance request of one install. The states are the shapes of the
# file: ``requested`` (``retryAfterNs`` 0: an attempt without a result yet, or a completed
# one), ``backoff`` (``retryAfterNs`` set) and ``busy_retry`` (a short retry after another
# install held the prepare lease, at most 30 in a row). A request before the due time
# and a second result of the same request change nothing; a completed run clears the
# failure count and stays ``requested``.
_MAINTENANCE = 'memvane.learning.selector_maintenance.'
M10 = Machine(
    'M10', 'selector request', store='selector-heal-request.json',
    lock='sync owner lock', owner='session, sync and wake requests; the maintenance child (result)',
    states=(ABSENT, 'requested', 'backoff', 'busy_retry'),
    terminal=(),
    transitions={
        **{(state, 'request_due'): 'requested' for state in (ABSENT, 'backoff', 'busy_retry')},
        **{(state, 'spawn_failed'): 'backoff' for state in (ABSENT, 'requested', 'busy_retry')},
        ('requested', 'failed'): 'backoff',
        ('requested', 'busy'): 'busy_retry',
    },
    where={'request_due': _MAINTENANCE + '_request_owned', 'spawn_failed': _MAINTENANCE + '_request_owned',
           'failed': _MAINTENANCE + '_record_request_result', 'busy': _MAINTENANCE + '_record_request_result'})

# M12: the setup journal of an enrollment. ``activate`` writes the configuration of a
# namespace that has none (no journal, or a recovered one) and refuses any other state;
# ``recover`` restores the bytes the backup holds and can be entered again at any point.
# The default enrollment recovers an unfinished journal before it activates anew.
_SETUP = 'memvane.learning.project_learning_setup.'
_RECOVERY = 'memvane.learning.project_learning_recovery.'
M12 = Machine(
    'M12', 'enrollment setup', store='project-learning-setup.json (phase)',
    lock='namespace lock, settings lock',
    owner='default enrollment and the setup request (activate), recovery (recover)',
    states=(ABSENT, 'prepared', 'installing', 'activated', 'recovering', 'recovered'),
    terminal=(),
    transitions={
        **{(state, 'activate'): 'activated' for state in (ABSENT, 'recovered')},
        **{(state, 'activate_stops_prepared'): 'prepared' for state in (ABSENT, 'recovered')},
        **{(state, 'activate_stops_installing'): 'installing' for state in (ABSENT, 'recovered')},
        **{(state, 'recover'): 'recovered' for state in ('prepared', 'installing', 'activated', 'recovering')},
        **{(state, 'recover_stops'): 'recovering' for state in ('prepared', 'installing', 'activated')},
    },
    where={'activate': _SETUP + 'activate', 'activate_stops_prepared': _SETUP + 'activate',
           'activate_stops_installing': _SETUP + 'activate', 'recover': _RECOVERY + 'recover',
           'recover_stops': _RECOVERY + 'recover'})

# M13: the renewal of a default enrollment's approval files to the installed bundle. The
# intent holds the old and the new bytes of every target and fences the consumers
# (``renewal_pending``) while it exists. A pass that finds an intent finishes it when the
# target is still the recorded one and otherwise restores the old bytes, then plans anew.
# A refusal (changed inputs, a mixed state, a newer owner) writes nothing.
_RENEWAL = 'memvane.learning.default_renewal.'
M13 = Machine(
    'M13', 'approval renewal', store='project-learning-renewal.json',
    lock='prepare lease, namespace lock, settings lock, learning-profile.lock',
    owner='selector maintenance (renew)',
    states=(ABSENT, 'intent', 'rolling_back'),
    terminal=(),
    transitions={
        (ABSENT, 'publish_interrupted'): 'intent',
        ('intent', 'rollback_interrupted'): 'rolling_back',
        ('intent', 'renew'): ABSENT,
        ('rolling_back', 'renew'): ABSENT,
    },
    where={'publish_interrupted': _RENEWAL + '_publish', 'rollback_interrupted': _RENEWAL + '_restore',
           'renew': _RENEWAL + 'renew'})

# M14: the switch of a default-local enrollment to the default teacher. Like M13 an intent
# with both byte sets fences the consumers (``teacher_enable_pending``). A pass finishes
# the intent when its inputs still hold and rolls it back otherwise; files that are
# neither the old nor the new bytes stop every writer (``teacher_enable_mixed_state``)
# until the explicit restore. Five failures with one reason on one version stop requests.
_ENABLE = 'memvane.learning.default_teacher_enable.'
M14 = Machine(
    'M14', 'default teacher enable', store='project-teacher-enable.json',
    lock='namespace lock, settings lock, learning-profile.lock, project-worker.lock',
    owner='selector maintenance (run), the restore request',
    states=(ABSENT, 'intent', 'rolling_back'),
    terminal=(),
    transitions={
        (ABSENT, 'forward_interrupted'): 'intent',
        ('intent', 'rollback_interrupted'): 'rolling_back',
        ('intent', 'run'): ABSENT,
        ('rolling_back', 'run'): ABSENT,
        ('intent', 'restore'): ABSENT,
        ('rolling_back', 'restore'): ABSENT,
    },
    where={'forward_interrupted': _ENABLE + '_forward', 'rollback_interrupted': _ENABLE + '_rollback',
           'run': _ENABLE + 'run', 'restore': _ENABLE + 'restore'})

# M16: the warm-up of one selector generation in the running inference service. The
# marker records the key (checkpoint, input view, receipt and the running service) of
# the last successful warm-up; a new checkpoint or receipt, or a restarted service, makes
# it stale. A warm-up that is deferred, fails, or finds another worker holding the lock
# writes nothing.
_HEAL = 'memvane.learning.context_learning.selector_heal.'
M16 = Machine(
    'M16', 'selector warm-up', store='selector-warm.json',
    lock='selector-warm.lock', owner='selector maintenance and the default enrollment (warm)',
    states=(ABSENT, 'warm', 'stale'),
    terminal=(),
    transitions={
        (ABSENT, 'warm'): 'warm',
        ('stale', 'warm'): 'warm',
        ('warm', 'new_checkpoint'): 'stale',
        ('warm', 'service_restart'): 'stale',
    },
    where={'warm': _HEAL + 'warm', 'new_checkpoint': _APPLY,
           'service_restart': 'memvane.learning.context_learning.inference_service'})

MACHINES = (M1, M1U, M1T, M2, M3, M4, M5, M6, M7, M8, M9, M10, M12, M13, M14, M15, M16)

# Every status a worker pass can end with (the cycle step's result is the pass result),
# as (class, the event row that says so, what the pass leaves). The classes:
# ``retry``   the worker asks for another wake (``memvane.runtime.types.RETRY``);
# ``waiting`` more input is needed (gold, cases after the cutoff, a setting); the next
#             wake a hook requests enters again;
# ``done``    the candidate is applied;
# ``closed``  a job, an attempt or a candidate ended with its reason, and later passes
#             go on with what remains;
# ``held``    every pass answers the same until something outside the pass changes; the
#             worker writes one ``learning.cycle`` ``held`` row when a pass first ends so.
_SCORE, _CYCLE, _PROMOTION = 'learning.score', 'learning.cycle', 'learning.promotion'
CYCLE_RESULTS = {
    'busy': ('retry', None, 'nothing: another worker holds the lock'),
    'setup_in_progress': ('retry', None, 'nothing: the setup has not finished'),
    'deferred_resource': ('retry', _CYCLE + ' deferred_resource', 'the job and the evaluation deferred with a backoff'),
    'deferred_quota': ('retry', None, 'nothing (explicit profile only)'),
    'staged_evaluation_deferred': ('retry', None, 'the evaluation deferred with a backoff'),
    'apply_deferred': ('retry', None, 'the application journal with its retry time and failures'),
    'disabled': ('waiting', None, 'nothing: capture or the cycle is off in the settings'),
    'waiting_fresh_train': ('waiting', _SCORE, 'gold cases below the training size'),
    'waiting_fresh_batch': ('waiting', _SCORE, 'no new job: too few fresh gold cases'),
    'staged_waiting_quality_cohort': ('waiting', _SCORE, 'a staged cycle waiting for evaluation cases'),
    'staged_evaluated': ('waiting', _PROMOTION + ' evaluated', 'an evaluated cycle (applied by the next step when the profile applies)'),
    'already_staged_evaluated': ('waiting', None, 'an evaluated cycle, unchanged'),
    'applied': ('done', _PROMOTION + ' applied', 'the active pointer is the candidate'),
    'already_applied': ('done', None, 'the applied cycle, unchanged'),
    'failed': ('closed', _CYCLE + ' failed', 'the job deferred with one more failure (blocked at three)'),
    'learning_job_blocked': ('closed', _CYCLE + ' blocked', 'the blocked job until its rearm time'),
    'staged_source_revoked': ('closed', _CYCLE + ' blocked', 'the job or the candidate closed as source_revoked'),
    'apply_blocked': ('closed', _PROMOTION + ' rejected', 'a rejected journal or a counted failure on the evaluation'),
    'staged_cohort_changed': ('closed', None, 'the evaluation deferred: its rows moved under the attempt'),
    'staged_authorization_expired': ('closed', None, 'the evaluation deferred until renewal carries or closes it'),
    'staged_artifact_changed': ('held', _CYCLE + ' held', 'a staged cycle whose artifact or evidence no longer matches'),
    'cohort_cycle_conflict': ('held', _CYCLE + ' held', 'a cycle row in a state training cannot enter'),
    'evaluation_stage_unavailable': ('held', _CYCLE + ' held', 'an evaluation whose cycle or job row is missing'),
    'experiment_paused': ('held', _CYCLE + ' held', 'no cycle: the training authorization is refused'),
    'existing_trial_requires_review': ('held', _CYCLE + ' held', 'an experimental checkpoint from an older flow'),
    'multilingual_policy_required': ('held', _CYCLE + ' held', 'a policy with another model input view'),
    'cohort_source_revoked': ('held', _CYCLE + ' held', 'more than 2,000 admitted rows'),
    'runtime_approval_required': ('held', _CYCLE + ' held', 'no valid runtime approval'),
    'synthetic_training_budget_exhausted': ('held', _CYCLE + ' held', 'the synthetic QA profile used its budget'),
}

# M11: one selector maintenance pass has no durable state of its own. Every way it ends,
# as (what the request file gets, the ``learning.selector.maintenance`` outcome and
# reason, the stage the event names). ``incomplete`` results carry their own name in the
# event's ``incomplete`` field. A raised failure names the stage it was in. The request
# settlements are M10's: ``completed`` clears the counts, ``failed`` and ``deferred`` back
# off, ``busy`` is the short retry, and ``kept`` writes a backoff without a wake re-offer.
_INCOMPLETE = ('deferred', 'deferred', 'maintenance_incomplete')
MAINTENANCE_RESULTS = {
    'completed': ('completed', 'completed', None, None),
    'learning_runtime_pending': ('kept', 'deferred', 'learning_runtime_pending', None),
    'version_invalid': (*_INCOMPLETE, 'proof'),
    'prepare_status': (*_INCOMPLETE, 'prepare'),
    'renewal_refused': (*_INCOMPLETE, 'profile'),
    'teacher_enable_pending': (*_INCOMPLETE, 'profile'),
    'selector_state_missing': (*_INCOMPLETE, 'profile'),
    'heal_status': (*_INCOMPLETE, 'heal'),
    'warm_status': (*_INCOMPLETE, 'warm'),
    'publish_owner_busy': (*_INCOMPLETE, 'publish'),
    'install_changed': (*_INCOMPLETE, 'publish'),
    'learning_prepare_busy': ('busy', 'deferred', 'learning_prepare_busy', 'the stage it was in'),
    'resource_deferred': ('deferred', 'deferred', 'the fixed code, else resource_deferred', 'the stage it was in'),
    'maintenance_failed': ('failed', 'failed', 'the fixed code, else maintenance_failed', 'the stage it was in'),
    'maintenance_timeout': ('failed', 'failed', 'maintenance_timeout', 'the stage it was in'),
}
