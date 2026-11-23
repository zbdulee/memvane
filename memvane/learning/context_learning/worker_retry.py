"""The end of a worker request that keeps asking to be woken again.

An owned worker child that ends with a retry result (``memvane.runtime.types.RETRY``:
resources not ready, a deferred evaluation or application) leaves its request
pending and asks for another ``learning`` wake. ``again`` counts such results in
a row in ``worker-retry.json``, whatever their status: resource deferrals and a
busy lock come and go with the machine's load and must not hide an application
or evaluation that never gets done. At ``RETRY_RESULT_LIMIT`` the request is
settled with the last status instead (no further wake) and one
``learning.cycle`` row says so (``retry_cap`` with that status as its reason
and the count). The next hook launch writes a new request, and the count
starts over. A result that is not a retry clears the count.

Only passes without progress are counted. Progress has one definition,
``moved``: between the durable state kept at the last retry result
(``snapshot``, in the same file) and the state now, a job, cycle, evaluation,
application or the active pointer went forward (``FORWARD``), or the training
cursor saved more groups. Such a pass starts the count over, whichever way it
ended and whatever its result says: a job that keeps moving is never settled
by this limit.
"""
from __future__ import annotations

from pathlib import Path

NAME = 'worker-retry.json'
# About 24 hours of passes at the wake's 300-second backoff ceiling.
RETRY_RESULT_LIMIT = 288
# The rows of each table a snapshot keeps (the newest): the file stays small.
SNAPSHOT_ROWS = 64
ABSENT = '(absent)'
# Per cycle machine of the learning state model (M3 job, M4 cycle, M5 evaluation, M6
# application): new state -> the old states from which reaching it is progress (a job
# trained or closed, an evaluation's metrics stored or the evaluation closed, a pointer
# committed, an application activated or ended; several steps in one pass included).
# Waiting, retrying, an intent or a preparation formed again, and going back are not here.
# The model derives this table from its progress events (``forward_pairs``); product code
# does not import the model, so it is written out and ``test_cycle_results`` compares them.
FORWARD = {
    'M3': {'blocked': ('(absent)', 'deferred', 'queued', 'running'), 'queued': ('deferred', 'running'),
           'staged': ('(absent)', 'blocked', 'deferred', 'queued', 'running')},
    'M4': {'applied': ('(absent)', 'deferred', 'failed', 'staged', 'staged_evaluated', 'training'),
           'staged': ('(absent)', 'deferred', 'failed', 'training'),
           'staged_evaluated': ('(absent)', 'deferred', 'failed', 'staged', 'training')},
    'M5': {'blocked': ('(absent)', 'completed', 'deferred', 'queued', 'running', 'waiting_quality'),
           'completed': ('(absent)', 'blocked', 'deferred', 'queued', 'running', 'waiting_quality'),
           'queued': ('deferred', 'running')},
    'M6': {'activated': ('(absent)', 'intent', 'pointer_committed', 'prepared'),
           'pointer_committed': ('(absent)', 'intent', 'prepared'),
           'rejected': ('(absent)', 'activated', 'intent', 'pointer_committed', 'prepared')},
}


def moved(before, after):
    """Whether the durable state of a cycle went forward between two snapshots.

    A row of a cycle machine crossed a ``FORWARD`` pair, the active pointer (M7, kept as
    its digest) changed, or the training cursor of a cycle saved more optimizer groups.
    A change the table does not list is not progress, and neither is a cursor that was
    not read before or now.
    """
    for name, table in FORWARD.items():
        old, new = before.get(name) or {}, after.get(name) or {}
        for key in set(old) | set(new):
            if old.get(key, ABSENT) in table.get(new.get(key, ABSENT), ()):
                return True
    first, last = (before.get('M7') or {}).get('pointer'), (after.get('M7') or {}).get('pointer')
    if first is not None and last is not None and first != last:
        return True
    saved, now = before.get('groups') or {}, after.get('groups') or {}
    # A cursor that could not be read (``None``) is no observation on either side.
    return any(type(step) is int and step > saved.get(cycle, -1) for cycle, step in now.items()
               if type(saved.get(cycle, -1)) is int)


