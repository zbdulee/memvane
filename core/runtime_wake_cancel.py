"""Revision-scoped cancel of one owned synthetic-QA ``laya-sync`` build wake.

Only a wake that can do nothing but drive an *inactive* initial build of a
release-QA fixture project is eligible. Ownership comes from official private
state, never from caller strings alone: the registered initial-build pointer
and record (``laya_initial_build.lookup``), the bound install journal (pending
candidate of the same install, nothing selected), the project still selecting
QMD with no Laya route, and the release-QA fixture markers. An online SQLite
backup is taken and verified before the single transaction that CAS-deletes
exactly (kind, target, revision, pending), removes a coalesced overflow row and
appends an audit row. A running, changed or re-enqueued wake is refused, so a
newer revision always survives. Replay never restores the same revision: the
official recovery is a fresh request (``index-build-rescan``).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

import runtime_wake_policy as wake_policy

KIND = 'laya-sync'
FIXTURE_SCHEMA = 'qmd-release-qa-fixture-v1'
AUDIT_TABLE = ('CREATE TABLE IF NOT EXISTS wake_cancellations (seq INTEGER PRIMARY KEY AUTOINCREMENT,'
               'at REAL NOT NULL,wake_id TEXT NOT NULL,kind TEXT NOT NULL,target TEXT NOT NULL,'
               'revision INTEGER NOT NULL,attempts INTEGER NOT NULL,reason TEXT,due REAL NOT NULL,'
               'overflow_removed INTEGER NOT NULL,proof_sha256 TEXT NOT NULL,'
               'backup_path TEXT NOT NULL,backup_sha256 TEXT NOT NULL)')


class CancelRefused(ValueError):
    pass


def _refuse(reason):
    raise CancelRefused(reason)


def _private_file(path, *, private=True):
    """Owner-only regular file, single link, no link anywhere in its path."""
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        _refuse('unsafe_proof_path')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_mode & (0o077 if private else 0o022)):
        _refuse('unsafe_proof_path')
    return info


def _project(raw):
    if not isinstance(raw, str) or not os.path.isabs(raw):
        _refuse('invalid_project')
    project = Path(raw)
    if any(p.is_symlink() for p in (project, *project.parents)) or not project.is_dir():
        _refuse('invalid_project')
    if str(project.resolve(strict=True)) != raw or project.stat().st_uid != os.getuid():
        _refuse('invalid_project')
    return project


def fixture_proof(project):
    """Release-QA fixture markers around ``project``; returns the fixture id.

    Shared by every QA wake cancel (``runtime_update_cancel`` too), so the
    meaning of "this project belongs to a release-QA fixture" stays one rule.
    """
    fixture = project.parent
    for name in ('MANIFEST.json', '.qa-fixture-owner'):
        _private_file(fixture / name, private=False)
    try:
        manifest = json.loads((fixture / 'MANIFEST.json').read_text())
        marker = json.loads((fixture / '.qa-fixture-owner').read_text())
    except (OSError, ValueError) as exc:
        raise CancelRefused('fixture_marker_invalid') from exc
    if (not isinstance(manifest, dict) or manifest.get('schema') != FIXTURE_SCHEMA
            or manifest.get('root') != str(fixture)
            or (manifest.get('paths') or {}).get('project') != str(project)
            or not isinstance(marker, dict) or marker.get('uid') != os.getuid()
            or marker.get('fixtureId') != manifest.get('fixtureId')):
        _refuse('fixture_marker_invalid')
    return manifest['fixtureId']


def ownership_proof(project_raw):
    """Read-only proof that the wake target is an owned, inactive QA build."""
    project = _project(project_raw)
    fixture_id = fixture_proof(project)
    import laya_initial_build as initial_build
    try:
        record = initial_build.lookup(project)
    except (OSError, ValueError) as exc:
        raise CancelRefused('initial_build_unavailable') from exc
    if record is None:
        _refuse('initial_build_not_registered')
    identity = record.get('identity') or {}
    state = Path(identity.get('stateRoot', ''))
    pointer = initial_build.pointer_path(project, initial_build._pointer_dir(create=False))
    _private_file(pointer)
    _private_file(state / 'initial-build.json')
    journal_path = Path(identity.get('journalPath', ''))
    _private_file(journal_path)
    if (identity.get('projectRoot') != str(project) or journal_path.parent != state
            or record.get('phase') in ('bound', 'retired')):
        _refuse('initial_build_not_inactive')
    from laya_install_journal import inspect_journal
    try:
        journal = inspect_journal(journal_path)
    except (OSError, ValueError) as exc:
        raise CancelRefused('journal_unavailable') from exc
    pending = journal.get('pending') or {}
    if (journal.get('state') != 'candidate_ready' or journal.get('selected') is not None
            or pending.get('installId') != identity.get('installId')):
        _refuse('journal_not_pending_candidate')
    from laya_index_host_route import select_route
    decision = select_route(str(project), action='update')
    if (decision.get('state'), decision.get('reason')) != ('legacy', 'no_route_record'):
        _refuse('project_route_not_original_qmd')
    proof = {'project': str(project), 'fixtureId': fixture_id,
             'installId': identity['installId'], 'stateRoot': str(state),
             'journalPath': str(journal_path), 'buildPhase': record.get('phase'),
             'journalEpoch': journal.get('epoch')}
    return {**proof, 'sha256': hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()}


def _row(db, key):
    found = db.execute('SELECT * FROM wakes WHERE id=?', (key,)).fetchone()
    return dict(found) if found is not None else None


ROW_VERSION = ('id', 'kind', 'target', 'revision', 'status', 'due', 'attempts', 'reason')


def _backup(queue, backup_dir, key, expected):
    """Online SQLite backup, verified to hold exactly the row about to be removed."""
    revision = expected['revision']
    directory = Path(backup_dir)
    if (not directory.is_absolute() or any(p.is_symlink() for p in (directory, *directory.parents))
            or not directory.is_dir()):
        _refuse('unsafe_backup_dir')
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        _refuse('unsafe_backup_dir')
    target = directory / f'runtime-wakes-before-cancel-{revision}-{time.time_ns()}.sqlite3'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    source = sqlite3.connect(queue.path.as_uri() + '?mode=ro', uri=True, timeout=.2)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    check = sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)
    try:
        check.row_factory = sqlite3.Row
        ok = check.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        row = _row(check, key)
    finally:
        check.close()
    if not ok or row is None or any(row[name] != expected[name] for name in ROW_VERSION):
        _refuse('backup_unverified')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {'path': str(target), 'sha256': digest, 'quickCheck': 'ok', 'row': row}


def status(project_raw, *, queue=None):
    """Read-only: the current wake row for this project and its cancel audit."""
    from runtime_work import WakeQueue
    queue = queue or WakeQueue()
    key = wake_policy.wake_key(KIND, str(project_raw))
    if not queue.path.exists():
        return {'wake': None, 'cancellations': []}
    db = sqlite3.connect(queue.path.as_uri() + '?mode=ro', uri=True, timeout=.2)
    try:
        db.row_factory = sqlite3.Row
        audits = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='wake_cancellations'").fetchone():
            audits = [dict(r) for r in db.execute(
                'SELECT * FROM wake_cancellations WHERE wake_id=? ORDER BY seq', (key,))]
        overflow = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='wake_overflow'").fetchone()
                        and db.execute('SELECT 1 FROM wake_overflow WHERE id=?', (key,)).fetchone())
        return {'wake': _row(db, key), 'overflow': overflow, 'cancellations': audits}
    finally:
        db.close()


def _state_locks(state_root):
    """Repository order: install lock, then build owner; both non-blocking."""
    from contextlib import ExitStack
    import laya_initial_build as initial_build
    from laya_product_install import InstallBlocked, _install_lock
    stack = ExitStack()
    try:
        stack.enter_context(_install_lock(Path(state_root)))
        if not stack.enter_context(initial_build.owner(state_root)):
            _refuse('state_busy')
    except InstallBlocked as exc:
        stack.close()
        raise CancelRefused('state_busy') from exc
    except BaseException:
        stack.close()
        raise
    return stack


def cancel(project_raw, revision, backup_dir, *, queue=None, clock=time.time):
    """``applied`` / ``unchanged`` / ``refused``; never deletes another row version.

    Ownership is proven, then re-proven while holding the install lock and the
    build owner (the locks finalize/activate/rescan and build advancement
    take), and once more inside the queue transaction before commit. The CAS
    matches the full row version the verified backup holds, so a same-revision
    retry (attempts/due/reason) or a newer revision is never deleted. A refusal
    commits nothing to the queue; only an already-written backup file remains.
    """
    if type(revision) is not int or revision <= 0:
        return {'status': 'refused', 'reason': 'invalid_revision'}
    try:
        first = ownership_proof(project_raw)
        with _state_locks(first['stateRoot']):
            proof = ownership_proof(project_raw)
            if proof['sha256'] != first['sha256']:
                _refuse('proof_changed')
            return cancel_locked(project_raw, revision, backup_dir, proof,
                                 queue=queue, clock=clock)
    except CancelRefused as exc:
        return {'status': 'refused', 'reason': str(exc)}
    except sqlite3.OperationalError as exc:
        # Busy or locked: nothing committed; inspect with ``build-wake-status``.
        return {'status': 'refused', 'reason': 'queue_busy', 'detail': type(exc).__name__}


def cancel_locked(project_raw, revision, backup_dir, proof, *, queue=None, clock=time.time):
    """The queue half of ``cancel``; the caller holds the install lock and the
    build owner (``_state_locks`` order) and passes the proof it took under
    them. It raises ``CancelRefused``/``sqlite3.OperationalError`` instead of
    answering, so a composing operation stops before its own first write.
    """
    from runtime_work import WakeQueue
    queue = queue or WakeQueue()
    key = wake_policy.wake_key(KIND, proof['project'])
    with queue.connect() as db:
        current = _row(db, key)
    if current is None:
        return {'status': 'unchanged', 'reason': 'wake_absent', 'proof': proof}
    if current['kind'] != KIND or current['target'] != proof['project']:
        _refuse('wake_identity_mismatch')
    if current['revision'] != revision:
        _refuse('revision_changed')
    if current['status'] != 'pending':
        _refuse('wake_not_pending')
    backup = _backup(queue, backup_dir, key, current)
    with queue.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(AUDIT_TABLE)
        removed = db.execute(
            "DELETE FROM wakes WHERE id=? AND kind=? AND target=? AND revision=? AND status='pending' "
            "AND due=? AND attempts=? AND reason IS ?",
            (key, KIND, proof['project'], revision, current['due'], current['attempts'],
             current['reason'])).rowcount
        if removed != 1:
            db.rollback()
            _refuse('wake_changed_before_delete')
        if ownership_proof(project_raw)['sha256'] != proof['sha256']:
            db.rollback()
            _refuse('proof_changed')
        overflow = db.execute('DELETE FROM wake_overflow WHERE id=?', (key,)).rowcount
        db.execute('INSERT INTO wake_cancellations (at,wake_id,kind,target,revision,attempts,reason,due,'
                   'overflow_removed,proof_sha256,backup_path,backup_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (clock(), key, KIND, proof['project'], revision, current['attempts'], current['reason'],
                    current['due'], overflow, proof['sha256'], backup['path'], backup['sha256']))
    after = status(proof['project'], queue=queue)
    return {'status': 'applied', 'revision': revision, 'overflowRemoved': overflow,
            'removedRow': {name: current[name] for name in ROW_VERSION},
            'backup': {k: backup[k] for k in ('path', 'sha256', 'quickCheck')},
            'proof': proof, 'after': after}
