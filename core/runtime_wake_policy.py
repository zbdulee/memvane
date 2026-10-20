"""Pure wake decisions for runtime_work: validation, ordering, retry and ACK.

No I/O lives here. runtime_work owns SQLite, the dispatch lease and admission
sampling; runtime_handlers owns handler side effects. This module only answers
"is this wake well-formed", "does this kind wait for compute admission", "which
due wake runs next", "did the handler finish" and "when does a failed attempt
come back". Keeping the answers pure lets tests pin the old retry/busy
semantics without a database or a real handler.
"""
from __future__ import annotations

import hashlib
from pathlib import PurePath

# Lower value runs first. learning and auto share a class on purpose: both are
# best-effort background work that must never starve index/update/cleanup.
KINDS = {'index': 0, 'update': 1, 'cleanup': 2, 'laya-sync': 2,
         'laya-compile': 2,
         'learning': 3, 'auto': 3}
# Handler statuses meaning "not done yet, keep the wake and back off".
RETRY = {'busy', 'deferred_resource', 'deferred_quota', 'staged_evaluation_deferred', 'apply_deferred',
         'setup_required', 'setup_in_progress'}
MAX_WAKES = 256
# Capacity-deferred wakes kept for promotion when a slot frees; coalesced by
# wake_key like the queue itself. Only beyond this is a wake dropped.
MAX_OVERFLOW = 1024
MAX_TARGET_BYTES = 4096
RETRY_BASE_SECS = 5
RETRY_MAX_EXPONENT = 6
RETRY_MAX_SECS = 300
IDLE_WAIT_SECS = 5
# Reason recorded when a handler returns falsy without raising.
NOT_DONE_REASON = 'pending'
CLEANUP_DEFERRED_REASON = 'resource_deferred'


def validate_wake(kind, target):
    """Raise ValueError('invalid_runtime_wake') unless (kind, target) is fixed-shape."""
    if kind not in KINDS or not isinstance(target, str) or len(target.encode()) > MAX_TARGET_BYTES:
        raise ValueError('invalid_runtime_wake')
    if kind == 'index':
        if target:
            raise ValueError('invalid_runtime_wake')
    elif kind == 'update':
        if len(target) != 32 or any(c not in '0123456789abcdef' for c in target):
            raise ValueError('invalid_runtime_wake')
    elif not PurePath(target).is_absolute() or '\x00' in target:
        raise ValueError('invalid_runtime_wake')


def wake_key(kind, target):
    """Coalescing identity: one row per (kind, target); repeats bump its revision."""
    return hashlib.sha256((kind + '\0' + target).encode()).hexdigest()


def capacity_limit(kind, max_wakes=MAX_WAKES):
    """Row count at which a *new* wake is refused; one slot stays reserved for index."""
    return max_wakes if kind == 'index' else max_wakes - 1


def needs_compute_admission(kind):
    """Only learning/auto wait for the shared compute admission before dispatch.

    The wake is only a dispatch hint. Index/update/cleanup and Laya work have
    their own admission and retained content queues in their handlers, so a
    training-sized memory reserve must not hold those wakes here.
    """
    return kind in ('learning', 'auto')


def due_rows(rows, now):
    return [row for row in rows if row['due'] <= now]


def next_row(due):
    """Highest-priority due wake: kind class, then oldest due, then id."""
    return min(due, key=lambda row: (KINDS[row['kind']], row['due'], row['id']))


def idle_wait(remaining):
    """Sleep while nothing is due or admission says no, never past the deadline."""
    return min(IDLE_WAIT_SECS, max(0, remaining))


def handler_done(status):
    """ACK predicate for status-returning handlers (learning, auto)."""
    return status not in RETRY


def cleanup_done(answer):
    """ACK predicate for orphan_reclaim --json output (only resource deferral retries)."""
    return answer.get('reason') != CLEANUP_DEFERRED_REASON


def retry_delay(attempts):
    """Exponential backoff from prior attempts: 5, 10, 20 ... capped at 300 seconds."""
    return min(RETRY_MAX_SECS, RETRY_BASE_SECS * 2 ** min(attempts, RETRY_MAX_EXPONENT))


def failure_reason(error):
    return NOT_DONE_REASON if error is None else type(error).__name__


def settle(attempts, *, done, now, error=None):
    """Decide what happens to a claimed wake after one execution.

    ack: delete the claimed revision; a newer revision (enqueued while running)
    survives and is re-pended at ``due`` without counting an attempt.
    retry: keep the row, push ``due`` back by the backoff and count the attempt.
    """
    if done and error is None:
        return {'ack': True, 'due': now, 'reason': None}
    return {'ack': False, 'due': now + retry_delay(attempts), 'reason': failure_reason(error)}