def snapshot(root):
    """The durable state of the namespace's cycles, as ``moved`` compares it.

    Reads only. ``unread`` names the parts that could not be read this time (a locked
    database, a file being replaced): they are not a state, and ``again`` keeps what
    was read last for them, so a failed read neither looks like progress when the next
    read succeeds nor clears the count.
    """
    from . import candidate_apply
    from .dataset import read_private_json
    from .learning_profile import digest
    from .learning_queue import LearningQueue
    root = Path(root)
    state = {name: {} for name in (*FORWARD, 'M7')}
    state['groups'], state['unread'] = {}, []
    try:
        queue = LearningQueue(root)
        with queue.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'learning_jobs' in tables:
                state['M3'] = dict(db.execute(
                    'SELECT job_id,status FROM learning_jobs WHERE namespace=? ORDER BY rowid DESC LIMIT ?',
                    (queue.scope, SNAPSHOT_ROWS)))
            if 'cycles' in tables and state['M3']:
                marks = ','.join('?' * len(state['M3']))
                state['M4'] = dict(db.execute(
                    'SELECT cycle_id,status FROM cycles WHERE cycle_id IN (%s)' % marks, tuple(state['M3'])))
            if 'learning_evaluations' in tables:
                state['M5'] = dict(db.execute(
                    'SELECT cycle_id,status FROM learning_evaluations WHERE namespace=? ORDER BY rowid DESC LIMIT ?',
                    (queue.scope, SNAPSHOT_ROWS)))
    except Exception:  # noqa: BLE001 - not read: the rows and the groups of their cycles
        state['M3'], state['M4'], state['M5'] = {}, {}, {}
        state['unread'] += ['M3', 'M4', 'M5', 'groups']
    for cycle, status in state['M4'].items():
        if status in ('training', 'deferred', 'failed'):
            state['groups'][cycle] = _groups(root / 'cycles' / cycle / 'resume' / 'cursor.json')
    # A file that is not there is a state (no journal, no pointer); one that cannot be read is not.
    for name, path in (('M6', root / candidate_apply.JOURNAL), ('M7', root / 'active-checkpoint.json')):
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            if name == 'M6':
                row = candidate_apply.read_journal(root)
                state['M6'] = {row['apply_id']: row['phase']}
            else:
                # M7 as the pointer itself: whichever way it changed, it was written.
                state['M7'] = {'pointer': digest(read_private_json(path))}
        except Exception:  # noqa: BLE001
            state['unread'].append(name)
    return state


def _groups(path):
    """Saved optimizer groups of a training cursor: -1 without one, None when it cannot be read."""
    from .dataset import read_private_json
    if not (path.exists() or path.is_symlink()):
        return -1
    try:
        step = read_private_json(path).get('next_group')
    except (OSError, ValueError, AttributeError):
        return None
    return step if type(step) is int else None


def _saved(path):
    from .dataset import read_private_json
    try:
        row = read_private_json(path) if path.exists() else None
    except (OSError, ValueError, TypeError):
        row = None
    if not isinstance(row, dict) or type(row.get('count')) is not int or row['count'] < 1:
        return 0, None
    return row['count'], row.get('state') if isinstance(row.get('state'), dict) else None


def _clear(path):
    try:
        path.unlink()
    except OSError:
        pass  # Ignored: a count that stays is only reached sooner, and the next result rewrites it.


def again(root, project, status, retryable):
    """Whether this result keeps the request pending and asks for another wake."""
    path = Path(root) / NAME
    if not retryable:
        _clear(path)
        return False
    count, before = _saved(path)
    now = snapshot(root)
    unread = now.pop('unread', ())
    if unread and before is None:
        now = None  # Nothing read before either: no state is kept, and the next read is the first.
    for name in unread if now is not None else ():
        now[name] = before.get(name) or {}  # What was read last stands for what could not be read.
    if now is not None:
        # The same for one cycle's cursor: the groups read last, or none known (``None``).
        known = (before or {}).get('groups') or {}
        now['groups'] = {cycle: known.get(cycle) if step is None else step
                         for cycle, step in (now.get('groups') or {}).items()}
    if before is not None and moved(before, now):
        count = 0  # The pass moved: counted from here.
    count += 1
    if count < RETRY_RESULT_LIMIT:
        from .local_cycle import _atomic_json
        try:
            _atomic_json(path, {'schema': 1, 'status': status, 'count': count, 'state': now})
        except OSError:
            pass  # Ignored: the count is not advanced, so the limit comes one result later.
        return True
    _clear(path)
    try:
        from memvane.diagnostics.api import emit
        from memvane.journal.api import select_route
        emit('learning.cycle', outcome='retry_cap', reason=status, retries=count,
             decision=select_route(str(project), action='update'))
    except Exception:
        pass  # Diagnostic only: the settled request is the record.
    return False
