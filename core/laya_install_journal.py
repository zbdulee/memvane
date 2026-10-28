"""Installer-owned synthetic activation journal and read-only route resolver.

This module writes only an explicit, private journal path. It never publishes a
project route, touches QMD, deletes an engine, or authorizes destructive GC.
Route-file publication is a separate installer step. CAS plus recovery records
detect interruption, but are not a cross-file power-loss transaction.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

from laya_index_a import IndexContractError, LayaIndexA
from laya_index_claim_queue import ClaimQueue, UnknownDatabase
from laya_index_host_route import (FIELDS as ROUTE_FIELDS, FIELDS_V2 as ROUTE_FIELDS_V2,
                                   RECORD as ROUTE_FILE, SCHEMA as ROUTE_SCHEMA,
                                   SCHEMA_V2 as ROUTE_SCHEMA_V2, select_route)
# Loaded for the executing-bundle proof (``_executing_from_candidate``),
# which requires laya_package from the sealed core; no QMD inventory is read.
import laya_package  # noqa: F401
from laya_product_bundle import (BundleEvidenceError, COMMIT, inspect_product_candidate,
                                 inspect_full_plugin_candidate,
                                 PRODUCTION_ATTESTATION_SCHEMA, RUNTIME_FILES_FIELD,
                                 production_provider_approved,
                                 validate_production_provider_attestation,
                                 validate_vector_profile)


SCHEMA_V3 = 'laya-installer-journal-v3'
SCHEMA = 'laya-installer-journal-v4'
DEVELOPMENT_APPROVAL_SCHEMA = 'laya-development-vector-approval-v1'
APP_ID = 0x4c594a31
VERSION = 4
# Every journal format this code reads and CAS-writes. A v3 journal still
# carries the retired global-QMD inventory column; it is never re-checked.
READ_VERSIONS = (3, 4)
ID = re.compile(r'[0-9a-f]{32}\Z')
GEN = re.compile(r'g-[0-9a-f]{32}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
PRODUCTION_ATTESTATION = 'production-provider-attestation.json'
_CREATE_V3 = '''CREATE TABLE selection(
  id INTEGER PRIMARY KEY CHECK(id=1), schema TEXT NOT NULL,
  epoch INTEGER NOT NULL, state TEXT NOT NULL,
  selected TEXT, selected_route_sha TEXT, previous TEXT, previous_route_sha TEXT,
  pending TEXT, pending_route_sha TEXT,
  scope_id TEXT NOT NULL, project_root TEXT NOT NULL,
  project_dev INTEGER NOT NULL, project_ino INTEGER NOT NULL,
  user_qmd_inventory TEXT NOT NULL, new_write_seen INTEGER NOT NULL,
  provider_attestation_sha TEXT, previous_provider_attestation_sha TEXT,
  provider_file_identity TEXT, previous_provider_file_identity TEXT)'''
_CREATE = '''CREATE TABLE selection(
  id INTEGER PRIMARY KEY CHECK(id=1), schema TEXT NOT NULL,
  epoch INTEGER NOT NULL, state TEXT NOT NULL,
  selected TEXT, selected_route_sha TEXT, previous TEXT, previous_route_sha TEXT,
  pending TEXT, pending_route_sha TEXT,
  scope_id TEXT NOT NULL, project_root TEXT NOT NULL,
  project_dev INTEGER NOT NULL, project_ino INTEGER NOT NULL,
  new_write_seen INTEGER NOT NULL,
  provider_attestation_sha TEXT, previous_provider_attestation_sha TEXT,
  provider_file_identity TEXT, previous_provider_file_identity TEXT)'''
_FORMATS = {3: (SCHEMA_V3, _CREATE_V3), 4: (SCHEMA, _CREATE)}
# The withdrawn-selection state. A v3 file stores its old name; readers see
# only ``restored_origin``.
RESTORED = 'restored_origin'
_V3_RESTORED = 'restored_qmd'
_FIELDS_V4 = ('id', 'schema', 'epoch', 'state', 'selected', 'selected_route_sha',
              'previous', 'previous_route_sha', 'pending', 'pending_route_sha',
              'scope_id', 'project_root', 'project_dev', 'project_ino',
              'new_write_seen', 'provider_attestation_sha',
              'previous_provider_attestation_sha', 'provider_file_identity',
              'previous_provider_file_identity')


class JournalEvidenceError(ValueError):
    pass


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


def stage_production_attestation(journal_path, bundle_proof, provider_manifest,
                                 dependency_closure):
    """No-clobber installer record, validated before journal CAS can bind it.

    Repeating an identical, complete staging operation is safe. A partial or
    different file is retained for explicit recovery instead of overwritten.
    """
    journal = _private_parent(journal_path)
    proof = inspect_full_plugin_candidate(bundle_proof.get('pluginRoot'))
    if proof != bundle_proof:
        _fail('production_bundle_changed')
    record = {'schema': PRODUCTION_ATTESTATION_SCHEMA,
              'sourceCommit': proof['sourceCommit'],
              'bundleManifestSha256': proof['bundleManifestSha256'],
              'ownershipManifestSha256': proof['ownershipManifestSha256'],
              'journalPath': str(journal),
              'vectorProfile': proof['vectorProfile'],
              'providerManifest': provider_manifest,
              'dependencyClosure': dependency_closure}
    try:
        from laya_embed_runtime import RuntimeUnavailable, sealed_files_sha256
        files = sealed_files_sha256(provider_manifest.get('moduleEntryPath')
                                    if isinstance(provider_manifest, dict) else None)
    except (RuntimeUnavailable, OSError) as exc:
        raise JournalEvidenceError('production_attestation_invalid') from exc
    if files is not None:
        # Seal every runtime file, not only the hashed entries and closure.
        record[RUNTIME_FILES_FIELD] = files
    try:
        validate_production_provider_attestation(proof['vectorProfile'], proof, record)
    except BundleEvidenceError as exc:
        raise JournalEvidenceError('production_attestation_invalid') from exc
    raw = _canon(record).encode('utf-8')
    if len(raw) > 1024 * 1024:
        _fail('production_attestation_too_large')
    target = journal.parent / PRODUCTION_ATTESTATION
    if target.exists() or target.is_symlink():
        existing, digest = _attestation(journal)
        if existing != record:
            _fail('production_attestation_already_owned')
        return {'attestationPath': str(target), 'attestationSha256': digest}
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        parent_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise JournalEvidenceError('production_attestation_stage_incomplete') from exc
    existing, digest = _attestation(journal)
    if existing != record:
        _fail('production_attestation_changed')
    return {'attestationPath': str(target), 'attestationSha256': digest}


def _provider_file_identity(attestation):
    """Cheap immutable-file identity after one full validated hash pass."""
    manifest = attestation['providerManifest']
    paths = {manifest[key] for key in
             ('providerPath', 'nodePath', 'moduleEntryPath', 'workerPath', 'modelPath')}
    paths.update(item['path'] for item in attestation['dependencyClosure'])
    rows = []
    try:
        for raw in sorted(paths):
            path = Path(raw)
            if not path.is_absolute() or path.resolve(strict=True) != path:
                _fail('production_provider_file_changed')
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                _fail('production_provider_file_changed')
            rows.append([raw, info.st_dev, info.st_ino, info.st_size,
                         info.st_mtime_ns, info.st_ctime_ns])
    except (OSError, RuntimeError, TypeError, KeyError) as exc:
        raise JournalEvidenceError('production_provider_file_unavailable') from exc
    return rows


def _strict_vector_profile(value):
    if not isinstance(value, dict):
        _fail('invalid_vector_profile')
    relative = value.get('providerRelativePath')
    digest = value.get('providerArtifactSha256')
    if type(relative) is not str or type(digest) is not str:
        _fail('invalid_vector_profile')
    try:
        return validate_vector_profile(value, {relative: {'sha256': digest}})
    except BundleEvidenceError as exc:
        raise JournalEvidenceError('invalid_vector_profile') from exc


def _route(raw, journal_path):
    if type(raw) is not bytes or len(raw) > 8192:
        _fail('invalid_route_bytes')
    try:
        route = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=_nonfinite)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalEvidenceError('invalid_route_bytes') from exc
    schema = route.get('schema') if isinstance(route, dict) else None
    expected_fields = (ROUTE_FIELDS if schema == ROUTE_SCHEMA else
                       ROUTE_FIELDS_V2 if schema == ROUTE_SCHEMA_V2 else None)
    if (not isinstance(route, dict) or expected_fields is None or
            set(route) != expected_fields or route.get('status') != 'active' or
            route.get('journalPath') != str(journal_path)):
        _fail('route_contract_mismatch')
    _strict_vector_profile(route.get('vectorProfile'))
    return route, hashlib.sha256(raw).hexdigest()


def _private_parent(path):
    try:
        path = Path(path)
        if not path.is_absolute() or path.is_symlink() or path.name != 'install-journal.sqlite':
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


def _identity(proof, route, journal_path):
    if not isinstance(proof, dict) or proof.get('status') not in (
            'exact_commit_candidate_consistent',
            'exact_commit_full_plugin_candidate_consistent'):
        _fail('unverified_bundle')
    full = proof['status'] == 'exact_commit_full_plugin_candidate_consistent'
    live = (inspect_full_plugin_candidate(proof.get('pluginRoot')) if full else
            inspect_product_candidate(proof.get('engineRoot')))
    if live != proof:
        _fail('bundle_proof_changed')
    root = live['pluginRoot'] if full else live['engineRoot']
    if ((route['schema'] == ROUTE_SCHEMA and
         Path(root).parent != journal_path.parent) or
            route.get('pluginRoot') != root or
            type(route.get('installId')) is not str or not ID.fullmatch(route['installId']) or
            type(route.get('generation')) is not str or not GEN.fullmatch(route['generation']) or
            route.get('bundleManifestSha256') != live['bundleManifestSha256'] or
            route.get('ownershipManifestSha256') != live['ownershipManifestSha256'] or
            route.get('vectorProfile') != live.get('vectorProfile')):
        _fail('route_bundle_mismatch')
    return {'pluginRoot': root, 'installId': route['installId'],
            'generation': route['generation'],
            'bundleManifestSha256': live['bundleManifestSha256'],
            'ownershipManifestSha256': live['ownershipManifestSha256'],
            'sourceCommit': live['sourceCommit'],
            'vectorProfile': live.get('vectorProfile'),
            'bundleKind': 'full_plugin' if full else 'engine_subbundle'}


def _inspect_identity(identity):
    if not isinstance(identity, dict):
        _fail('invalid_bundle_identity')
    if identity.get('bundleKind') == 'full_plugin':
        proof = inspect_full_plugin_candidate(identity.get('pluginRoot'))
    elif identity.get('bundleKind') == 'engine_subbundle':
        proof = inspect_product_candidate(identity.get('pluginRoot'))
    else:
        _fail('invalid_bundle_identity')
    if (proof['bundleManifestSha256'] != identity.get('bundleManifestSha256') or
            proof['ownershipManifestSha256'] != identity.get('ownershipManifestSha256') or
            proof['sourceCommit'] != identity.get('sourceCommit') or
            proof.get('vectorProfile') != identity.get('vectorProfile')):
        _fail('installed_bundle_changed')
    return proof


def _bind_attestation(journal_path, identity, proof):
    if identity.get('bundleKind') != 'full_plugin':
        return None, None
    record, digest = _attestation(journal_path)
    if record is None:
        return None, None
    try:
        validate_production_provider_attestation(proof.get('vectorProfile'),
                                                 proof, record)
    except BundleEvidenceError as exc:
        raise JournalEvidenceError('production_attestation_invalid') from exc
    return digest, _provider_file_identity(record)


def preflight_product_candidate(journal_path, *, require_executing=False):
    """Review the pending installed provider before publishing a QMD cutover.

    The returned digest is an expectation to persist before the route becomes
    visible. Recovery must compare current attestation bytes to this value.
    """
    pending = inspect_journal(journal_path)
    if pending['state'] != 'candidate_ready' or pending['pending'] is None:
        _fail('product_candidate_not_pending')
    identity = pending['pending']
    if identity.get('bundleKind') != 'full_plugin':
        _fail('product_full_plugin_required')
    proof = _inspect_identity(identity)
    if require_executing:
        _verified_executing_root(identity, proof)
    attestation, digest = _attestation(journal_path)
    if not production_provider_approved(
            proof.get('vectorProfile'), proof, attestation,
            journal_attestation_sha256=digest):
        _fail('production_provider_unapproved')
    identity_rows = _provider_file_identity(attestation)
    return {'attestationSha256': digest,
            'providerFileIdentitySha256': hashlib.sha256(_canon(identity_rows).encode('utf-8')).hexdigest(),
            'providerManifest': attestation['providerManifest'],
            'pendingRouteSha256': pending['pendingRouteSha256']}


def _executing_from_candidate(plugin_root):
    """The verifier and its loaded Laya dependencies must be the sealed files."""
    core = Path(plugin_root) / 'core'
    required = ('laya_install_journal', 'laya_product_bundle', 'laya_index_a',
                'laya_index_claim_queue', 'laya_index_host_route', 'laya_package',
                'config')
    for name in required:
        module = sys.modules.get(name)
        source = getattr(module, '__file__', None)
        if source is None or Path(source).resolve(strict=False) != core / (name + '.py'):
            _fail('executing_code_outside_bundle')
    # A candidate/core first import path is necessary so subsequent runtime,
    # flow and source-port imports cannot silently come from another checkout.
    if not sys.path or Path(sys.path[0]).resolve(strict=False) != core:
        _fail('future_import_path_unbound')


def _verified_executing_root(selected, owner_proof):
    """Prove this host's sealed copy against the journal-bound owner copy."""
    root = Path(__file__).resolve().parent.parent
    if str(root) != selected['pluginRoot']:
        try:
            local = inspect_full_plugin_candidate(root)
        except BundleEvidenceError as exc:
            raise JournalEvidenceError('executing_code_outside_bundle') from exc
        if (local['pluginRoot'] != str(root) or
                any(local[key] != owner_proof[key] for key in
                    ('sourceCommit', 'bundleManifestSha256',
                     'vectorProfile', 'includedFiles'))):
            _fail('executing_bundle_not_selected')
    _executing_from_candidate(root)
    return str(root)


