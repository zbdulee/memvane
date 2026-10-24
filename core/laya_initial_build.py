"""Durable record of one inactive initial Laya Index build (installer-owned).

An install whose corpus one synchronous call cannot cover registers a build
instead of embedding in-process. The build's data lives in the stores the
capacity path already makes durable: the private candidate A-int DB, its
claim queue, the source catalog, the binding store and the vector chunk
cache. This module owns only the small record that names those stores, the
per-user pointer the runtime wake uses to find it from a project path, and
the phase the installer's finalize gate reads.

Phases (``PHASES``):

* ``registered``: install created every store, the journal is
  ``candidate_ready`` and the project still selects QMD. No source byte has
  been read and nothing is staged.
* ``building``: a bounded runtime step ran; the candidate head does not yet
  serve a complete, servable catalog snapshot with a vector seal (scan, fill,
  stage, a newer scan or a deferral is pending). ``progress`` names why.
* ``sealed``: the head of the private, unselected candidate serves the
  catalog's current snapshot under a stored binding and a vector seal for the
  reviewed profile, and no newer scan is pending. ``sealed`` is evidence of
  *that* snapshot only, never of the live tree.
* ``blocked``: a step reported a block that retrying cannot clear (invalid
  source, vector budget, a member the provider refuses). It is re-evaluated by
  every wake and cleared by an explicit rescan.
* ``bound``: finalize bound the sealed candidate to the QMD origin (a v3
  ``laya_qmd_cutover`` ledger and project marker). The wake does nothing
  more; activation is a separate explicit installer step. Between the
  marker and this phase (an interrupted finalize) the wake defers without
  stepping, and a repeated finalize records ``bound``. If QMD changes
  before activation the binding is retired and the record stays ``bound``
  for good; ``retire_project_pointer`` releases the project pointer so a
  fresh install (its own state root and record) can register.
* ``retired``: an explicit, audited abandon of a build that never did any
  work (still ``registered``, nothing read or written; see
  ``laya_product_install.abandon_unused_initial_build``). Terminal and
  written before the project pointer is released. A worker that knows it
  ACKs; a worker that predates it fails closed on the unknown phase
  (``initial_build_record_invalid``), so it can never step or publish.

Freshness is separate from ``sealed``: ``request_freshness`` records a time T
and requests a catalog scan. ``freshness_satisfied`` is true only when the
sealed snapshot came from a scan *started* at or after T. Anything else is
"freshness unknown", and finalize refuses it.

Nothing here selects a route, writes a project file, deletes a store or
loads a model. A partial catalog never publishes or deletes: only
``sync_step`` publishes, and only from a COMPLETE snapshot.
"""
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

from laya_index_a import IndexContractError

SCHEMA = 'laya-initial-build-v1'
POINTER_SCHEMA = 'laya-initial-build-pointer-v1'
RECORD = 'initial-build.json'
LOCK = 'initial-build.lock'
POINTERS = 'initial-builds'
PHASES = ('registered', 'building', 'sealed', 'blocked', 'bound', 'retired')
MAX_RECORD_BYTES = 65536
IDENTITY_FIELDS = frozenset((
    'projectRoot', 'stateRoot', 'installId', 'scopeId', 'collections',
    'candidateDb', 'queueDb', 'catalogDb', 'bindingStore', 'dbIncarnation',
    'journalPath', 'routeFile', 'routeSha256', 'attestationSha256',
    'vectorProfileId', 'sourceDb', 'sourceFormat', 'proposalSha256',
    'providerProposalSha256'))
REPLACEMENT_IDENTITY_FIELDS = IDENTITY_FIELDS | {'replacementEvidenceSha256'}
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


def _canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_dir(path):
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        _fail('initial_build_parent_unavailable')
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or not path.is_absolute()):
        _fail('initial_build_parent_unsafe')
    return path


