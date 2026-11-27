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
by this limit. The snapshot keeps the newest rows, the rows of every open
candidate and the rows the last snapshot kept, so a candidate older than the
newest rows that ends its evaluation is progress too.

A count that cannot be written cannot go on: the request settles at once with
the ``retry_cap`` row (``detail`` ``retry_count_unwritable``), as at the limit.
A file without a saved state (written before states were kept) is no
observation: the count starts over from the state read now.
"""
from __future__ import annotations

from pathlib import Path

NAME = 'worker-retry.json'
# About 24 hours of passes at the wake's 300-second backoff ceiling.
RETRY_RESULT_LIMIT = 288
# The rows of each table a snapshot keeps (the newest, besides those of open candidates
# and those the last snapshot kept): the file stays small.
SNAPSHOT_ROWS = 64
# A candidate is open while its cycle waits to be evaluated or applied.
OPEN_CYCLES = ('staged', 'staged_evaluated')
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


def snapshot(root, before=None):
    """The durable state of the namespace's cycles, as ``moved`` compares it.

    Reads only. The rows are the newest ``SNAPSHOT_ROWS``, those of open candidates
    and those named in ``before`` (the last snapshot), whatever their age.
    ``unread`` names the parts that could not be read this time (a locked
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
    state['groups'], state['unread'], state['carried'] = {}, [], []
    try:
        queue = LearningQueue(root)
        with queue.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            open_cycles = set()
            if 'cycles' in tables:
                open_cycles = {row[0] for row in db.execute(
                    'SELECT cycle_id FROM cycles WHERE status IN (%s)' % ','.join('?' * len(OPEN_CYCLES)),
                    OPEN_CYCLES)}
            if 'learning_jobs' in tables:
                state['M3'] = dict(db.execute(
                    'SELECT job_id,status FROM learning_jobs WHERE namespace=? ORDER BY rowid DESC LIMIT ?',
                    (queue.scope, SNAPSHOT_ROWS)))
                newest = set(state['M3'])
                state['M3'].update(_rows(db, 'SELECT job_id,status FROM learning_jobs WHERE namespace=? AND job_id IN',
                                         queue.scope, (open_cycles | set(_kept(before, 'M3'))) - newest))
                state['carried'] += [(name, key) for key in set(state['M3']) - newest - open_cycles
                                     for name in ('M3', 'M4')]
            if 'cycles' in tables and state['M3']:
                marks = ','.join('?' * len(state['M3']))
                state['M4'] = dict(db.execute(
                    'SELECT cycle_id,status FROM cycles WHERE cycle_id IN (%s)' % marks, tuple(state['M3'])))
            if 'learning_evaluations' in tables:
                state['M5'] = dict(db.execute(
                    'SELECT cycle_id,status FROM learning_evaluations WHERE namespace=? ORDER BY rowid DESC LIMIT ?',
                    (queue.scope, SNAPSHOT_ROWS)))
                newest = set(state['M5'])
                state['M5'].update(_rows(
                    db, 'SELECT cycle_id,status FROM learning_evaluations WHERE namespace=? AND cycle_id IN',
                    queue.scope, (open_cycles | set(_kept(before, 'M5'))) - newest))
                # Read only because the last snapshot kept them: compared once, then not kept.
                state['carried'] += [('M5', key) for key in set(state['M5']) - newest - open_cycles]
    except Exception:  # noqa: BLE001 - not read: the rows and the groups of their cycles
        state['M3'], state['M4'], state['M5'], state['carried'] = {}, {}, {}, []
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


def _kept(before, name):
    """The row keys of ``name`` in the last snapshot; none when there is none."""
    rows = before.get(name) if isinstance(before, dict) else None
    return [key for key in rows if isinstance(key, str)] if isinstance(rows, dict) else []


def _rows(db, query, scope, keys):
    """``{key: status}`` of ``query`` (ending ``IN``) for ``keys``, read in bounded batches."""
    found, keys = {}, sorted(keys)
    for start in range(0, len(keys), 500):
        batch = keys[start:start + 500]
        found.update(db.execute(query + ' (%s)' % ','.join('?' * len(batch)), (scope, *batch)))
    return found


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
    if (not isinstance(row, dict) or type(row.get('count')) is not int or row['count'] < 1
            or 'state' not in row):
        # No file, or one written before states were kept: no observation to count from.
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
    now = snapshot(root, before)
    unread = now.pop('unread', ())
    carried = now.pop('carried', ())
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
    for name, key in carried if now is not None else ():
        now[name].pop(key, None)
        now['groups'].pop(key, None)
    count += 1
    detail = None
    if count < RETRY_RESULT_LIMIT:
        from .local_cycle import _atomic_json
        try:
            _atomic_json(path, {'schema': 1, 'status': status, 'count': count, 'state': now})
            return True
        except (OSError, ValueError):
            # Checked: a count that is not written stays where it was on every later result,
            # so the request would never reach the limit. It settles now instead.
            detail = 'retry_count_unwritable'
    _clear(path)
    try:
        from memvane.diagnostics.api import emit
        from memvane.journal.api import select_route
        emit('learning.cycle', outcome='retry_cap', reason=status, retries=count,
             decision=select_route(str(project), action='update'), **({'detail': detail} if detail else {}))
    except Exception:
        pass  # Diagnostic only: the settled request is the record.
    return False

