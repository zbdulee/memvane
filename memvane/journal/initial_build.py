"""Persist initial build identity, phase, ownership and project pointer."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time

from memvane.kernel.errors import IndexContractError
from memvane.journal import build_record

_canon = build_record._canon
_private_dir = build_record._private_dir
_read_json = build_record._read_json
read_v1 = build_record.read_v1
from memvane.journal.types import MAX_RECORD_BYTES, V1_BUILD_SCHEMA, V1_BUILD_FILE, V1_BUILD_IDENTITY

POINTER_SCHEMA = 'memvane-initial-build-pointer-v1'
LOCK = 'initial-build.lock'
POINTERS = 'initial-builds'


def _fail(reason):
    raise IndexContractError(reason)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path, value, *, replace, max_bytes=MAX_RECORD_BYTES):
    """Fsynced temp file, then a no-clobber link or an atomic replace."""
    path = Path(path)
    raw = _canon(value)
    if len(raw) > max_bytes:
        _fail('initial_build_record_too_large')
    fd, name = tempfile.mkstemp(prefix='.laya-build-', dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            if path.is_symlink():
                _fail('initial_build_record_unsafe')
            os.replace(temp, path)
        else:
            try:
                os.link(temp, path)
            except FileExistsError:
                _fail('initial_build_record_exists')
        _fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def create_v1(identity):
    """Register a proved v1 build; the caller holds the existing build owner."""
    from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
    from memvane.journal.install_journal import _route, inspect_journal
    if not isinstance(identity, dict) or set(identity) != V1_BUILD_IDENTITY:
        _fail('initial_build_identity_invalid')
    state = _private_dir(identity['stateRoot'])
    require_cached_plugin_for_live_write(state)
    if identity['journalPath'] != str(state / 'journal.sqlite'):
        _fail('initial_build_identity_invalid')
    existing = read_v1(state)
    if existing is not None:
        if existing['identity'] != identity:
            _fail('initial_build_identity_changed')
        return existing
    journal = inspect_journal(state / 'journal.sqlite')
    route_record = _read_json(state / 'route.json')
    if route_record is None:
        _fail('initial_build_route_missing')
    route, digest = _route(_canon(route_record), state / 'journal.sqlite')
    if (journal['state'] != 'prepared' or digest != identity['routeSha256']
            or journal['routeSha256'] != digest
            or journal['attestationSha256'] != identity['attestationSha256']
            or any(journal[k] != identity[k] for k in
                   ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation'))
            or journal['storeInode'] != identity['storeNode'][1]
            or any(route[k] != identity[k] for k in
                   ('projectRoot', 'stateRoot', 'installId', 'scopeId', 'storePath',
                    'storeNode', 'storeIncarnation'))):
        _fail('initial_build_identity_changed')
    record = {'schema': V1_BUILD_SCHEMA, 'epoch': 1, 'phase': 'registered',
              'identity': identity, 'reason': None, 'updatedNs': time.time_ns()}
    _write_json(state / V1_BUILD_FILE, record, replace=False)
    return read_v1(state)


def advance_v1(record, *, phase, reason=None):
    """CAS the build phase while its existing owner lock is held."""
    if phase not in ('registered', 'building', 'ready'):
        _fail('initial_build_change_invalid')
    if reason is not None and (type(reason) is not str or len(reason) > 256):
        _fail('initial_build_change_invalid')
    state = record['identity']['stateRoot']
    if read_v1(state) != record:
        _fail('initial_build_stale_cas')
    updated = {**record, 'phase': phase, 'reason': reason,
               'epoch': record['epoch'] + 1, 'updatedNs': time.time_ns()}
    _write_json(Path(state) / V1_BUILD_FILE, updated, replace=True)
    return read_v1(state)


def _pointer_dir(create):
    """Return the private initial-build pointer directory."""
    from memvane.kernel import memvane_paths
    cursor = Path.home()
    parts = memvane_paths.STATE_PARTS
    for name in (*parts, POINTERS):
        cursor = cursor / name
        if not cursor.exists() and not cursor.is_symlink():
            if not create:
                return None
            os.mkdir(cursor, 0o700)
        info = cursor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            _fail('initial_build_pointer_unsafe')
    return _private_dir(cursor)


def _pointer_dirs():
    """Return the current pointer directory when it exists."""
    directory = _pointer_dir(create=False)
    return [directory] if directory is not None else []


def registered_pointer_path(project_root):
    """Path of the project's existing pointer, or None."""
    for directory in _pointer_dirs():
        path = pointer_path(project_root, directory)
        if path.exists() or path.is_symlink():
            return path
    return None


def pointer_path(project_root, directory):
    digest = hashlib.sha256(str(project_root).encode('utf-8')).hexdigest()[:32]
    return Path(directory) / (digest + '.json')