def _read_json(path):
    """One owned, private, single-link canonical JSON object; None if absent."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    except OSError:
        _fail('initial_build_record_unavailable')
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size > MAX_RECORD_BYTES):
            _fail('initial_build_record_unsafe')
        raw = os.read(fd, MAX_RECORD_BYTES + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (len(raw) != before.st_size or
            (before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_ino, after.st_size, after.st_mtime_ns)):
        _fail('initial_build_record_changed')
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        _fail('initial_build_record_invalid')
    if not isinstance(value, dict) or _canon(value) != raw:
        _fail('initial_build_record_invalid')
    return value


def _write_json(path, value, *, replace):
    """Fsynced temp file, then a no-clobber link or an atomic replace."""
    path = Path(path)
    raw = _canon(value)
    if len(raw) > MAX_RECORD_BYTES:
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
    if not isinstance(identity, dict) or set(identity) not in (
            IDENTITY_FIELDS, REPLACEMENT_IDENTITY_FIELDS):
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
    if ('replacementEvidenceSha256' in identity and
            not _HEX64.fullmatch(identity['replacementEvidenceSha256'])):
        _fail('initial_build_identity_invalid')
    for key in ('projectRoot', 'stateRoot', 'candidateDb', 'queueDb', 'catalogDb',
                'bindingStore', 'journalPath', 'routeFile', 'sourceDb'):
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
    """``~/.local/state/laya/initial-builds``, created 0700 only if asked."""
    cursor = Path.home()
    for name in ('.local', 'state', 'laya', POINTERS):
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


def pointer_path(project_root, directory):
    digest = hashlib.sha256(str(project_root).encode('utf-8')).hexdigest()[:32]
    return Path(directory) / (digest + '.json')


def register_project(identity):
    """Point the project's runtime wake at this build; never at a second one."""
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
    its QMD binding retired, or terminal ``retired`` because no work started;
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
    directory = _pointer_dir(create=False)
    if directory is None:
        return 'absent'
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
    directory = _pointer_dir(create=False)
    if directory is None:
        return None
    pointer = _read_json(pointer_path(project_root, directory))
    if pointer is None:
        return None
    if (set(pointer) != {'schema', 'projectRoot', 'stateRoot', 'installId'}
            or pointer['schema'] != POINTER_SCHEMA
            or pointer['projectRoot'] != str(project_root)):
        _fail('initial_build_pointer_invalid')
    record = read(pointer['stateRoot'])
    if (record is None or record['identity']['projectRoot'] != str(project_root)
            or record['identity']['stateRoot'] != pointer['stateRoot']
            or record['identity']['installId'] != pointer['installId']):
        _fail('initial_build_pointer_invalid')
    return record


@contextmanager
def owner(state_root):
    """One build owner at a time; yields False (never waits) when held."""
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
    keep = ('state', 'reason', 'path', 'required', 'detail', 'claims_pending')
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

    ``sealed`` is ``evidence(...)`` when the head serves a servable snapshot;
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


def evidence(runtime, generation, profile_id):
    """Sealed-candidate facts, or None while the head does not serve them.

    Every fact is re-read from the stores: the head generation, its source
    manifest, the stored binding (whose name commits to it), the catalog's
    current snapshot and its scan start time, and the vector seal. The head
    binding must serve the same bytes from the same live root nodes as a
    fresh capture of the current snapshot (``same_bound_source``); like
    ``sync_step``, an unchanged rescan does not need a new generation, so its
    scan id may be older than the snapshot's. A pending newer scan, a
    replaced root or a missing binding answers None (not sealed).
    """
    from laya_index_capacity import (binding_digest, capture_bound_generation_binding,
                                     generation_digest, same_bound_source)
    from laya_index_sync import _head
    from laya_index_vector import verify_generation_seal
    catalog, store, index = runtime.catalog, runtime.binding_store, runtime.index
    if catalog is None or store is None:
        _fail('capacity_not_configured')
    if not isinstance(generation, str) or not catalog.status()['servable']:
        return None
    snapshot = catalog.require_servable()
    head, epoch = _head(runtime)
    if head is None or head[0] != generation or head[2] != epoch:
        return None
    digest = generation_digest(generation)
    binding = store.get(runtime.scope, generation, store.db_incarnation)
    if (digest is None or binding is None or binding_digest(binding) != digest
            or binding.source_manifest != head[1]):
        return None
    try:
        captured = capture_bound_generation_binding(index, catalog, snapshot)
    except IndexContractError:
        return None
    if not same_bound_source(binding, captured):
        return None
    index.db.execute('BEGIN')
    try:
        seal = verify_generation_seal(index, generation)
        index.db.rollback()
    except BaseException:
        index.db.rollback()
        raise
    if seal['profile_id'] != profile_id:
        return None
    return {'generation': generation, 'bindingDigest': digest,
            'sourceManifest': head[1], 'catalogScanId': snapshot.scan_id,
            'catalogRawDigest': snapshot.raw_digest, 'files': snapshot.files,
            'bytes': snapshot.bytes,
            'scanStartedNs': catalog.snapshot_started_ns(snapshot),
            'vectorProfileId': profile_id, 'vectorRows': seal['rows_count'],
            'dbIncarnation': binding.db_incarnation}


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