def _project(route):
    try:
        path = Path(route.get('projectRoot'))
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            _fail('invalid_project_root')
        path = path.resolve(strict=True)
        info = path.stat()
        if (str(path) != route['projectRoot'] or type(route.get('scopeId')) is not str or
                not ID.fullmatch(route['scopeId'])):
            _fail('invalid_project_root')
        return str(path), info.st_dev, info.st_ino
    except (OSError, TypeError, RuntimeError) as exc:
        raise JournalEvidenceError('project_root_unavailable') from exc


def _observed_route(row, supplied_raw):
    route_path = Path(row['project_root']) / '.auto-context' / ROUTE_FILE
    try:
        if route_path.is_symlink() or not route_path.is_file():
            _fail('route_unavailable')
        before = route_path.stat()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                before.st_mode & stat.S_IWOTH or before.st_size > 8192):
            _fail('unsafe_route_file')
        fd = os.open(route_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            raw = os.read(fd, 8193)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        now = route_path.stat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(raw) > 8192 or identity(before) != identity(after) or identity(before) != identity(now):
            _fail('route_file_changed')
        if supplied_raw is not None and raw != supplied_raw:
            _fail('observed_route_mismatch')
        return raw
    except OSError as exc:
        raise JournalEvidenceError('route_unavailable') from exc


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
    if db.execute('PRAGMA application_id').fetchone()[0] != APP_ID:
        return None
    version = db.execute('PRAGMA user_version').fetchone()[0]
    expected = _FORMATS.get(version)
    if (expected is None or
            db.execute("SELECT sql FROM sqlite_master WHERE name='selection'").fetchone()
            != (expected[1],)):
        return None
    return version


def _recover_interrupted_transaction(path):
    """Roll back a crashed writer's hot rollback journal, then nothing else.

    A ``mode=ro`` connection cannot roll a hot journal back and fails, which
    would stop every reader until the next writer. One read on a read-write
    connection makes SQLite restore the last committed state under its own
    exclusive lock. A live writer holds RESERVED, so its journal is not hot
    and is left alone.
    """
    side = path.with_name(path.name + '-journal')
    if not side.exists() or side.is_symlink():
        return False
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=2,
                                 isolation_level=None)) as db:
        db.execute('PRAGMA application_id').fetchone()
    return True