def register_project(identity):
    """Point the project's runtime wake at this build; never at a second one."""
    if not isinstance(identity, dict) or set(identity) != V1_BUILD_IDENTITY:
        _fail('initial_build_identity_invalid')
    record = read_v1(identity['stateRoot'])
    if record is None or record['identity'] != identity:
        _fail('initial_build_identity_invalid')
    pointer = {'schema': POINTER_SCHEMA, 'projectRoot': identity['projectRoot'],
               'stateRoot': identity['stateRoot'], 'installId': identity['installId']}
    path = pointer_path(identity['projectRoot'], _pointer_dir(create=True))
    existing = _read_json(path)
    if existing is None:
        try:
            _write_json(path, pointer, replace=False)
        except IndexContractError as exc:
            if str(exc) != 'initial_build_record_exists':
                raise
        existing = _read_json(path)
    if existing != pointer:
        _fail('initial_build_already_registered')
    return str(path)


RETIRED_POINTER = 'retired-build-pointer.json'


def retire_project_pointer(identity):
    """Release the project's wake pointer of a retired build; deletes no store.

    The caller holds the build ``owner`` and is undoing the build (the
    installer's restore), so no wake steps it. The exact pointer bytes are first
    archived, no-clobber, as
    ``RETIRED_POINTER`` in the build's state root. Then only a pointer that
    is exactly this build's is moved aside within the pointer directory and
    removed; a pointer a later install registered is never touched (it is
    re-linked if it raced in). The holding name carries this install id, so
    an interrupted retirement of one install never blocks another's; only a
    held file equal to the archived pointer is ever removed. Idempotent,
    including after an interruption between the move and the removal.
    Returns ``retired``, ``absent`` or ``other``.
    """
    if not isinstance(identity, dict) or set(identity) != V1_BUILD_IDENTITY:
        _fail('initial_build_identity_invalid')
    record = read_v1(identity['stateRoot'])
    if record is None or record['identity'] != identity:
        _fail('initial_build_identity_invalid')
    pointer = {'schema': POINTER_SCHEMA, 'projectRoot': identity['projectRoot'],
               'stateRoot': identity['stateRoot'], 'installId': identity['installId']}
    archive = Path(_private_dir(identity['stateRoot'])) / RETIRED_POINTER
    stored = _read_json(archive)
    if stored is None:
        try:
            _write_json(archive, pointer, replace=False)
        except IndexContractError as exc:
            if str(exc) != 'initial_build_record_exists':
                raise
        stored = _read_json(archive)
    if stored != pointer:
        _fail('initial_build_pointer_archive_mismatch')
    results = [_retire_pointer_in(directory, identity, pointer, stored)
               for directory in _pointer_dirs()]
    for answer in ('retired', 'other'):
        if answer in results:
            return answer
    return 'absent'


def _retire_pointer_in(directory, identity, pointer, stored):
    path = pointer_path(identity['projectRoot'], directory)
    holding = path.with_name('.retiring-' + identity['installId'] + '-' + path.name)
    held = _read_json(holding)
    if held is not None and held != stored:
        # Only this install moves a file here, and only its own pointer.
        _fail('initial_build_pointer_changed')
    current = _read_json(path)
    if current is not None and current != pointer:
        if held is not None:
            os.unlink(holding)
            _fsync_dir(directory)
        return 'other'
    if current is not None:
        if held is not None:
            os.unlink(holding)
        os.rename(path, holding)
        _fsync_dir(directory)
        if _read_json(holding) != pointer:
            # Replaced between the read and the move: restore, never delete.
            try:
                os.link(holding, path)
            except FileExistsError:
                _fail('initial_build_pointer_changed')
            os.unlink(holding)
            _fsync_dir(directory)
            _fail('initial_build_pointer_changed')
        held = pointer
    if held is not None:
        os.unlink(holding)
        _fsync_dir(directory)
        return 'retired'
    return 'absent'


def lookup(project_root):
    """The record of the build registered for this project, or None.

    A pointer that does not name a matching record fails closed rather than
    letting the wake fall through to another path.
    """
    pointer = None
    for directory in _pointer_dirs():
        pointer = _read_json(pointer_path(project_root, directory))
        if pointer is not None:
            break
    if pointer is None:
        return None
    if (set(pointer) != {'schema', 'projectRoot', 'stateRoot', 'installId'}
            or pointer['schema'] != POINTER_SCHEMA
            or pointer['projectRoot'] != str(project_root)):
        _fail('initial_build_pointer_invalid')
    record = read_v1(pointer['stateRoot'])
    if (record is None or record['identity']['projectRoot'] != str(project_root)
            or record['identity']['stateRoot'] != pointer['stateRoot']
            or record['identity']['installId'] != pointer['installId']):
        _fail('initial_build_pointer_invalid')
    return record


@contextmanager
def owner(state_root):
    """One build owner at a time; yields False (never waits) when held."""
    from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
    require_cached_plugin_for_live_write(state_root)
    state = _private_dir(state_root)
    fd = os.open(state / LOCK, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_CLOEXEC', 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('initial_build_lock_unsafe')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)
