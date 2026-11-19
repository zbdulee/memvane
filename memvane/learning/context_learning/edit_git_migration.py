"""One-time deletion of edit cases captured without the git-ignore check.

Decision 11 refuses an edit case whose edited path git ignores. Before the
fix the check ran only when the project root itself held ``.git``, so a
project in a subdirectory of a git work tree stored such cases. Decision 8
deletes, for an affected project (no ``.git`` entry in its root, inside a git
work tree), the shared ``cases`` rows of schema 5 (``context_origin:
tool_edit``) captured before the fix and the private ``samples`` rows of the
same request ids with schema 5. Nothing else changes: other schemas, other
projects, families and cohort tables stay.

"Before the fix" is the earlier of two times: the fix time the fixed capture
path records (``record_fix_time``) before it queues its first edit case of a
project, and the migration's own start. Rows whose ``captured_at`` is earlier
are deleted; a case the fixed code queued is never earlier than its fix time.
A per-project marker makes later passes do nothing; its counts and one
``learning.migration`` event carry only numbers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

FIX_PREFIX = 'edit-git-check-fixed.'
MARKER_PREFIX = 'edit-git-migration.'
PENDING_PREFIX = 'edit-git-migration-pending.'
FIX_SCHEMA = 'memvane-edit-git-check-fixed-v1'
SCHEMA = 'memvane-edit-git-migration-v1'


def _name(prefix, project):
    from . import shared
    return prefix + shared._project_key(project) + '.json'


def record_fix_time(root, project) -> None:
    """Record once when fixed capture first queues an edit case of ``project``.

    The time is taken before the file exists and never moves forward: a
    second writer finds the file and leaves it. Raises on I/O errors so the
    caller does not queue a case without its fix time.
    """
    from memvane.kernel.atomic_rename import publish_new
    root = Path(root)
    target = root / _name(FIX_PREFIX, project)
    if os.path.lexists(target):
        return
    fixed_at = time.time()
    data = json.dumps({'schema': FIX_SCHEMA, 'fixedAt': fixed_at}, sort_keys=True,
                      separators=(',', ':')).encode() + b'\n'
    fd, name = tempfile.mkstemp(prefix='.write-', dir=str(root))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        try:
            publish_new(name, target)
        except FileExistsError:
            pass  # An earlier writer's time stays.
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def _fix_time(root, project):
    """The recorded fix time, or None when absent or unreadable."""
    from .dataset import read_private_json
    path = Path(root) / _name(FIX_PREFIX, project)
    if not os.path.lexists(path):
        return None
    try:
        value = read_private_json(path)
        fixed_at = value.get('fixedAt') if value.get('schema') == FIX_SCHEMA else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None  # The migration's own start then bounds the cutoff.
    return float(fixed_at) if type(fixed_at) in (int, float) else None


def affected(project) -> bool:
    """No ``.git`` in the project root, and the root is inside a git work tree.

    Raises ValueError('edit_git_check_failed') when git cannot answer.
    """
    from .seam import inside_git_work_tree
    project = Path(project)
    if os.path.lexists(project / '.git'):
        return False
    return inside_git_work_tree(project)


def _unchecked(sample_json) -> bool:
    try:
        sample = json.loads(sample_json)
        return (type(sample.get('schema_version')) is int and sample['schema_version'] == 5
                and sample.get('context_origin') == 'tool_edit')
    except (ValueError, TypeError, AttributeError):
        return False


def migrate(root, project, config) -> dict:
    """Delete pre-fix edit cases of an affected project once.

    Returns ``{'status', 'count', 'privateSamples'}``. Raises on I/O and git
    errors; then no marker is written and the next pass retries.
    """
    from .dataset import read_private_json
    from .local_cycle import _atomic_json
    from . import shared
    root, project = Path(root), Path(project).resolve(strict=True)
    marker = root / _name(MARKER_PREFIX, project)
    pending_path = root / _name(PENDING_PREFIX, project)
    if os.path.lexists(marker):
        read_private_json(marker)  # an unsafe marker refuses, it is never replaced
        return {'status': 'already_migrated', 'count': 0, 'privateSamples': 0}
    pending = None
    if os.path.lexists(pending_path):
        pending = read_private_json(pending_path)
        if (pending.get('schema') != SCHEMA or type(pending.get('cutoff')) not in (int, float)
                or type(pending.get('count')) is not int
                or type(pending.get('privateSamples')) is not int):
            raise ValueError('edit_git_migration_pending_invalid')
        cutoff = float(pending['cutoff'])
    else:
        started = time.time()
        if not affected(project):
            _atomic_json(marker, {'schema': SCHEMA, 'status': 'not_affected', 'count': 0,
                                  'privateSamples': 0,
                                  'migratedAtUnixMs': int(time.time() * 1000)})
            _record(project, 'not_affected', 0, 0)
            return {'status': 'not_affected', 'count': 0, 'privateSamples': 0}
        fixed_at = _fix_time(root, project)
        cutoff = started if fixed_at is None else min(fixed_at, started)
    key = shared._project_key(project)
    database = root / shared.DATABASE_NAME
    ids = []
    if os.path.lexists(database):
        with shared._db(root) as db:
            ids = [rid for rid, sample in db.execute(
                'SELECT request_id,sample FROM cases WHERE project_key=? AND captured_at<?',
                (key, cutoff)) if _unchecked(sample)]
    from .seam import _private_state
    state = _private_state(config, project, create=False)
    private_db = (state / 'learning.sqlite3') if state is not None else None
    if private_db is not None and not private_db.is_file():
        private_db = None
    if pending is None and ids:
        planned = 0
        if private_db is not None:
            from .store import database as open_private
            with open_private(state) as db:
                for rid in ids:
                    planned += db.execute(
                        'SELECT COUNT(*) FROM samples WHERE request_id=? AND schema_version=5',
                        (rid,)).fetchone()[0]
        pending = {'schema': SCHEMA, 'cutoff': cutoff, 'count': len(ids),
                   'privateSamples': planned}
        # The counts are durable before any delete commits, so a crash after
        # the deletes still reports them on the next pass.
        _atomic_json(pending_path, pending)
    if ids:
        # Private rows first: a crash before the case delete leaves the case,
        # which the next pass deletes; never a private row without its case.
        if private_db is not None:
            from .store import database as open_private
            with open_private(state) as db:
                for rid in ids:
                    db.execute('DELETE FROM samples WHERE request_id=? AND schema_version=5',
                               (rid,))
        with shared._db(root) as db:
            for rid in ids:
                db.execute('DELETE FROM cases WHERE project_key=? AND request_id=?', (key, rid))
    count = pending['count'] if pending is not None else 0
    private = pending['privateSamples'] if pending is not None else 0
    _atomic_json(marker, {'schema': SCHEMA, 'status': 'deleted', 'count': count,
                          'privateSamples': private,
                          'migratedAtUnixMs': int(time.time() * 1000)})
    if os.path.lexists(pending_path):
        os.unlink(pending_path)
    _record(project, 'deleted', count, private)
    return {'status': 'deleted', 'count': count, 'privateSamples': private}


def _record(project, status, count, private) -> None:
    try:
        from memvane.journal.api import select_route
        from memvane.diagnostics.api import emit
        decision = select_route(str(project), action='update')
        emit('learning.migration',
             outcome='edit_cases_deleted' if status == 'deleted' else 'edit_cases_kept',
             reason=('edit_git_check_missing' if status == 'deleted'
                     else 'edit_git_check_not_affected'),
             decision=decision, count=count, privateSamples=private)
    except Exception:
        pass  # The marker keeps the counts; the event is diagnostic only.


def migrate_quietly(root, project, config) -> dict:
    """``migrate`` for the worker pass: a failure is reported, never raised."""
    try:
        return migrate(root, project, config)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return {'status': 'migration_deferred', 'count': 0, 'privateSamples': 0}