def _open(path, *, write):
    path = _private_parent(path)
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('unsafe_journal_file')
        uri = path.as_uri() + ('?mode=rw' if write else '?mode=ro')
        db = sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)
        try:
            version = _schema_version(db)
        except sqlite3.OperationalError:
            db.close()
            if write or not _recover_interrupted_transaction(path):
                raise
            db = sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)
            version = _schema_version(db)
        if version is None:
            db.close()
            _fail('journal_schema_unknown')
        return path, db
    except (OSError, sqlite3.Error) as exc:
        raise JournalEvidenceError('journal_unavailable') from exc


def journal_version(journal_path):
    """Read-only journal format (3 or 4); refuses an unknown file."""
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


def _row(db):
    # Re-read inside the caller's transaction: a v3->v4 conversion may have
    # committed between ``_open`` and this read.
    version = _schema_version(db)
    if version is None:
        _fail('journal_schema_unknown')
    record = db.execute('SELECT * FROM selection WHERE id=1').fetchone()
    if record is None or db.execute('SELECT count(*) FROM selection').fetchone()[0] != 1:
        _fail('journal_selection_missing')
    fields = (_FIELDS_V4 if version == 4 else
              _FIELDS_V4[:14] + ('user_qmd_inventory',) + _FIELDS_V4[14:])
    if len(record) != len(fields):
        _fail('journal_schema_unknown')
    row = dict(zip(fields, record))
    stored_restored = RESTORED if version == 4 else _V3_RESTORED
    if (row['schema'] != _FORMATS[version][0] or type(row['epoch']) is not int or
            row['epoch'] < 1 or
            row['state'] not in ('candidate_ready', 'pending', 'active', 'restored_old',
                                 stored_restored) or
            type(row['new_write_seen']) is not int or row['new_write_seen'] not in (0, 1)):
        _fail('journal_state_invalid')
    if row['state'] == stored_restored:
        row['state'] = RESTORED
    # The v3 global-QMD inventory is retired evidence: carried, never bound.
    row.pop('user_qmd_inventory', None)
    row['_version'] = version
    try:
        for key in ('selected', 'previous', 'pending',
                    'provider_file_identity', 'previous_provider_file_identity'):
            row[key] = json.loads(row[key], object_pairs_hook=_pairs,
                                  parse_constant=_nonfinite) if row[key] is not None else None
    except (TypeError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalEvidenceError('journal_value_invalid') from exc
    for key in ('provider_file_identity', 'previous_provider_file_identity'):
        value = row[key]
        if value is not None and (not isinstance(value, list) or
                not value or any(not isinstance(item, list) or len(item) != 6 or
                    type(item[0]) is not str or
                    any(type(n) is not int or n < 0 for n in item[1:])
                    for item in value) or
                [item[0] for item in value] != sorted(set(item[0] for item in value))):
            _fail('journal_provider_identity_invalid')
    if (type(row['scope_id']) is not str or not ID.fullmatch(row['scope_id']) or
            type(row['project_root']) is not str or not Path(row['project_root']).is_absolute() or
            type(row['project_dev']) is not int or type(row['project_ino']) is not int or
            any(value is not None and (type(value) is not str or not SHA.fullmatch(value))
                for value in (row['selected_route_sha'], row['previous_route_sha'],
                              row['pending_route_sha'],
                              row['provider_attestation_sha'],
                              row['previous_provider_attestation_sha']))):
        _fail('journal_value_invalid')
    for key in ('selected', 'previous', 'pending'):
        value = row[key]
        if value is None:
            continue
        if (not isinstance(value, dict) or
                set(value) != {'pluginRoot', 'installId', 'generation',
                               'bundleManifestSha256', 'ownershipManifestSha256',
                               'sourceCommit', 'bundleKind', 'vectorProfile'} or
                type(value['pluginRoot']) is not str or
                not Path(value['pluginRoot']).is_absolute() or
                type(value['installId']) is not str or not ID.fullmatch(value['installId']) or
                type(value['generation']) is not str or not GEN.fullmatch(value['generation']) or
                any(type(value[k]) is not str or not SHA.fullmatch(value[k]) for k in
                    ('bundleManifestSha256', 'ownershipManifestSha256')) or
                type(value['sourceCommit']) is not str or
                not COMMIT.fullmatch(value['sourceCommit']) or
                value['bundleKind'] not in ('full_plugin', 'engine_subbundle')):
            _fail('journal_value_invalid')
        if value['vectorProfile'] is not None:
            _strict_vector_profile(value['vectorProfile'])
    if ((row['state'] in ('active', 'restored_old') and
         (row['selected'] is None or row['selected_route_sha'] is None)) or
            (row['state'] in ('candidate_ready', 'pending') and
             (row['pending'] is None or row['pending_route_sha'] is None)) or
            (row['state'] == RESTORED and
             (row['selected'] is not None or row['selected_route_sha'] is not None
              or row['pending'] is not None or row['pending_route_sha'] is not None
              or row['new_write_seen'] or row['provider_attestation_sha'] or
              row['provider_file_identity']))):
        _fail('journal_state_invalid')
    return row


def create_journal(journal_path, bundle_proof, proposed_route_raw, *, user_qmd_roots=None):
    """No-clobber synthetic v4 journal; route publication remains external.

    ``user_qmd_roots`` is accepted for older callers and ignored: a journal
    binds only Laya-owned evidence, never the global QMD directories.
    """
    del user_qmd_roots
    path = _private_parent(journal_path)
    if path.exists() or path.is_symlink():
        _fail('journal_already_exists')
    route, digest = _route(proposed_route_raw, path)
    identity = _identity(bundle_proof, route, path)
    project, dev, ino = _project(route)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(path, timeout=2, isolation_level=None)) as db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA synchronous=FULL')
            db.execute(f'PRAGMA application_id={APP_ID}')
            db.execute(f'PRAGMA user_version={VERSION}')
            db.execute('BEGIN IMMEDIATE')
            db.execute(_CREATE)
            db.execute('INSERT INTO selection VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (SCHEMA, 1, 'candidate_ready', None, None, None, None,
                        _canon(identity), digest, route['scopeId'], project, dev, ino,
                        0, None, None, None, None))
            db.commit()
        file_fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, sqlite3.Error) as exc:
        raise JournalEvidenceError('journal_creation_incomplete') from exc
    return inspect_journal(path)


