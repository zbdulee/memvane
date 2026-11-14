"""Installer-owned synthetic activation journal and read-only route resolver.

This module writes only an explicit, private journal path. It never publishes a
project route, touches QMD, deletes an engine, or authorizes destructive GC.
Route-file publication is a separate installer step. CAS plus recovery records
detect interruption, but are not a cross-file power-loss transaction.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import threading
import time
import unicodedata

from memvane.kernel.bundle_paths import (PROVIDER_RELATIVE_PATH, WORKER_RELATIVE_PATH, bundle_file,
                                         is_provider_relative_path)
from memvane.kernel.errors import IndexContractError
from memvane.journal.types import JournalEvidenceError
from memvane.journal.api import select_route


V1_SCHEMA = 'memvane-install-journal-v1'
V1_APP_ID = 0x4d564a31
V1_SELECTION_DDL = '''CREATE TABLE selection(
  id INTEGER PRIMARY KEY CHECK(id=1), schema TEXT NOT NULL,
  epoch INTEGER NOT NULL, state TEXT NOT NULL,
  install_id TEXT NOT NULL, scope_id TEXT NOT NULL,
  project_root TEXT NOT NULL, project_ino INTEGER NOT NULL,
  state_format INTEGER NOT NULL, writer_floor TEXT NOT NULL,
  max_plugin_version TEXT NOT NULL,
  store_path TEXT NOT NULL, store_ino INTEGER NOT NULL,
  store_incarnation TEXT NOT NULL, route_sha TEXT NOT NULL,
  attestation_sha TEXT NOT NULL, vector_profile TEXT NOT NULL,
  vector_recipe TEXT, new_write_seen INTEGER NOT NULL)'''
V1_READER_DDL = '''CREATE TABLE reader(
  id INTEGER PRIMARY KEY CHECK(id=1), record TEXT NOT NULL)'''
ID = re.compile(r'[0-9a-f]{32}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
PRODUCTION_ATTESTATION = 'provider-attestation.json'
RUNTIME_FILES_FIELD = 'runtimeFilesSha256'


def _fail(reason):
    raise JournalEvidenceError(reason)


def _canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'))


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail('duplicate_journal_route_key')
        result[key] = value
    return result


def _nonfinite(_value):
    _fail('nonfinite_journal_route_number')


def _attestation(path):
    """Read installer-owned bytes; a declaration inside the plugin is not proof."""
    target = Path(path).parent / PRODUCTION_ATTESTATION
    if not target.exists() and not target.is_symlink():
        return None, None
    try:
        if target.is_symlink():
            _fail('unsafe_production_attestation')
        fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                    or before.st_size > 1024 * 1024):
                _fail('unsafe_production_attestation')
            raw = os.read(fd, 1024 * 1024 + 1)
            after = os.fstat(fd)
            if (len(raw) != before.st_size or
                    (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                    != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)):
                _fail('production_attestation_changed')
        finally:
            os.close(fd)
        parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                            parse_constant=_nonfinite)
        if not isinstance(parsed, dict) or raw != _canon(parsed).encode('utf-8'):
            _fail('production_attestation_noncanonical')
        if parsed.get('journalPath') != str(Path(path)):
            _fail('production_attestation_journal_mismatch')
        return parsed, hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalEvidenceError('production_attestation_unavailable') from exc




def stage_v1_attestation(journal_path, reviewed_provider, *, provider=None):
    """Stage a reviewed provider under the install lock without bundle paths.

    ``provider`` is a fixture provider port (``provider.api.fixture_port``).
    """
    from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
    from memvane.provider.api import sealed_files_sha256
    from memvane.provider.types import RuntimeUnavailable
    journal = _private_parent(journal_path)
    require_cached_plugin_for_live_write(journal.parent)
    if (not isinstance(reviewed_provider, dict)
            or set(reviewed_provider) != {'vectorProfile', 'providerManifest', 'dependencyClosure'}
            or not isinstance(reviewed_provider['providerManifest'], dict)):
        _fail('production_attestation_invalid')
    profile = reviewed_provider['vectorProfile']
    _strict_vector_profile(profile)
    manifest = dict(reviewed_provider['providerManifest'])
    manifest.pop('providerPath', None)
    manifest.pop('workerPath', None)
    try:
        runtime_sha = sealed_files_sha256(manifest.get('moduleEntryPath'), port=provider)
    except (RuntimeUnavailable, OSError) as exc:
        raise JournalEvidenceError('production_attestation_invalid') from exc
    if type(runtime_sha) is not str or not SHA.fullmatch(runtime_sha):
        _fail('production_attestation_invalid')
    record = {'schema': 'memvane-provider-attestation-v1', 'journalPath': str(journal),
              'vectorProfile': profile, 'providerManifest': manifest,
              'dependencyClosure': reviewed_provider['dependencyClosure'],
              RUNTIME_FILES_FIELD: runtime_sha}
    _v1_provider_manifest(record, profile, provider=provider)
    raw = _canon(record).encode('utf-8')
    if len(raw) > 1024 * 1024:
        _fail('production_attestation_too_large')
    target = journal.parent / PRODUCTION_ATTESTATION
    if target.exists() or target.is_symlink():
        existing, digest = _attestation(journal)
        if existing != record:
            _fail('production_attestation_already_owned')
        return {'attestationPath': str(target), 'attestationSha256': digest}
    from memvane.kernel.atomic_rename import write_new_file
    try:
        # Complete bytes beside it, then a no-replace rename: an interrupted
        # stage leaves no attestation and its rerun writes it again.
        write_new_file(target, raw)
    except FileExistsError:
        pass  # compared below
    except OSError as exc:
        raise JournalEvidenceError('production_attestation_stage_incomplete') from exc
    existing, digest = _attestation(journal)
    if existing != record:
        _fail('production_attestation_changed')
    return {'attestationPath': str(target), 'attestationSha256': digest}




def _strict_vector_profile(value):
    fields = {'profileId', 'modelId', 'embeddingFingerprint', 'dimension', 'metric',
              'approvedProviderId', 'providerArtifactSha256', 'providerRelativePath'}
    if not isinstance(value, dict) or set(value) != fields:
        _fail('invalid_vector_profile')
    model, fingerprint, dimension, metric = (value['modelId'], value['embeddingFingerprint'],
                                             value['dimension'], value['metric'])
    relative = value['providerRelativePath']
    if (type(model) is not str or not 1 <= len(model) <= 512
            or any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp')
                   for char in model)
            or type(fingerprint) is not str or not SHA.fullmatch(fingerprint)
            or type(dimension) is not int or not 1 <= dimension <= 2048 or metric != 'cosine'
            or type(value['approvedProviderId']) is not str
            or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,127}', value['approvedProviderId'])
            or type(value['providerArtifactSha256']) is not str
            or not SHA.fullmatch(value['providerArtifactSha256'])
            or not is_provider_relative_path(relative)
            or not relative.endswith('.py') or Path(relative).is_absolute() or '\\' in relative
            or any(part in ('', '.', '..') for part in relative.split('/'))):
        _fail('invalid_vector_profile')
    expected = hashlib.sha256(_canon([model, fingerprint, dimension, metric]).encode('utf-8')).hexdigest()
    if value['profileId'] != expected:
        _fail('invalid_vector_profile')
    return dict(value)


def _route(raw, journal_path):
    if type(raw) is not bytes or len(raw) > 8192:
        _fail('invalid_route_bytes')
    try:
        route = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=_nonfinite)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalEvidenceError('invalid_route_bytes') from exc
    from memvane.route.types import FIELDS_V4, SCHEMA_V4
    state = Path(journal_path).parent
    if (not isinstance(route, dict) or route.get('schema') != SCHEMA_V4
            or set(route) != FIELDS_V4 or route.get('status') != 'active'
            or route.get('stateRoot') != str(state)
            or route.get('storePath') != str(state / 'store.sqlite')
            or type(route.get('stateFormat')) is not int or route['stateFormat'] < 1
            or any(type(route.get(key)) is not str or not ID.fullmatch(route[key])
                   for key in ('installId', 'scopeId', 'storeIncarnation'))
            or type(route.get('settingsSha256')) is not str
            or not SHA.fullmatch(route['settingsSha256'])
            or not isinstance(route.get('projectRoot'), str)
            or not Path(route['projectRoot']).is_absolute()
            or not isinstance(route.get('collections'), list)
            or any(not isinstance(route.get(key), list) or len(route[key]) != 2
                   or any(type(n) is not int or n < 0 for n in route[key])
                   for key in ('stateNode', 'storeNode'))):
        _fail('route_contract_mismatch')
    try:
        from memvane.route.api import settings_identity_digest, wiki_index_roots
        wiki_index_roots(route['collections'])
        if (len({item['name'] for item in route['collections']}) != 2
                or next(item['sourceRoot'] for item in route['collections']
                        if item['role'] == 'raw') != route['projectRoot']
                or settings_identity_digest(route['projectRoot'], route['collections'])
                != route['settingsSha256']):
            _fail('route_contract_mismatch')
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise JournalEvidenceError('route_contract_mismatch') from exc
    _strict_vector_profile(route.get('vectorProfile'))
    return route, hashlib.sha256(raw).hexdigest()



def _private_parent(path):
    try:
        path = Path(path)
        if not path.is_absolute() or path.is_symlink() or path.name != 'journal.sqlite':
            _fail('invalid_journal_path')
        parent = path.parent
        if parent.is_symlink() or not parent.is_dir():
            _fail('unsafe_journal_parent')
        parent = parent.resolve(strict=True)
        info = parent.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            _fail('unsafe_journal_parent')
        return parent / path.name
    except (OSError, TypeError, RuntimeError) as exc:
        raise JournalEvidenceError('journal_path_unavailable') from exc






















def _schema_version(db):
    """The journal format of an open connection, or None when unknown.

    ``user_version`` and the table SQL are read in one read transaction (the
    caller's, or one opened here): a v3->v4 conversion committing between
    the two reads would otherwise pair v3 with v4 SQL (``journal_schema_unknown``).
    """
    if db.in_transaction:
        return _schema_version_read(db)
    db.execute('BEGIN')
    try:
        version = _schema_version_read(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return version


def _schema_version_read(db):
    app_id = db.execute('PRAGMA application_id').fetchone()[0]
    if app_id != V1_APP_ID or db.execute('PRAGMA user_version').fetchone()[0] != 1:
        return None
    objects = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"))
    return 1 if objects == {'selection': V1_SELECTION_DDL, 'reader': V1_READER_DDL} else None


_reader_no_recovery = ContextVar('journal_reader_no_recovery', default=False)
_reader_deadline = ContextVar('journal_reader_deadline', default=None)


@contextmanager
def reader_deadline(deadline):
    """Bound every journal reader's busy wait by a ``time.monotonic`` deadline.

    A caller with its own small budget (the registry reconcile of a hook)
    keeps a locked journal from holding it for the reader's 2 second wait.
    Past the deadline a read fails at once with ``journal_unavailable``.
    """
    token = _reader_deadline.set(deadline)
    try:
        yield
    finally:
        _reader_deadline.reset(token)


def _reader_timeout():
    deadline = _reader_deadline.get()
    if deadline is None:
        return 2
    left = deadline - time.monotonic()
    if left <= 0:
        raise JournalEvidenceError('journal_unavailable')
    return min(2, left)


@contextmanager
def read_only_journals():
    """Prevent writes while a caller performs read-only journal checks."""
    token = _reader_no_recovery.set(True)
    try:
        yield
    finally:
        _reader_no_recovery.reset(token)




def _check_rollback_file(path):
    """Check rollback file ownership and identity before SQLite opens it."""
    side = Path(str(path)+'-journal')
    for attempt in range(2):
        try:
            fd = os.open(side, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise JournalEvidenceError('journal_unavailable') from exc
        try:
            info = os.fstat(fd)
            if info.st_nlink == 0:
                raise FileNotFoundError('journal_unlinked')
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
                _fail('unsafe_journal_file')
            after = os.fstat(fd)
            if after.st_nlink == 0:
                raise FileNotFoundError('journal_unlinked')
            named = side.lstat()
            for current in (after, named):
                if current.st_nlink == 0:
                    raise FileNotFoundError('journal_unlinked')
                if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
                        or current.st_nlink != 1 or stat.S_IMODE(current.st_mode) & 0o077):
                    _fail('unsafe_journal_file')
            if (not os.path.samestat(info, named) or
                    (info.st_size, info.st_mtime_ns, info.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns) !=
                    (named.st_size, named.st_mtime_ns, named.st_ctime_ns)):
                _fail('journal_changed')
            return
        except FileNotFoundError:
            # SQLite may remove its rollback journal between open and inspection.
            # Recheck the name once; sustained churn is a typed proof failure.
            if attempt:
                _fail('journal_changed')
        except OSError as exc:
            raise JournalEvidenceError('journal_unavailable') from exc
        finally:
            os.close(fd)


def _open(path, *, write):
    if write and _reader_no_recovery.get():
        _fail('reader_journal_read_only')
    path = _private_parent(path)
    _check_rollback_file(path)
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('unsafe_journal_file')
        uri = path.as_uri() + ('?mode=rw' if write else '?mode=ro')
        db = sqlite3.connect(uri, uri=True, timeout=2 if write else _reader_timeout(),
                             isolation_level=None)
        try:
            if not write:
                db.execute('PRAGMA query_only=ON')
            version = _schema_version(db)
        except sqlite3.OperationalError:
            db.close()
            raise
        if version is None:
            db.close()
            _fail('journal_schema_unknown')
        return path, db
    except (OSError, sqlite3.Error) as exc:
        raise JournalEvidenceError('journal_unavailable') from exc


def journal_version(journal_path):
    """Read the supported journal format and refuse an unknown file."""
    path, db = _open(journal_path, write=False)
    with closing(db):
        db.execute('BEGIN')
        try:
            version = _schema_version(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    if version is None:
        _fail('journal_schema_unknown')
    return version






def create_v1_journal(journal_path, route_raw, expected_attestation_sha256, *, provider=None):
    """Create an inactive journal under the installer's lock after review approval."""
    from memvane.settings import api as project_dir
    from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
    from memvane.kernel.memvane_version import plugin_version, same_major
    from memvane.route.api import classify_v4 as _classify_v4
    path = _private_parent(journal_path)
    require_cached_plugin_for_live_write(path.parent)
    if path.exists() or path.is_symlink():
        _fail('journal_already_exists')
    route, route_sha = _route(route_raw, path)
    if route.get('schema') != 'memvane-route-v4' or not same_major(route['stateFormat']):
        _fail('state_format_mismatch')
    project = Path(route['projectRoot'])
    settings = project_dir.settings_path(project)
    if settings is None or _classify_v4(project, settings, route).get('state') != 'candidate':
        _fail('journal_route_unproved')
    attestation, digest = _attestation(path)
    if (type(expected_attestation_sha256) is not str or not SHA.fullmatch(expected_attestation_sha256)
            or digest != expected_attestation_sha256):
        _fail('production_attestation_changed')
    _v1_provider_manifest(attestation, route['vectorProfile'], provider=provider)
    node = project.lstat()
    if not stat.S_ISDIR(node.st_mode) or node.st_uid != os.getuid():
        _fail('journal_project_unavailable')
    from memvane.kernel.atomic_rename import discard_partial, partial_path, publish_new
    # The journal is assembled beside its path and published with a
    # no-replace rename: an interrupted creation leaves no journal, and the
    # rerun (under the same install lock) discards the partial file.
    building = partial_path(path)
    discard_partial(building, '-journal')
    fd = os.open(building, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        original = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        with closing(sqlite3.connect(building, timeout=2, isolation_level=None)) as db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA synchronous=FULL')
            db.execute(f'PRAGMA application_id={V1_APP_ID}')
            db.execute('PRAGMA user_version=1')
            db.execute('BEGIN IMMEDIATE')
            db.execute(V1_SELECTION_DDL)
            db.execute(V1_READER_DDL)
            db.execute('INSERT INTO selection VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (V1_SCHEMA, 1, 'prepared', route['installId'], route['scopeId'],
                        str(project), node.st_ino, route['stateFormat'], '1.0.0', plugin_version(),
                        route['storePath'], route['storeNode'][1], route['storeIncarnation'],
                        route_sha, digest, _canon(route['vectorProfile']), None, 0))
            _inspect_v1_row(db)
            if (_attestation(path)[1] != digest
                    or _classify_v4(project, settings, route).get('state') != 'candidate'
                    or project.lstat().st_ino != node.st_ino):
                _fail('journal_identity_changed')
            db.commit()
        fd = os.open(building, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            current = os.fstat(fd)
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                _fail('journal_file_changed')
            os.fsync(fd)
        finally:
            os.close(fd)
        publish_new(building, path)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            _fail('journal_file_changed')
    except FileExistsError as exc:
        discard_partial(building, '-journal')
        raise JournalEvidenceError('journal_already_exists') from exc
    except (OSError, sqlite3.Error) as exc:
        discard_partial(building, '-journal')
        raise JournalEvidenceError('journal_creation_incomplete') from exc
    except BaseException:
        discard_partial(building, '-journal')
        raise
    return inspect_journal(path)


def _inspect_v1_row(db):
    """Read the v1 journal row without granting a reader or writer authority."""
    from memvane.kernel.memvane_version import version_key
    rows = db.execute('SELECT * FROM selection').fetchall()
    fields = ('id', 'schema', 'epoch', 'state', 'installId', 'scopeId',
              'projectRoot', 'projectInode', 'stateFormat', 'writerFloor',
              'maxPluginVersion', 'storePath', 'storeInode', 'storeIncarnation',
              'routeSha256', 'attestationSha256', 'vectorProfile',
              'vectorRecipe', 'newWriteSeen')
    if len(rows) != 1 or len(rows[0]) != len(fields):
        _fail('journal_selection_missing')
    row = dict(zip(fields, rows[0]))
    if (row['id'] != 1 or row['schema'] != V1_SCHEMA
            or type(row['epoch']) is not int or row['epoch'] < 1
            or row['state'] not in ('prepared', 'selected', 'restored')
            or any(type(row[key]) is not str or not ID.fullmatch(row[key])
                   for key in ('installId', 'scopeId', 'storeIncarnation'))
            or any(type(row[key]) is not int or row[key] < 1
                   for key in ('projectInode', 'stateFormat', 'storeInode'))
            or any(type(row[key]) is not str or not Path(row[key]).is_absolute()
                   for key in ('projectRoot', 'storePath'))
            or any(type(row[key]) is not str or version_key(row[key]) is None
                   for key in ('writerFloor', 'maxPluginVersion'))
            or any(type(row[key]) is not str or not SHA.fullmatch(row[key])
                   for key in ('routeSha256', 'attestationSha256'))
            or (row['vectorRecipe'] is not None and
                (type(row['vectorRecipe']) is not str or not SHA.fullmatch(row['vectorRecipe'])))
            or type(row['newWriteSeen']) is not int or row['newWriteSeen'] not in (0, 1)):
        _fail('journal_value_invalid')
    try:
        profile = json.loads(row['vectorProfile'], object_pairs_hook=_pairs,
                             parse_constant=_nonfinite)
    except (TypeError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalEvidenceError('journal_value_invalid') from exc
    if _canon(profile) != row['vectorProfile']:
        _fail('journal_value_invalid')
    _strict_vector_profile(profile)
    row['vectorProfile'] = profile
    readers = db.execute('SELECT record FROM reader').fetchall()
    if len(readers) > 1 or (row['state'] == 'selected' and not readers):
        _fail('journal_reader_missing')
    reader = None
    if readers:
        try:
            raw = readers[0][0]
            if type(raw) is not str or len(raw.encode('utf-8')) > 65536:
                _fail('journal_reader_invalid')
            reader = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
            from memvane.journal.journal_reader import _schema3_shape
            if (not isinstance(reader, dict) or _canon(reader) != raw
                    or not _schema3_shape(reader)
                    or reader['install_id'] != row['installId']
                    or reader['scope'] != row['scopeId']
                    or reader['project'] != row['projectRoot']
                    or reader['project_node'][1] != row['projectInode']
                    or reader['db_path'] != row['storePath']
                    or reader['db_node'][1] != row['storeInode']
                    or reader['incarnation'] != row['storeIncarnation']):
                _fail('journal_reader_invalid')
        except (TypeError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise JournalEvidenceError('journal_reader_invalid') from exc
    row['reader'] = reader
    row['newWriteSeen'] = bool(row['newWriteSeen'])
    row.pop('id')
    return row


def inspect_journal(journal_path):
    path, db = _open(journal_path, write=False)
    with closing(db):
        db.execute('BEGIN')
        try:
            row = _inspect_v1_row(db)
            db.commit()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            db.rollback()
            raise JournalEvidenceError('journal_unavailable') from exc
        except BaseException:
            db.rollback()
            raise
    return {'journalPath': str(path), **row}







# Test seam: called with each conversion step name inside the transaction.
_conversion_step = None
















RELOCATED_ROUTE_FIELDS = frozenset(('journalPath', 'dbPath', 'queuePath'))
RELOCATED_ATTESTATION_PATHS = (('journalPath',), ('providerManifest', 'serviceSocketPath'))






def _executing_root():
    return Path(__file__).resolve().parents[2]


# (ledger phase, journal state) pairs an interrupted activate or restore leaves.
_SELECTION_TRANSITIONS = frozenset((
    ('prepared', 'prepared'), ('prepared', 'selected'), ('restoring', 'prepared'),
    ('restoring', 'selected'), ('restoring', 'restored'), ('restored', 'restored')))


def _verify_v1_selected(decision):
    """Reprove the selected v1 journal, route, ledger and provider."""
    from memvane.settings import api as project_dir
    from memvane.journal.build_record import read_v1
    from memvane.journal.ledger_io import _private_file, _read_json
    from memvane.kernel.memvane_version import same_major
    if not isinstance(decision, dict) or decision.get('state') != 'candidate':
        _fail('unselected_host_route')
    path = _private_parent(decision.get('journalPath'))
    project = Path(decision.get('projectRoot', ''))
    route_path = project_dir.runtime_file(project, 'route.json')
    raw = _private_file(route_path, 8192)
    route, digest = _route(raw, path)
    if route.get('schema') != 'memvane-route-v4' or not same_major(route['stateFormat']):
        _fail('state_format_mismatch')
    current = select_route(str(project), action='recall')
    if current != decision:
        _fail('host_decision_changed')
    row = inspect_journal(path)
    build = read_v1(path.parent)
    if build is None or build['phase'] != 'ready':
        _fail('initial_build_missing')
    identity = build['identity']
    ledger = _read_json(path.parent / 'ledger.json')
    if (isinstance(ledger, dict) and ledger.get('identity') == identity
            and digest == row['routeSha256'] == identity['routeSha256']
            and (ledger.get('phase'), row['state']) in _SELECTION_TRANSITIONS):
        # An activation or restore stopped between its durable steps; its
        # rerun converges. Recall answers without context meanwhile.
        _fail('selection_transition_pending')
    if (not isinstance(ledger, dict)
            or set(ledger) != {'schema', 'epoch', 'phase', 'identity'}
            or ledger['schema'] != 'memvane-install-ledger-v1'
            or type(ledger['epoch']) is not int or ledger['epoch'] < 1
            or ledger['phase'] != 'selected' or ledger['identity'] != identity
            or row['state'] != 'selected' or row['reader'] is None
            or digest != row['routeSha256'] or digest != identity['routeSha256']
            or row['attestationSha256'] != identity['attestationSha256']
            or row['projectInode'] != project.stat().st_ino
            or row['storeInode'] != identity['storeNode'][1]
            or any(row[key] != identity[key] for key in
                   ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation'))
            or any(decision.get(key) != identity[key] for key in
                   ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation', 'journalPath'))
            or row['vectorProfile'] != decision['vectorProfile']):
        _fail('selected_route_changed')
    attestation, sha = _attestation(path)
    if (sha != row['attestationSha256'] or not isinstance(attestation, dict)
            or attestation.get('journalPath') != str(path)):
        _fail('worker_attestation_changed')
    manifest, provider_available = _v1_provider_manifest(
        attestation, row['vectorProfile'], allow_local_unavailable=True)
    if (inspect_journal(path) != row or read_v1(path.parent) != build
            or _read_json(path.parent / 'ledger.json') != ledger
            or _private_file(route_path, 8192) != raw
            or select_route(str(project), action='recall') != decision):
        _fail('selected_route_changed')
    return {'status': 'installer_v1_selected_verified', 'productRouteAllowed': True,
            'journalPath': str(path), 'selectionEpoch': row['epoch'],
            'projectRoot': str(project), 'installId': row['installId'],
            'scopeId': row['scopeId'], 'storePath': row['storePath'],
            'storeIncarnation': row['storeIncarnation'], 'stateFormat': row['stateFormat'],
            'writerFloor': row['writerFloor'], 'vectorProfile': row['vectorProfile'],
            'providerManifest': manifest, 'providerAvailable': provider_available}


def verify_selected_route(decision):
    """Verify the selected 1.0 route, journal, store and provider identity."""
    if not isinstance(decision, dict) or decision.get('routeSchema') != 'memvane-route-v4':
        _fail('unselected_host_route')
    return _verify_v1_selected(decision)


def verify_selected_route_for_write(decision, action='update'):
    """Prove the selected route before a write."""
    proof = verify_selected_route(decision)
    if decision.get('routeSchema') == 'memvane-route-v4':
        if not proof.get('providerAvailable'):
            _fail('provider_identity_changed')
        from memvane.kernel.memvane_version import writer_floor_allows
        from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
        if not writer_floor_allows(proof['writerFloor']):
            _fail('writer_floor')
        require_cached_plugin_for_live_write(Path(proof['journalPath']).parent)
    return proof




def verify_dispatch_target(decision):
    """Read-only owner proof for choosing a child; never authorizes work here."""
    proof = verify_selected_route(decision)
    if proof['productRouteAllowed'] is not True:
        _fail('dispatch_target_unapproved')
    return proof


def verify_notification_target(decision):
    """Prove an external-edit hint using only local metadata and the journal."""
    from memvane.settings import api as project_dir
    from memvane.journal.notification_route import select_notification_route
    from memvane.journal.build_record import read_v1
    from memvane.journal.ledger_io import _private_file, _read_json
    from memvane.kernel.memvane_version import same_major, writer_floor_allows
    if not isinstance(decision, dict) or decision.get('routeSchema') != 'memvane-route-v4':
        _fail('unselected_host_route')
    project = Path(decision['projectRoot'])
    if select_notification_route(project) != decision:
        _fail('host_decision_changed')
    path = _private_parent(decision['journalPath'])
    raw = _private_file(project_dir.runtime_file(project, 'route.json'), 8192)
    route, digest = _route(raw, path)
    row = inspect_journal(path)
    build = read_v1(path.parent)
    ledger = _read_json(path.parent / 'ledger.json')
    attestation, att_sha = _attestation(path)
    if (build is None or build['phase'] != 'ready' or not isinstance(ledger, dict)
            or set(ledger) != {'schema', 'epoch', 'phase', 'identity'}
            or ledger.get('schema') != 'memvane-install-ledger-v1'
            or ledger.get('phase') != 'selected' or ledger.get('identity') != build['identity']
            or row['state'] != 'selected' or row['routeSha256'] != digest
            or att_sha != row['attestationSha256'] or row['reader'] is None
            or any(row[k] != decision[k] for k in
                   ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation'))
            or route['vectorProfile'] != row['vectorProfile']
            or not isinstance(attestation, dict)
            or attestation.get('vectorProfile') != row['vectorProfile']
            or not same_major(row['stateFormat']) or not writer_floor_allows(row['writerFloor'])):
        _fail('notification_target_unapproved')
    if (select_notification_route(project) != decision or inspect_journal(path) != row
            or _private_file(project_dir.runtime_file(project, 'route.json'), 8192) != raw):
        _fail('host_decision_changed')
    return {'status': 'installer_v1_notification_target', 'notificationOnly': True,
            'journalPath': str(path), 'selectionEpoch': row['epoch'],
            'projectRoot': str(project), 'installId': row['installId'],
            'scopeId': row['scopeId'], 'storePath': row['storePath'],
            'storeIncarnation': row['storeIncarnation']}


def verify_worker_attestation(decision):
    """Rehash model, worker and native closure once before an owned worker starts."""
    selected = verify_selected_route(decision)
    manifest = _worker_attestation_manifest(selected)
    if verify_selected_route(decision) != selected:
        _fail('worker_selection_changed')
    return manifest


def _worker_attestation_manifest(selected):
    if selected.get('productRouteAllowed') is not True:
        _fail('worker_provider_unapproved')
    path, db = _open(selected['journalPath'], write=False)
    with closing(db):
        db.execute('BEGIN')
        try:
            row = _inspect_v1_row(db)
            if (row['epoch'] != selected['selectionEpoch'] or row['state'] != 'selected'
                    or any(row[key] != selected.get(key) for key in
                           ('installId', 'scopeId', 'projectRoot', 'storePath',
                            'storeIncarnation', 'stateFormat', 'vectorProfile'))):
                _fail('worker_selection_changed')
            db.commit()
        except BaseException:
            db.rollback()
            raise
    record, digest = _attestation(path)
    if digest != row['attestationSha256']:
        _fail('worker_attestation_changed')
    manifest = _v1_provider_manifest(record, row['vectorProfile'])
    return manifest



def _v1_provider_manifest(record, profile, *, allow_local_unavailable=False, provider=None):
    """Verify provider bytes and runtime identity using this plugin copy.

    A fixture provider port (``provider``, or the test runtime root's) proves instead.
    """
    from memvane.provider.api import fixture_port, reviewed_provider, hash_file as _hash_file
    port = fixture_port(provider)
    if port is not None:
        from memvane.provider.types import RuntimeUnavailable
        try:
            return port.provider_manifest(record, profile, allow_local_unavailable=allow_local_unavailable)
        except RuntimeUnavailable as exc:
            raise JournalEvidenceError(str(exc)) from exc
    if (not isinstance(record, dict)
            or set(record) != {'schema', 'journalPath', 'vectorProfile', 'providerManifest',
                               'dependencyClosure', RUNTIME_FILES_FIELD}
            or record.get('schema') != 'memvane-provider-attestation-v1'
            or record.get('vectorProfile') != profile
            or not isinstance(record.get('providerManifest'), dict)
            or not isinstance(record.get('dependencyClosure'), list)
            or type(record.get(RUNTIME_FILES_FIELD)) is not str
            or not SHA.fullmatch(record[RUNTIME_FILES_FIELD])):
        _fail('worker_attestation_invalid')
    manifest = {**record['providerManifest'],
                'providerPath': str(bundle_file(PROVIDER_RELATIVE_PATH)),
                'workerPath': str(bundle_file(WORKER_RELATIVE_PATH))}
    try:
        from memvane.kernel import memvane_paths
        if (hashlib.sha256(_canon(record['dependencyClosure']).encode('utf-8')).hexdigest()
                != manifest['dependencyClosureSha256']
                or manifest['serviceSocketPath'] != str(memvane_paths.install_socket(
                    Path(record['journalPath']).parent))):
            _fail('worker_attestation_invalid')
        provider = reviewed_provider(manifest)
        if (provider.profile['profile_id'] != profile['profileId']
                or manifest['providerArtifactSha256'] != profile['providerArtifactSha256']
                or manifest['approvedProviderId'] != profile['approvedProviderId']
                or manifest['providerRelativePath'] != profile['providerRelativePath']):
            _fail('worker_attestation_invalid')
        try:
            local_available = all(_hash_file(manifest[path_key]) == manifest[hash_key]
                                  for path_key, hash_key in
                                  (('providerPath', 'providerArtifactSha256'),
                                   ('workerPath', 'workerSha256')))
        except (OSError, ValueError):
            local_available = False
        if not local_available:
            if allow_local_unavailable:
                return manifest, False
            _fail('worker_attestation_invalid')
        from memvane.provider import api as laya_embed_runtime
        try:
            for path_key, hash_key in (('nodePath', 'nodeSha256'),
                                       ('moduleEntryPath', 'moduleEntrySha256'), ('modelPath', 'modelSha256')):
                if laya_embed_runtime.runtime_file_sha256(manifest[path_key]) != manifest[hash_key]:
                    _fail('provider_identity_changed')
            _verify_runtime_files(record)
            active = laya_embed_runtime.select()
            if any(active[key] != manifest[key] for key in
                   ('runtimeId', 'nodeSha256', 'moduleEntrySha256', 'modelSha256')):
                _fail('provider_identity_changed')
            if active[laya_embed_runtime.FILES_SHA] != record[RUNTIME_FILES_FIELD]:
                _fail('provider_identity_changed')
        except (JournalEvidenceError, laya_embed_runtime.RuntimeUnavailable, OSError):
            _fail('provider_identity_changed')
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, JournalEvidenceError):
            raise
        raise JournalEvidenceError('worker_attestation_invalid') from exc
    return (manifest, True) if allow_local_unavailable else manifest


def _verify_runtime_files(record):
    """Re-hash every runtime file (node, JS dependencies, native, model)."""
    from memvane.provider.api import verify_attested_files
    from memvane.provider.types import RuntimeUnavailable
    try:
        verify_attested_files(record['providerManifest'], record.get(RUNTIME_FILES_FIELD))
    except (RuntimeUnavailable, OSError, KeyError, TypeError) as exc:
        raise JournalEvidenceError('worker_runtime_files_invalid') from exc


def verify_preview_worker_attestation(journal_path, provider_manifest):
    path = _private_parent(journal_path)
    record, digest = _attestation(path)
    row = inspect_journal(path)
    if row['state'] != 'prepared' or row['attestationSha256'] != digest:
        _fail('worker_attestation_changed')
    manifest = _v1_provider_manifest(record, row['vectorProfile'])
    if manifest != provider_manifest or inspect_journal(path) != row:
        _fail('worker_attestation_changed')
    return manifest
