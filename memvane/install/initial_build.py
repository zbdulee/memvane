"""Persist initial build identity, phase, ownership and project pointer."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from memvane.kernel.errors import IndexContractError
from memvane.journal.api import build as build_record

_canon = build_record.canonical
_private_dir = build_record.private_dir
_read_json = build_record.read_json
read_v1 = build_record.read_v1
from memvane.journal.types import MAX_RECORD_BYTES, V1_BUILD_SCHEMA, V1_BUILD_FILE, V1_BUILD_IDENTITY

SCHEMA = 'laya-initial-build-v1'
POINTER_SCHEMA = 'laya-initial-build-pointer-v1'
RECORD = 'initial-build.json'
LOCK = 'initial-build.lock'
POINTERS = 'initial-builds'
PHASES = ('registered', 'building', 'sealed', 'blocked', 'bound', 'retired',
          'upgrading')
IDENTITY_FIELDS = frozenset((
    'projectRoot', 'stateRoot', 'installId', 'scopeId', 'collections',
    'candidateDb', 'queueDb', 'catalogDb', 'bindingStore', 'dbIncarnation',
    'journalPath', 'routeFile', 'routeSha256', 'attestationSha256',
    'vectorProfileId', 'proposalSha256', 'providerProposalSha256',
    'originMode'))
RECORD_FIELDS = frozenset(('schema', 'epoch', 'phase', 'identity', 'progress',
                           'sealed', 'freshness', 'blocked', 'updatedNs'))
SEALED_FIELDS = frozenset(('generation', 'bindingDigest', 'sourceManifest',
                           'catalogScanId', 'catalogRawDigest', 'files', 'bytes',
                           'scanStartedNs', 'vectorProfileId', 'vectorRows',
                           'dbIncarnation'))
# Step answers that a later wake can clear without a source change.
_RETRYABLE = ('pending', 'deferred')
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')


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


def _validate_identity(identity):
    if (not isinstance(identity, dict) or set(identity) != IDENTITY_FIELDS or
            identity['originMode'] != 'fresh_wiki_only'):
        _fail('initial_build_identity_invalid')
    for key in IDENTITY_FIELDS - {'collections'}:
        if not isinstance(identity[key], str) or not identity[key]:
            _fail('initial_build_identity_invalid')
    for key in ('installId', 'scopeId', 'dbIncarnation'):
        if not _HEX32.fullmatch(identity[key]):
            _fail('initial_build_identity_invalid')
    for key in ('routeSha256', 'attestationSha256', 'vectorProfileId',
                'proposalSha256', 'providerProposalSha256'):
        if not _HEX64.fullmatch(identity[key]):
            _fail('initial_build_identity_invalid')
    for key in ('projectRoot', 'stateRoot', 'candidateDb', 'queueDb', 'catalogDb',
                'bindingStore', 'journalPath', 'routeFile'):
        if not Path(identity[key]).is_absolute():
            _fail('initial_build_identity_invalid')
    stores = [identity[key] for key in ('candidateDb', 'queueDb', 'catalogDb',
                                        'bindingStore')]
    if (len(set(stores)) != 4 or
            any(Path(path).parent != Path(identity['stateRoot']) for path in stores) or
            Path(identity['journalPath']).parent != Path(identity['stateRoot'])):
        _fail('initial_build_identity_invalid')
    collections = identity['collections']
    if (not isinstance(collections, list) or not 1 <= len(collections) <= 16 or
            any(not isinstance(item, dict) or set(item) != {'name', 'role', 'sourceRoot'}
                or not all(isinstance(item[key], str) and item[key] for key in item)
                for item in collections) or
            [item['name'] for item in collections] !=
            sorted({item['name'] for item in collections})):
        _fail('initial_build_identity_invalid')
    return identity


def _validate(record):
    if (not isinstance(record, dict) or set(record) != RECORD_FIELDS
            or record['schema'] != SCHEMA or type(record['epoch']) is not int
            or record['epoch'] < 1 or record['phase'] not in PHASES
            or type(record['updatedNs']) is not int):
        _fail('initial_build_record_invalid')
    _validate_identity(record['identity'])
    for key in ('progress', 'freshness', 'blocked'):
        if record[key] is not None and not isinstance(record[key], dict):
            _fail('initial_build_record_invalid')
    sealed = record['sealed']
    if sealed is not None and (not isinstance(sealed, dict) or set(sealed) != SEALED_FIELDS):
        _fail('initial_build_record_invalid')
    if (record['phase'] in ('sealed', 'bound')) != (sealed is not None):
        _fail('initial_build_record_invalid')
    if record['phase'] == 'blocked' and record['blocked'] is None:
        _fail('initial_build_record_invalid')
    freshness = record['freshness']
    if freshness is not None and (set(freshness) != {'requestedNs'} or
                                  type(freshness['requestedNs']) is not int):
        _fail('initial_build_record_invalid')
    return record


def record_path(state_root):
    return Path(state_root) / RECORD






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


def read(state_root):
    """The validated record, or None when no build was registered here."""
    record = _read_json(record_path(_private_dir(state_root)))
    return None if record is None else _validate(record)


def create_or_verify(identity):
    """Register a build once; an identical repeat returns the stored record.

    A different identity at the same state root is never overwritten: it
    fails as ``initial_build_identity_changed`` for explicit recovery.
    """
    _validate_identity(identity)
    state = _private_dir(identity['stateRoot'])
    existing = read(state)
    if existing is not None:
        if existing['identity'] != identity:
            _fail('initial_build_identity_changed')
        return existing
    record = {'schema': SCHEMA, 'epoch': 1, 'phase': 'registered',
              'identity': identity, 'progress': None, 'sealed': None,
              'freshness': None, 'blocked': None, 'updatedNs': time.time_ns()}
    try:
        _write_json(record_path(state), record, replace=False)
    except IndexContractError as exc:
        if str(exc) != 'initial_build_record_exists':
            raise
    stored = read(state)
    if stored is None or stored['identity'] != identity:
        _fail('initial_build_identity_changed')
    return stored


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
    if isinstance(identity, dict) and set(identity) == V1_BUILD_IDENTITY:
        record = read_v1(identity['stateRoot'])
        if record is None or record['identity'] != identity:
            _fail('initial_build_identity_invalid')
    else:
        _validate_identity(identity)
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

    The caller holds the build ``owner``. The record is either ``bound`` with
    its selection binding retired, or terminal ``retired`` because no work started;
    in both cases a wake cannot step it. The exact pointer bytes are first
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
    if isinstance(identity, dict) and set(identity) == V1_BUILD_IDENTITY:
        record = read_v1(identity['stateRoot'])
        if record is None or record['identity'] != identity:
            _fail('initial_build_identity_invalid')
    else:
        _validate_identity(identity)
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
    if record is None:
        record = read(pointer['stateRoot'])
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


def advance(record, **changes):
    """Compare-and-set one change; the caller holds ``owner``.

    The stored record must still be the one the caller read (same epoch).
    The identity never changes, and ``bound`` is terminal here.
    """
    if not set(changes) <= RECORD_FIELDS - {'schema', 'epoch', 'identity', 'updatedNs'}:
        _fail('initial_build_change_invalid')
    state = record['identity']['stateRoot']
    current = read(state)
    if current is None or current != record:
        _fail('initial_build_stale_cas')
    if current['phase'] == 'bound' and changes.get('phase', 'bound') != 'bound':
        _fail('initial_build_already_bound')
    if current['phase'] == 'retired':
        _fail('initial_build_retired')
    updated = {**current, **changes, 'epoch': current['epoch'] + 1,
               'updatedNs': time.time_ns()}
    _validate(updated)
    _write_json(record_path(state), updated, replace=True)
    return updated






def _progress(result):
    keep = ('state', 'reason', 'path', 'required', 'detail', 'claims_pending',
            'lastAttemptNs', 'lastAttemptClass', 'lastAttemptReason')
    out = {key: result[key] for key in keep if key in result}
    catalog = result.get('catalog')
    if isinstance(catalog, dict):
        active = catalog.get('active')
        if isinstance(active, dict):
            out['scan'] = {'phase': active.get('phase'),
                           'backlog': active.get('backlog')}
    if isinstance(result.get('fill'), dict):
        out['fill'] = result['fill']
    return out


def transition(record, result, sealed=None):
    """Pure: the phase and fields one step answer moves ``record`` to.

    ``sealed`` contains the proof when the head serves a servable snapshot;
    only then can the build be ``sealed``. A ``blocked`` answer is kept with
    its reason and path; ``pending`` and ``deferred`` are ``building``.
    Returns the keyword changes for ``advance`` (empty when nothing changed).
    """
    if record['phase'] in ('bound', 'retired'):
        return {}
    state = result.get('state')
    if sealed is not None:
        if set(sealed) != SEALED_FIELDS:
            _fail('initial_build_evidence_invalid')
        changes = {'phase': 'sealed', 'sealed': sealed, 'blocked': None,
                   'progress': _progress(result)}
    elif state == 'blocked':
        changes = {'phase': 'blocked', 'sealed': None,
                   'blocked': {key: result[key] for key in ('reason', 'detail', 'path')
                               if key in result},
                   'progress': _progress(result)}
    elif state in _RETRYABLE or state in ('indexed', 'unchanged'):
        # indexed/unchanged without evidence: a newer scan is still pending.
        changes = {'phase': 'building', 'sealed': None, 'blocked': None,
                   'progress': _progress(result)}
    else:
        _fail('initial_build_result_invalid')
    return {key: value for key, value in changes.items() if record.get(key) != value}


def request_freshness(record, catalog):
    """Ask for a scan that starts now; the caller holds ``owner``.

    Returns the advanced record. A busy catalog records nothing.
    """
    if record['phase'] == 'bound':
        _fail('initial_build_already_bound')
    if record['phase'] == 'retired':
        _fail('initial_build_retired')
    requested = time.time_ns()
    if catalog.request_scan().get('status') == 'busy':
        _fail('catalog_busy')
    phase = 'building' if record['phase'] in ('sealed', 'registered') else record['phase']
    return advance(record, freshness={'requestedNs': requested}, phase=phase,
                   sealed=None if phase == 'building' else record['sealed'])


def freshness_satisfied(record):
    """True only for a sealed snapshot scanned entirely after the request."""
    freshness, sealed = record['freshness'], record['sealed']
    return (record['phase'] == 'sealed' and freshness is not None and sealed is not None
            and sealed['scanStartedNs'] >= freshness['requestedNs'])