def inspect_journal(journal_path):
    path, db = _open(journal_path, write=False)
    with closing(db):
        db.execute('BEGIN')
        try:
            row = _row(db)
            db.commit()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            db.rollback()
            raise JournalEvidenceError('journal_unavailable') from exc
        except BaseException:
            db.rollback()
            raise
    return {'journalPath': str(path), 'epoch': row['epoch'], 'state': row['state'],
            'selected': row['selected'], 'previous': row['previous'],
            'pending': row['pending'], 'scopeId': row['scope_id'],
            'projectRoot': row['project_root'], 'newWriteSeen': bool(row['new_write_seen']),
            'selectedRouteSha256': row['selected_route_sha'],
            'previousRouteSha256': row['previous_route_sha'],
            'pendingRouteSha256': row['pending_route_sha'],
            'providerAttestationSha256': row['provider_attestation_sha'],
            'previousProviderAttestationSha256': row['previous_provider_attestation_sha'],
            'providerFileIdentitySha256': (hashlib.sha256(_canon(row['provider_file_identity']).encode('utf-8')).hexdigest()
                                           if row['provider_file_identity'] is not None else None)}


def _cas(path, expected_epoch, mutate):
    path, db = _open(path, write=True)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        row = _row(db)
        if type(expected_epoch) is not int or row['epoch'] != expected_epoch:
            _fail('stale_journal_cas')
        values = mutate(path, row)
        if values is not None:
            if values.get('state') == RESTORED and row['_version'] == 3:
                values = {**values, 'state': _V3_RESTORED}
            fields = ','.join(f'{key}=?' for key in values)
            args = (*values.values(), expected_epoch + 1, expected_epoch)
            changed = db.execute(f'UPDATE selection SET {fields},epoch=? WHERE id=1 AND epoch=?',
                                 args)
            if changed.rowcount != 1:
                _fail('stale_journal_cas')
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    return inspect_journal(path)


# Test seam: called with each conversion step name inside the transaction.
_conversion_step = None


def _step(name):
    if _conversion_step is not None:
        _conversion_step(name)


def convert_to_v4(journal_path):
    """Rewrite a v3 journal as v4 in one SQLite transaction; idempotent.

    The logical row is unchanged: the retired global-QMD inventory column
    is dropped and ``restored_qmd`` is stored as ``restored_origin``. The
    epoch stays the same, so a reader's proof taken before the commit still
    equals one taken after it. A crash before COMMIT leaves the v3 file
    (SQLite rolls the hot journal back); after COMMIT the file is v4.
    The caller holds the install lock; this function takes no other lock.
    """
    path, db = _open(journal_path, write=True)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        before = _row(db)
        if before.pop('_version') == 4:
            db.commit()
            return {'status': 'journal_v4_current', 'journalPath': str(path),
                    'epoch': before['epoch']}
        _step('locked')
        stored = db.execute('SELECT ' + ','.join(_FIELDS_V4) +
                            ' FROM selection WHERE id=1').fetchone()
        values = dict(zip(_FIELDS_V4, stored))
        values['schema'] = SCHEMA
        if values['state'] == _V3_RESTORED:
            values['state'] = RESTORED
        db.execute('DROP TABLE selection')
        _step('dropped')
        # Never ALTER ... RENAME: SQLite would rewrite the stored CREATE text
        # and the exact-schema check would refuse the converted file.
        db.execute(_CREATE)
        _step('created')
        db.execute('INSERT INTO selection(' + ','.join(_FIELDS_V4) + ') VALUES(' +
                   ','.join('?' * len(_FIELDS_V4)) + ')',
                   tuple(values[key] for key in _FIELDS_V4))
        _step('inserted')
        db.execute(f'PRAGMA user_version={VERSION}')
        _step('versioned')
        after = _row(db)
        if (after.pop('_version') != 4 or after.pop('schema') != SCHEMA or
                before.pop('schema') != SCHEMA_V3 or after != before):
            _fail('journal_conversion_mismatch')
        db.commit()
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()
    _step('committed')
    try:
        file_fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise JournalEvidenceError('journal_conversion_unsynced') from exc
    return {'status': 'journal_v4_converted', 'journalPath': str(path),
            'epoch': after['epoch']}


def cas_stage_update(journal_path, expected_epoch, next_bundle_proof, proposed_route_raw):
    def stage(path, row):
        if row['state'] not in ('active', 'restored_old'):
            _fail('journal_not_active')
        route, digest = _route(proposed_route_raw, path)
        identity = _identity(next_bundle_proof, route, path)
        if (_project(route) != (row['project_root'], row['project_dev'], row['project_ino']) or
                route['scopeId'] != row['scope_id'] or identity == row['selected']):
            _fail('update_identity_mismatch')
        return {'state': 'pending', 'previous': _canon(row['selected']),
                'previous_route_sha': row['selected_route_sha'],
                'previous_provider_attestation_sha': row['provider_attestation_sha'],
                'previous_provider_file_identity': (_canon(row['provider_file_identity'])
                                                    if row['provider_file_identity'] is not None else None),
                'pending': _canon(identity), 'pending_route_sha': digest,
                'new_write_seen': 0}
    return _cas(journal_path, expected_epoch, stage)


def cas_replace_inactive_candidate(journal_path, expected_epoch,
                                   current_route_raw, next_bundle_proof,
                                   next_route_raw):
    """CAS a still-unselected initial build to an exact replacement bundle.

    Only installer-owned, private candidate metadata changes. The vector
    profile, project, scope, install ID, generation and every store path stay
    fixed. This function also supports a guarded rollback to the old bundle.
    """
    def replace(path, row):
        if (row['state'] != 'candidate_ready' or row['pending'] is None or
                row['selected'] is not None or row['previous'] is not None or
                row['new_write_seen']):
            _fail('inactive_candidate_not_replaceable')
        before, before_sha = _route(current_route_raw, path)
        after, after_sha = _route(next_route_raw, path)
        if before_sha != row['pending_route_sha']:
            _fail('inactive_candidate_route_changed')
        next_identity = _identity(next_bundle_proof, after, path)
        replaceable = {'pluginRoot', 'bundleManifestSha256',
                       'ownershipManifestSha256'}
        if (set(before) != set(after) or
                any(before[key] != after[key] for key in before.keys() - replaceable)
                or any(before[key] != row['pending'].get(key) for key in
                       ('pluginRoot', 'installId', 'generation',
                        'bundleManifestSha256', 'ownershipManifestSha256',
                        'vectorProfile'))
                or any(row['pending'][key] != next_identity[key] for key in
                       ('installId', 'generation', 'vectorProfile'))
                or _project(after) != (row['project_root'], row['project_dev'],
                                       row['project_ino'])
                or after['scopeId'] != row['scope_id']):
            _fail('inactive_candidate_identity_mismatch')
        return {'pending': _canon(next_identity), 'pending_route_sha': after_sha}
    return _cas(journal_path, expected_epoch, replace)


def cas_recover(journal_path, expected_epoch, observed_route_raw):
    """Finish pending switch if new route is visible, else retain exact old route."""
    def recover(path, row):
        if row['state'] not in ('candidate_ready', 'pending') or row['pending'] is None:
            _fail('journal_not_pending')
        route, digest = _route(_observed_route(row, observed_route_raw), path)
        if (_project(route) != (row['project_root'], row['project_dev'], row['project_ino']) or
                route['scopeId'] != row['scope_id']):
            _fail('recovery_project_mismatch')
        if digest == row['pending_route_sha']:
            if any(route.get(k) != row['pending'].get(k) for k in
                   ('pluginRoot', 'installId', 'generation', 'bundleManifestSha256',
                    'ownershipManifestSha256', 'vectorProfile')):
                _fail('recovery_identity_mismatch')
            proof = _inspect_identity(row['pending'])
            att_sha, file_identity = _bind_attestation(path, row['pending'], proof)
            return {'state': 'active', 'selected': _canon(row['pending']),
                    'selected_route_sha': digest, 'pending': None,
                    'pending_route_sha': None,
                    'provider_attestation_sha': att_sha,
                    'provider_file_identity': (_canon(file_identity) if file_identity is not None else None)}
        if row['previous'] is not None and digest == row['previous_route_sha']:
            _inspect_identity(row['previous'])
            return {'state': 'restored_old', 'selected': _canon(row['previous']),
                    'selected_route_sha': digest, 'pending': None,
                    'pending_route_sha': None,
                    'provider_attestation_sha': row['previous_provider_attestation_sha'],
                    'provider_file_identity': (_canon(row['previous_provider_file_identity'])
                                               if row['previous_provider_file_identity'] is not None else None)}
        _fail('uncertain_route_recovery')
    return _cas(journal_path, expected_epoch, recover)


def cas_activate(journal_path, expected_epoch, observed_route_raw):
    """Commit only the proposed route branch; another observation blocks."""
    def activate(path, row):
        if row['state'] not in ('candidate_ready', 'pending') or row['pending'] is None:
            _fail('journal_not_pending')
        route, digest = _route(_observed_route(row, observed_route_raw), path)
        if digest != row['pending_route_sha']:
            _fail('activation_not_selected')
        if (_project(route) != (row['project_root'], row['project_dev'], row['project_ino']) or
                route['scopeId'] != row['scope_id'] or
                any(route.get(k) != row['pending'].get(k) for k in
                    ('pluginRoot', 'installId', 'generation', 'bundleManifestSha256',
                     'ownershipManifestSha256', 'vectorProfile'))):
            _fail('activation_identity_mismatch')
        proof = _inspect_identity(row['pending'])
        att_sha, file_identity = _bind_attestation(path, row['pending'], proof)
        return {'state': 'active', 'selected': _canon(row['pending']),
                'selected_route_sha': digest, 'pending': None,
                'pending_route_sha': None,
                'provider_attestation_sha': att_sha,
                'provider_file_identity': (_canon(file_identity) if file_identity is not None else None)}
    return _cas(journal_path, expected_epoch, activate)


def cas_mark_new_write(journal_path, expected_epoch):
    def mark(_path, row):
        if row['state'] != 'active' or row['selected'] is None:
            _fail('journal_not_active')
        return {'new_write_seen': 1}
    return _cas(journal_path, expected_epoch, mark)


def cas_restore(journal_path, expected_epoch, observed_old_route_raw):
    def restore(path, row):
        if row['state'] != 'active' or row['previous'] is None or row['new_write_seen']:
            _fail('rollback_not_authorized')
        route, digest = _route(_observed_route(row, observed_old_route_raw), path)
        if digest != row['previous_route_sha'] or any(route.get(k) != row['previous'].get(k)
                for k in ('pluginRoot', 'installId', 'generation',
                          'bundleManifestSha256', 'ownershipManifestSha256',
                          'vectorProfile')):
            _fail('rollback_route_mismatch')
        _inspect_identity(row['previous'])
        return {'state': 'restored_old', 'selected': _canon(row['previous']),
                'selected_route_sha': digest, 'new_write_seen': 0,
                'provider_attestation_sha': row['previous_provider_attestation_sha'],
                'provider_file_identity': (_canon(row['previous_provider_file_identity'])
                                           if row['previous_provider_file_identity'] is not None else None)}
    return _cas(journal_path, expected_epoch, restore)


def cas_restore_fresh(journal_path, expected_epoch, expected_new_route_sha):
    """Withdraw only a fresh-wiki route under its independent restoring ledger."""
    def restore(path, row):
        if (row['state'] != 'active' or row['previous'] is not None or
                row['new_write_seen'] or row['selected'] is None or
                row['selected_route_sha'] != expected_new_route_sha):
            _fail('fresh_wiki_rollback_not_authorized')
        from laya_fresh_wiki_origin import (LEDGER, MARKER, ROUTE, _check_marker,
                                            _load)
        ledger = _load(Path(path).parent / LEDGER)
        if (ledger['phase'] != 'restoring' or ledger['journalPath'] != str(path) or
                ledger['projectRoot'] != row['project_root'] or
                ledger['routeSha256'] != expected_new_route_sha):
            _fail('fresh_wiki_restore_ledger_mismatch')
        auto = Path(row['project_root']) / '.auto-context'
        _check_marker(ledger, auto / MARKER)
        if (auto / ROUTE).exists() or (auto / ROUTE).is_symlink():
            _fail('fresh_wiki_route_still_selected')
        return {'state': RESTORED, 'selected': None,
                'selected_route_sha': None, 'pending': None,
                'pending_route_sha': None, 'new_write_seen': 0,
                'provider_attestation_sha': None,
                'provider_file_identity': None}
    return _cas(journal_path, expected_epoch, restore)


# Full-proof failures that only say the executing or bound plugin bundle moved
# (a host plugin update, or the bound cache was replaced or removed). The
# index stores, route and journal may still be intact; see
# ``verify_selected_route_read_only``.
BUNDLE_DRIFT_CODES = frozenset((
    'executing_code_outside_bundle', 'executing_bundle_not_selected',
    'installed_bundle_changed', 'production_provider_file_unavailable',
    'production_provider_file_changed', 'production_provider_identity_changed'))


def is_bundle_drift(exc):
    """True when a full selected proof failed only on plugin bundle identity."""
    if isinstance(exc, BundleEvidenceError):
        return True
    return type(exc) is JournalEvidenceError and str(exc) in BUNDLE_DRIFT_CODES


def _executing_root():
    return Path(__file__).resolve().parent.parent


def _verify_selected_route(decision, *, dispatch_target=False, read_only=False):
    """Read-only product proof for a host-validated candidate decision.

    The caller must still construct LayaIndexRuntime, which rechecks DB and
    queue identity before serving. No legacy fallback is authorized on error.
    ``read_only`` skips only the checks that read the bound plugin bundle or
    prove the executing bundle; it never authorizes a write.
    """
    if (not isinstance(decision, dict) or decision.get('state') != 'candidate' or
            any(type(decision.get(key)) is not str for key in
                ('journalPath', 'pluginRoot', 'projectRoot', 'dbPath',
                 'queuePath', 'dbIncarnation', 'scopeId'))):
        _fail('unselected_host_route')
    journal = _private_parent(decision.get('journalPath'))
    engine = Path(decision.get('pluginRoot'))
    raw = _observed_route({'project_root': decision.get('projectRoot')}, None)
    route, digest = _route(raw, journal)
    allowed = {digest}
    if (journal != journal.parent / 'install-journal.sqlite' or
            (route['schema'] == ROUTE_SCHEMA and engine.parent != journal.parent)):
        _fail('journal_engine_root_mismatch')
    fresh = select_route(route['projectRoot'], action='recall')
    if fresh.get('state') != 'candidate' or fresh != decision:
        _fail('host_route_unverified')
    for key in ROUTE_FIELDS:
        if key in fresh and fresh[key] != route[key]:
            _fail('host_decision_changed')
    path, db = _open(journal, write=False)
    with closing(db):
        db.execute('BEGIN')
        try:
            row = _row(db)
            if (row['state'] not in ('active', 'restored_old') or
                    row['selected'] is None or row['selected_route_sha'] not in allowed or
                    _project(route) != (row['project_root'], row['project_dev'], row['project_ino']) or
                    route['scopeId'] != row['scope_id'] or
                    any(route.get(k) != row['selected'].get(k) for k in
                        ('pluginRoot', 'installId', 'generation',
                         'bundleManifestSha256', 'ownershipManifestSha256',
                         'vectorProfile'))):
                _fail('inactive_or_mismatched_journal')
            if read_only:
                # The bound cache may be replaced or removed by a host plugin
                # update. Its sealed identity is the journal's own record.
                proof = {key: row['selected'][key] for key in
                         ('sourceCommit', 'bundleManifestSha256',
                          'ownershipManifestSha256', 'vectorProfile')}
            else:
                proof = _inspect_identity(row['selected'])
            # Only Laya-owned evidence binds the route: the global QMD
            # directories are neither read nor required to exist.
            # Read-only DB/queue identity classification. The runtime repeats
            # both checks when opening writable connections.
            db_path, queue_path = Path(route['dbPath']), Path(route['queuePath'])
            if (not db_path.is_absolute() or not queue_path.is_absolute() or
                    db_path.is_symlink() or queue_path.is_symlink() or
                    db_path == queue_path):
                _fail('invalid_index_paths')
            for candidate_path in (db_path, queue_path):
                canonical_path = candidate_path.resolve(strict=True)
                for plugin_root in {Path(row['selected']['pluginRoot']),
                                    *((_executing_root(),) if read_only else ())}:
                    if canonical_path == plugin_root or plugin_root in canonical_path.parents:
                        _fail('index_path_overlaps_plugin')
            with closing(sqlite3.connect(db_path.resolve(strict=True).as_uri() + '?mode=ro',
                                         uri=True, timeout=2)) as index_db:
                index_db.execute('BEGIN')
                LayaIndexA._check_header(index_db)
                incarnation = index_db.execute(
                    "SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()[0]
                if incarnation != route['dbIncarnation']:
                    _fail('index_incarnation_mismatch')
                index_db.commit()
            with closing(sqlite3.connect(queue_path.resolve(strict=True).as_uri() + '?mode=ro',
                                         uri=True, timeout=2)) as queue_db:
                queue_db.execute('BEGIN')
                if ClaimQueue._check_database(queue_db) != incarnation:
                    _fail('queue_incarnation_mismatch')
                queue_db.commit()
            db.commit()
        except (OSError, RuntimeError, sqlite3.Error, IndexContractError,
                UnknownDatabase) as exc:
            db.rollback()
            raise JournalEvidenceError('runtime_identity_unavailable') from exc
        except BaseException:
            db.rollback()
            raise
    full = row['selected']['bundleKind'] == 'full_plugin'
    if read_only:
        return _read_only_proof(path, row, route, proof, full)
    # The UID queue may be held by another installed version. Its dispatcher
    # needs the sealed target identity before it can enter that version's code;
    # a host/query/worker still requires the executing-root proof below.
    executing_root = (_verified_executing_root(row['selected'], proof)
                      if full and not dispatch_target else None)
    attestation, observed_sha = _attestation(path) if full else (None, None)
    sealed_sha = row['provider_attestation_sha']
    if sealed_sha is not None and observed_sha != sealed_sha:
        _fail('production_attestation_changed')
    provider_manifest = None
    if sealed_sha is not None:
        if (row['provider_file_identity'] is None or
                _provider_file_identity(attestation) != row['provider_file_identity'] or
                attestation.get('vectorProfile') != proof.get('vectorProfile') or
                any(attestation.get(key) != proof.get(key) for key in
                    ('sourceCommit', 'bundleManifestSha256',
                     'ownershipManifestSha256'))):
            _fail('production_provider_identity_changed')
        # Full hashes and recipe closure were checked at installer activation.
        # The journal seals that result; per-hook stat checks keep the model
        # and native closure bounded without reopening hundreds of MB.
        provider_manifest = attestation['providerManifest']
    approved = full and sealed_sha is not None and provider_manifest is not None
    return {'status': ('installer_active_full_plugin_verified' if approved else
                       'production_attestation_unapproved' if sealed_sha is not None else
                       'production_attestation_unbound' if full else
                       'engine_subbundle_verified_host_closure_unproved'),
            'productRouteAllowed': approved,
            'journalPath': str(path), 'selectionEpoch': row['epoch'],
            'pluginRoot': row['selected']['pluginRoot'], 'sourceCommit': proof['sourceCommit'],
            **({'executingPluginRoot': executing_root}
               if full and not dispatch_target else {}),
            'bundleManifestSha256': proof['bundleManifestSha256'],
            'ownershipManifestSha256': proof['ownershipManifestSha256'],
            'installId': route['installId'], 'generation': route['generation'],
            'projectRoot': route['projectRoot'], 'scopeId': route['scopeId'],
            'dbIncarnation': route['dbIncarnation'],
            'vectorProfile': proof.get('vectorProfile'),
            **({'providerManifest': provider_manifest} if approved else {})}


def _read_only_proof(path, row, route, proof, full):
    """The sealed provider record for serving the published generation only.

    The attestation must still be the exact bytes the journal sealed and must
    name the journal's selected bundle; the per-file provider identities in
    the bound bundle are not re-stated because that bundle may be gone.
    """
    sealed_sha = row['provider_attestation_sha']
    if not full or sealed_sha is None or row['provider_file_identity'] is None:
        _fail('read_only_route_unapproved')
    attestation, observed_sha = _attestation(path)
    if observed_sha != sealed_sha or not isinstance(attestation, dict):
        _fail('production_attestation_changed')
    if (attestation.get('vectorProfile') != row['selected']['vectorProfile'] or
            any(attestation.get(key) != row['selected'].get(key) for key in
                ('sourceCommit', 'bundleManifestSha256', 'ownershipManifestSha256')) or
            not isinstance(attestation.get('providerManifest'), dict)):
        _fail('production_provider_identity_changed')
    return {'status': 'installer_active_read_only',
            'productRouteAllowed': False, 'readOnlyAllowed': True,
            'journalPath': str(path), 'selectionEpoch': row['epoch'],
            'pluginRoot': row['selected']['pluginRoot'],
            'executingPluginRoot': str(_executing_root()),
            'sourceCommit': proof['sourceCommit'],
            'bundleManifestSha256': proof['bundleManifestSha256'],
            'ownershipManifestSha256': proof['ownershipManifestSha256'],
            'installId': route['installId'], 'generation': route['generation'],
            'projectRoot': route['projectRoot'], 'scopeId': route['scopeId'],
            'dbIncarnation': route['dbIncarnation'],
            'vectorProfile': proof.get('vectorProfile'),
            'providerManifest': attestation['providerManifest']}


def verify_selected_route(decision):
    """Full selected-route proof, including the currently executing bundle."""
    return _verify_selected_route(decision)


def verify_selected_route_read_only(decision):
    """Route, journal, store and sealed-attestation proof without bundle proof.

    For serving the last published generation after a host plugin update
    moved the executing bundle. Never authorizes sync, compile or any write.
    """
    return _verify_selected_route(decision, read_only=True)


def verify_dispatch_target(decision):
    """Read-only owner proof for choosing a child; never authorizes work here."""
    proof = _verify_selected_route(decision, dispatch_target=True)
    if proof['productRouteAllowed'] is not True:
        _fail('dispatch_target_unapproved')
    return proof


def verify_worker_attestation(decision):
    """Rehash model, worker and native closure once before an owned worker starts."""
    selected = verify_selected_route(decision)
    if selected.get('productRouteAllowed') is not True:
        _fail('worker_provider_unapproved')
    path, db = _open(selected['journalPath'], write=False)
    with closing(db):
        db.execute('BEGIN')
        try:
            row = _row(db)
            if (row['epoch'] != selected['selectionEpoch'] or
                    row['provider_attestation_sha'] is None or
                    row['selected'] is None):
                _fail('worker_selection_changed')
            identity = row['selected']
            expected_sha = row['provider_attestation_sha']
            db.commit()
        except BaseException:
            db.rollback()
            raise
    proof = _inspect_identity(identity)
    record, digest = _attestation(path)
    if digest != expected_sha:
        _fail('worker_attestation_changed')
    try:
        manifest = validate_production_provider_attestation(
            proof.get('vectorProfile'), proof, record)
    except BundleEvidenceError as exc:
        raise JournalEvidenceError('worker_attestation_invalid') from exc
    _verify_runtime_files(record)
    if verify_selected_route(decision) != selected:
        _fail('worker_selection_changed')
    return manifest


def _verify_runtime_files(record):
    """Re-hash every runtime file (node, JS dependencies, native, model)."""
    from laya_embed_runtime import RuntimeUnavailable, verify_attested_files
    try:
        verify_attested_files(record['providerManifest'], record.get(RUNTIME_FILES_FIELD))
    except (RuntimeUnavailable, OSError, KeyError, TypeError) as exc:
        raise JournalEvidenceError('worker_runtime_files_invalid') from exc


def verify_preview_worker_attestation(journal_path, provider_manifest):
    """Before a pending build's worker starts: its sealed record's runtime files.

    The preview proof already validated the record against the journal's
    reviewed digest; this re-hashes every runtime file it executes.
    """
    record, _digest = _attestation(_private_parent(journal_path))
    if (not isinstance(record, dict) or not isinstance(provider_manifest, dict) or
            record.get('providerManifest') != provider_manifest):
        _fail('worker_attestation_changed')
    _verify_runtime_files(record)
    return provider_manifest


def verify_development_vector_approval(decision, approval_manifest_path):
    """Explicit, read-only L0 proof; never authorizes the product hook."""
    proof = verify_selected_route(decision)
    if proof['productRouteAllowed'] or proof['vectorProfile'] is None:
        _fail('development_approval_not_applicable')
    try:
        path = Path(approval_manifest_path)
    except (TypeError, ValueError) as exc:
        raise JournalEvidenceError('invalid_development_approval_path') from exc
    if (not path.is_absolute() or path.is_symlink() or
            path != Path(proof['journalPath']).parent / 'development-vector-approval.json'):
        _fail('invalid_development_approval_path')
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or
                info.st_size > 8192):
            _fail('unsafe_development_approval')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            opened = os.fstat(fd)
            raw = os.read(fd, 8193)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        now = path.lstat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                              s.st_ctime_ns)
        if (len(raw) > 8192 or identity(info) != identity(opened) or
                identity(info) != identity(after) or identity(info) != identity(now)):
            _fail('development_approval_changed')
        approval = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                              parse_constant=_nonfinite)
    except (OSError, TypeError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalEvidenceError('development_approval_unavailable') from exc
    expected = {'schema': DEVELOPMENT_APPROVAL_SCHEMA, 'testOwned': True,
                'pluginRoot': proof['pluginRoot'], 'sourceCommit': proof['sourceCommit'],
                'bundleManifestSha256': proof['bundleManifestSha256'],
                'ownershipManifestSha256': proof['ownershipManifestSha256'],
                'vectorProfile': proof['vectorProfile']}
    if type(approval) is not dict or approval != expected:
        _fail('development_approval_mismatch')
    return {**proof, 'status': 'development_only_vector_provider_verified',
            'productRouteAllowed': False, 'developmentOnlyRouteAllowed': True}
